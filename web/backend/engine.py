"""Meeting engine — orchestrates watcher, Bedrock analysis, chat, and summary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Callable, Awaitable

from web.backend import bleed_dedup
from web.backend.llm_service import LLMService, InvalidModelError, system_prompt_blocks
from web.backend.meeting_context import MeetingContext
from web.backend.name_registry import NameRegistry
from web.backend.prompt_loader import PromptLoader
from web.backend.user_profile import load_user_profile
from web.backend.session_store import (
    save_session, load_session as _load_session_from_store,
    search_sessions, load_session_details, update_session_meeting_context,
)
from web.backend.srt_parser import SrtSegment, segments_to_text

logger = logging.getLogger(__name__)

NO_ALERT = "[NO_ALERT]"


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "prompts")
PROMPTS = PromptLoader(PROMPTS_DIR)

# When LOG_CONTENT=false, log lengths/counts instead of transcript/chat text (§4.4).
_LOG_CONTENT = _env_bool("LOG_CONTENT", default=True)

# Transcript windowing settings
ANALYSIS_WINDOW = 50     # segments sent as context for analysis
ANALYSIS_SEGMENT_THRESHOLD = 3  # min pending segments for the time-based analysis trigger
ANALYSIS_MIN_CHARS = 120  # skip analysis when the combined new text is shorter than this
CHAT_WINDOW = 100        # segments sent as context for chat
ROLLING_SUMMARY_INTERVAL = 200  # generate rolling summary every N segments


def _analysis_min_segments() -> int:
    """Pending-segment count that fires an analysis regardless of elapsed time
    (ANALYSIS_MIN_SEGMENTS env, default 8) — §3.4."""
    try:
        return int(os.environ.get("ANALYSIS_MIN_SEGMENTS", "").strip() or 8)
    except ValueError:
        return 8


def _analysis_min_interval_s() -> float:
    """Seconds since the last analysis after which a smaller batch (>=3 segments)
    is allowed to fire (ANALYSIS_MIN_INTERVAL_S env, default 45) — §3.4."""
    try:
        return float(os.environ.get("ANALYSIS_MIN_INTERVAL_S", "").strip() or 45)
    except ValueError:
        return 45.0

# Audio-bleed dedup constants + the pure matching algorithm now live in
# bleed_dedup.py (§6); this engine keeps only the mic-pending buffering/flush
# orchestration below, which owns the asyncio state.

CHAT_TOOLS = [
    {"toolSpec": {
        "name": "search_sessions",
        "description": "Search across all saved meeting sessions by keyword. Returns matching sessions with snippets showing where the match was found.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search terms to find in session transcripts, chat history, summaries, and insights"}},
            "required": ["query"],
        }},
    }},
    {"toolSpec": {
        "name": "load_session_details",
        "description": "Load full details of a specific saved session including transcript, summary, insights, and meeting context.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Session ID to load (e.g. '2026-03-19-1634')"}},
            "required": ["session_id"],
        }},
    }},
    {"toolSpec": {
        "name": "update_session_meeting_context",
        "description": "Update the meeting context (title, attendees) for a saved session. Use this to correct the title or attendees recorded for a meeting.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to update"},
                "title": {"type": "string", "description": "Corrected meeting title"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Corrected attendee list"},
            },
            "required": ["session_id"],
        }},
    }},
    {"toolSpec": {
        "name": "get_full_transcript",
        "description": "Get the full transcript of the current live meeting. Use this when the user asks about decisions, action items, or anything that requires reviewing the complete discussion — not just the recent window visible in context.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {},
        }},
    }},
]


# ── Knowledge vault (post-meeting KB) ──────────────────

VAULT_TOOLS = [
    {"toolSpec": {
        "name": "fs_read",
        "description": "Read a UTF-8 text file from the knowledge vault or the raw meetings dir (read-only). Use the absolute paths given in the vault instructions.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path of the file to read"}},
            "required": ["path"],
        }},
    }},
    {"toolSpec": {
        "name": "fs_write",
        "description": "Write a UTF-8 text file under the knowledge vault's outputs/ directory (generated artifacts only). Writes anywhere else — vault root, projects/, people/, sources/, or the raw meetings dir — are denied.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path under the vault's outputs/ directory"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        }},
    }},
]

_FS_READ_CAP = 200_000  # max chars returned from a single fs_read


def _resolve_within(path: str, roots: list[str]) -> str | None:
    """Resolve `path` to an absolute realpath, returned only if it sits within
    one of `roots` (defends against `..` traversal and symlink escape). Else None."""
    if not path:
        return None
    rp = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        root_rp = os.path.realpath(root)
        if rp == root_rp or rp.startswith(root_rp + os.sep):
            return rp
    return None


def _fs_read(path: str) -> dict:
    """Tool: read a file from the vault or the raw meetings dir (read-only)."""
    from web.backend import knowledge_store as _ks
    from web.backend import session_store as _ss
    rp = _resolve_within(path, [_ks.KNOWLEDGE_DIR, _ss.SESSIONS_DIR])
    if rp is None:
        return {"error": f"read denied (outside vault/meetings): {path}"}
    if not os.path.isfile(rp):
        return {"error": f"not found: {path}"}
    try:
        with open(rp, encoding="utf-8") as f:
            return {"path": path, "content": f.read(_FS_READ_CAP)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fs_write(path: str, content: str) -> dict:
    """Tool: write a file under the vault's outputs/ directory only (§4.3).

    Raw meetings are immutable and the broader vault is read-only via the tool
    interface; only artifacts under outputs/ may be created or overwritten.
    """
    from web.backend import knowledge_store as _ks
    outputs_root = os.path.join(_ks.KNOWLEDGE_DIR, "outputs")
    rp = _resolve_within(path, [outputs_root])
    if rp is None:
        return {"error": f"write denied (only inside vault outputs/): {path}"}
    try:
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(content or "")
        _ks.append_log(f"fs_write {os.path.relpath(rp, _ks.KNOWLEDGE_DIR)} via chat")
        return {"path": path, "ok": True, "bytes": len(content or "")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _knowledge_vault_instruction() -> str:
    """Vault usage instructions appended to the chat system prompt (absolute
    paths so the agent's fs_read/fs_write work regardless of cwd)."""
    from web.backend import knowledge_store as _ks
    from web.backend import session_store as _ss
    vault = _ks.KNOWLEDGE_DIR
    raw = _ss.SESSIONS_DIR
    return f"""

A knowledge vault of past meetings, projects, and people lives at {vault}/ (raw meetings live at {raw}/). When the user asks about a project or past meetings, or asks you to compile/draft an artifact (brief, architecture, cost estimate, freeform doc):
1. fs_read {vault}/AGENTS.md first — it documents the layout, frontmatter schemas, and workflows.
2. Read {vault}/projects/<slug>.md and any source pages it links to ({vault}/sources/<id>.md), then synthesize an answer with [[wikilink]] citations.
3. For artifact requests, fs_write the result to {vault}/outputs/<slug>-<type>-YYYY-MM-DD.md and reply with the file path. Type is one of brief, architecture, cost, freeform. Note: fs_write is restricted to {vault}/outputs/ only — writes to any other path will be denied.
4. Never modify {raw}/ — those are immutable raw records.
5. Append a single line to {vault}/log.md after any vault write.
"""


_KNOWLEDGE_VAULT_INSTRUCTION = _knowledge_vault_instruction()


SPEAKER_PROMPT_THRESHOLD = 3   # utterances from an unresolved cluster before we ask
SPEAKER_RECONCILE_INTERVAL = 50  # run mid-meeting cluster reconciliation every N new segments
SPEAKER_INHERIT_WINDOW_S = 3.0   # inherit prior speaker label across gaps shorter than this
                                  # (a short utterance < 2s gets no embedding, falls back to
                                  # the channel default "Others" — almost always a continuation
                                  # of whoever was just speaking)
_CLUSTER_RE = re.compile(r"^Speaker \d+$")


def _is_cluster_key(label: str | None) -> bool:
    """True iff the speaker label looks like a diarization cluster key (not a real name)."""
    return bool(label) and bool(_CLUSTER_RE.match(label))


class MeetingEngine:
    def __init__(self, llm: LLMService, emit: Callable[[dict], Awaitable[None]],
                 speaker_embedder=None, llm_fast: LLMService | None = None):
        self.llm = llm
        # Cheap "background" tier for extraction/classification (analysis,
        # curation, rolling summary, chat-insight) — §3.1. Falls back to the
        # primary service when a fast tier isn't wired (tests, callers pre-§3.1).
        self.llm_fast = llm_fast or llm
        self.emit = emit
        self.speaker_embedder = speaker_embedder
        self.segments: list[SrtSegment] = []
        self.insights: list[dict] = []
        self.explained_terms: set[str] = set()  # track terms already surfaced
        self.chat_history: list[dict] = []
        self.summary: str = ""
        self.meeting_context: MeetingContext = MeetingContext()
        self.user_profile: str = load_user_profile()
        self.srt_path: str = ""
        self.started_at: str = ""
        self.session_id: str = ""
        self.active = False
        self.paused = False
        self.chat_ready = False
        self.ended_at: str = ""
        self._next_insight_id = 0
        self._rolling_summary: str = ""
        self._last_rolling_summary_at = 0
        self._pending_segments: list[SrtSegment] = []
        # Coalescing guard: at most one _analyze runs at a time. New segments keep
        # accumulating in _pending_segments while one is in flight (§2.1).
        self._analysis_in_flight = False
        # Monotonic timestamp of the last analysis dispatch — drives the time-based
        # batching trigger (§3.4).
        self._last_analysis_at = 0.0
        self._last_curate_at = 0
        self._mic_pending: list[SrtSegment] = []
        self._mic_flush_task: asyncio.Task | None = None
        self._last_reconcile_at = 0
        # Monotonic timestamp of the most recent segment from either channel.
        # Used by the auto-stop watcher to detect inactivity.
        self._last_segment_at: float = 0.0
        # Set by auto-stop callers; tells _generate_summary to clip trailing
        # silence so the summary doesn't see 30 min of empty tail.
        self._trim_on_summary: bool = False
        self.name_registry = NameRegistry()
        # Vault-derived Whisper vocabulary (people + project titles), loaded once
        # at meeting start when WHISPER_VOCAB_FROM_VAULT is on (§7). Cached here so
        # current_initial_prompt() — called per transcription — stays cheap.
        self._vault_vocab_names: list[str] = []

    def _spawn_task(self, coro) -> asyncio.Task:
        """Create a background task with error logging."""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._task_done)
        return task

    @staticmethod
    def _task_done(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Background task failed: {type(exc).__name__}: {exc}", exc_info=exc)

    async def _fast_analyze(self, messages, system_prompt=None, *,
                            use_cache: bool = False, max_tokens: int = 4096) -> str:
        """Run an ``analyze()`` on the fast (background) tier, degrading gracefully
        when the fast model ID is invalid.

        Graceful degradation: if the fast tier's model ID is rejected
        (InvalidModelError) and a *distinct* fast tier is configured, log one
        warning, tell the UI once, permanently swap ``llm_fast → llm`` for the rest
        of the session, and retry the same call on the primary model. If the fast
        tier already *is* the primary (so the primary model ID is the invalid one),
        re-raise so each caller's own error handling surfaces it. Keeps the four
        fast-tier call sites (analysis / curation / rolling summary / chat-insight)
        clean.
        """
        try:
            return await self.llm_fast.analyze(
                messages, system_prompt, use_cache=use_cache, max_tokens=max_tokens,
            )
        except InvalidModelError:
            if self.llm_fast is self.llm:
                raise  # primary is also invalid — let the caller's handler surface it
            logger.warning(
                "Fast model ID invalid — falling back to the primary model for the "
                "rest of this session (fix in Settings → AI Provider)."
            )
            self.llm_fast = self.llm
            await self.emit({
                "type": "info",
                "text": "Fast model ID invalid — falling back to the primary model "
                        "(fix in Settings → AI Provider)",
            })
            return await self.llm_fast.analyze(
                messages, system_prompt, use_cache=use_cache, max_tokens=max_tokens,
            )

    @property
    def transcript(self) -> str:
        return segments_to_text(self.segments)

    @property
    def is_vault_chat_only(self) -> bool:
        """No live meeting and no loaded session — chat is pure vault Q&A."""
        return not self.active and not self.segments

    def _known_terms_str(self) -> str:
        return ", ".join(sorted(self.explained_terms)) if self.explained_terms else "(none yet)"

    def _system(self, window: int = CHAT_WINDOW):
        """Chat system prompt, split into a cacheable stable prefix + volatile
        suffix (§3.2).

        soul.md is authored stable-first with a ``===VOLATILE_BOUNDARY===`` marker:
        everything before it (role, style rules, user profile, vault instructions)
        is identical for the whole meeting; everything after (meeting context,
        summaries, transcript window, explained terms) changes every turn. Returns
        the structured block list from ``system_prompt_blocks`` so the Bedrock layer
        puts the cachePoint after the stable prefix only. The vault-only case has no
        volatile content and returns a plain string.
        """
        # No live meeting and no loaded transcript → the chat box is a pure
        # vault Q&A interface.
        if self.is_vault_chat_only:
            return (PROMPTS.render("vault-chat", user_profile=self.user_profile)
                    + _KNOWLEDGE_VAULT_INSTRUCTION)
        ctx = self.meeting_context.to_prompt_text() if self.meeting_context.title != "Unknown Meeting" else ""
        summary_ctx = f"Previous meeting summary:\n{self.summary}" if self.summary else ""
        rolling = f"Summary of earlier discussion:\n{self._rolling_summary}" if self._rolling_summary else ""
        windowed = segments_to_text(self.segments[-window:]) if self.segments else "(no transcript yet)"
        rendered = PROMPTS.render(
            "soul",
            user_profile=self.user_profile,
            meeting_context=ctx,
            summary_context=summary_ctx,
            rolling_summary=rolling,
            transcript=windowed,
            window_size=window,
            explained_terms=self._known_terms_str(),
        )
        stable, _, volatile = rendered.partition("===VOLATILE_BOUNDARY===")
        stable = stable.strip() + _KNOWLEDGE_VAULT_INSTRUCTION
        return system_prompt_blocks(stable, volatile.strip())

    def _analysis_system(self):
        """Compact, cacheable system prompt for the analysis call (§3.7).

        Just the extraction role, insight style rules, and user profile — the
        recent-transcript window lives in the USER message instead. Fully stable
        across a meeting, so it's returned as a single cacheable block.
        """
        stable = PROMPTS.render("analysis-system", user_profile=self.user_profile)
        return system_prompt_blocks(stable)

    def current_initial_prompt(self) -> str | None:
        """Assemble the Whisper `initial_prompt` for the next transcription.

        Layered (see transcript_prompt.py): the standing WHISPER_VOCABULARY list
        is always included; meeting attendees and user-confirmed speaker names
        are opt-in (default off) via WHISPER_PROMPT_INCLUDE_ATTENDEES /
        WHISPER_PROMPT_INCLUDE_SPEAKER_NAMES. Called per transcription on both
        channels — cheap (env lookups + a short join). Returns None when there's
        nothing to bias with, so transcription behaves exactly as before.
        """
        from web.backend.transcript_prompt import build_initial_prompt

        names: list[str] = []
        if _env_bool("WHISPER_PROMPT_INCLUDE_ATTENDEES", False):
            if self.meeting_context.title != "Unknown Meeting" or self.meeting_context.attendees:
                names.extend(self.meeting_context.attendees or [])
        if _env_bool("WHISPER_PROMPT_INCLUDE_SPEAKER_NAMES", False):
            names.extend(self.name_registry.assigned_names())
        # Vault people/project names (opt-in, §7). Appended after attendees/
        # confirmed speakers so — since build_initial_prompt keeps the prompt tail
        # on truncation — these user-vault names survive the budget last. Read from
        # the per-meeting cache (populated in start()), never re-scanned here.
        if _env_bool("WHISPER_VOCAB_FROM_VAULT", False):
            names.extend(self._vault_vocab_names)

        try:
            max_tokens = int(os.environ.get("WHISPER_PROMPT_MAX_TOKENS", "").strip() or 150)
        except ValueError:
            max_tokens = 150
        return build_initial_prompt(
            os.environ.get("WHISPER_VOCABULARY", ""), names=names, max_tokens=max_tokens
        )

    @staticmethod
    def _load_vault_vocab_names() -> list[str]:
        """People + project titles from the knowledge vault, for the Whisper
        vocabulary prompt (§7). Order: people first, then projects; the
        downstream build_initial_prompt dedupes and applies the token cap.
        """
        from web.backend import knowledge_store
        names: list[str] = []
        for entry in knowledge_store.list_people():
            title = (entry.get("title") or "").strip()
            if title:
                names.append(title)
        for entry in knowledge_store.list_projects():
            title = (entry.get("title") or "").strip()
            if title:
                names.append(title)
        return names

    async def start(self, meeting_context: MeetingContext | None = None):
        self.segments = []
        self.insights = []
        self.explained_terms = set()
        self.chat_history = []
        self.summary = ""
        self.started_at = datetime.now().isoformat()
        self.ended_at = ""
        self.meeting_context = meeting_context or MeetingContext()
        # Reload the profile each meeting so edits apply without a server restart.
        self.user_profile = load_user_profile()
        self._pending_segments = []
        self._analysis_in_flight = False
        self._last_analysis_at = time.monotonic()
        self._last_curate_at = 0
        self._mic_pending = []
        self._mic_flush_task = None
        self._last_reconcile_at = 0
        self._last_segment_at = time.monotonic()  # treat session start as recent activity
        self._trim_on_summary = False
        self.name_registry.reset()
        self.name_registry.add_known_names(self.meeting_context.attendees or [])
        # Load vault people/project names once for the Whisper vocab prompt (§7).
        # Off by default; kept cheap thereafter since current_initial_prompt runs
        # per transcription. A vault read failure must never block the meeting.
        self._vault_vocab_names = []
        if _env_bool("WHISPER_VOCAB_FROM_VAULT", False):
            try:
                self._vault_vocab_names = self._load_vault_vocab_names()
            except Exception as e:
                logger.warning(f"Vault vocab load failed (continuing without it): {e}")
        self.active = True
        self.paused = False
        self.chat_ready = True
        status = self._status_payload(segment_count=0)
        if self.meeting_context.title != "Unknown Meeting":
            status["meeting_title"] = self.meeting_context.title
        await self.emit(status)

    async def stop(self):
        self.active = False
        self.paused = False
        self.ended_at = datetime.now().isoformat()
        if self._trim_on_summary:
            self._trim_trailing_silence()
        await self._generate_summary()
        await asyncio.to_thread(self._save_session)
        await self.emit(self._status_payload(active=False, segment_count=len(self.segments)))

    async def pause(self) -> None:
        """Stop capture/transcribers but keep the engine session alive.

        Capture teardown is done by the caller (main.py) — the engine only
        tracks the flag. Timer freezes client-side based on this status.
        """
        if not self.active or self.paused:
            return
        self.paused = True
        await self.emit(self._status_payload(segment_count=len(self.segments)))
        logger.info("Engine paused")

    async def resume(self) -> None:
        """Re-enter active state. Capture relaunch is handled by the caller."""
        if not self.active or not self.paused:
            return
        self.paused = False
        # Treat resume as fresh activity so the inactivity watcher doesn't
        # immediately fire on an old timestamp.
        self._last_segment_at = time.monotonic()
        await self.emit(self._status_payload(segment_count=len(self.segments)))
        logger.info("Engine resumed")

    def _status_payload(self, *, active: bool | None = None, segment_count: int | None = None) -> dict:
        return {
            "type": "status",
            "meeting_active": self.active if active is None else active,
            "paused": self.paused,
            "chat_ready": self.chat_ready,
            "segment_count": len(self.segments) if segment_count is None else segment_count,
        }

    def last_segment_age_s(self) -> float:
        """Seconds since the last real segment arrived on either channel.

        Returns a large number if no segments yet (treated as no-activity).
        """
        if self._last_segment_at <= 0:
            return float("inf")
        return time.monotonic() - self._last_segment_at

    def _trim_trailing_silence(self) -> None:
        """Clip segments to the last non-empty transcript entry so an auto-stop
        summary doesn't see a long silent tail. Safe no-op if nothing to trim.
        """
        if not self.segments:
            return
        last_real = -1
        for i in range(len(self.segments) - 1, -1, -1):
            if (self.segments[i].text or "").strip():
                last_real = i
                break
        if last_real == -1:
            # No real segments at all — preserve them as-is rather than emptying.
            return
        if last_real < len(self.segments) - 1:
            dropped = len(self.segments) - 1 - last_real
            self.segments = self.segments[: last_real + 1]
            logger.info(f"Trimmed {dropped} trailing silent segment(s) before summary")

    def reset(self):
        self.segments = []
        self.insights = []
        self.explained_terms = set()
        self.chat_history = []
        self.summary = ""
        self.meeting_context = MeetingContext()
        self._mic_pending = []
        self._mic_flush_task = None
        self._pending_segments = []
        self._analysis_in_flight = False
        self._last_analysis_at = 0.0
        self._last_segment_at = 0.0
        self._trim_on_summary = False
        self.name_registry.reset()
        self.active = False
        self.paused = False
        self.chat_ready = False

    # ── Speaker naming ─────────────────────────────────

    async def confirm_speaker(self, cluster: str, name: str) -> None:
        """Assign a real name to a diarization cluster.

        - Merges the cluster into an existing cluster if `name` was already
          assigned to a different cluster (server-side centroid merge).
        - Rewrites every past segment whose speaker matches the old cluster
          (or the old cluster's assigned name, if it was renamed earlier).
        - Emits `speaker_update` so the frontend re-renders.
        """
        name = name.strip()
        if not name:
            return
        # Find any cluster that was previously assigned to this name — merge target.
        existing_cluster = self.name_registry.original_cluster(name)
        merge_into = existing_cluster if (existing_cluster and existing_cluster != cluster) else None

        # Rename in the embedder (merges centroids if merge_into is not None).
        if self.speaker_embedder is not None:
            try:
                if merge_into:
                    self.speaker_embedder.rename_cluster(cluster, merge_into)
                else:
                    # No merge — just rename the centroid key to the real name so
                    # future identify() calls return the name directly.
                    self.speaker_embedder.rename_cluster(cluster, name)
            except Exception as e:
                logger.warning(f"SpeakerEmbedder rename failed ({cluster}→{name}): {e}")

        self.name_registry.assign(cluster, name)
        if merge_into:
            # Both clusters now resolve to the same name.
            self.name_registry.assign(merge_into, name)

        # Rewrite past segments. Match by both the cluster key and the stale name.
        updated_indexes: list[int] = []
        new_segments: list[SrtSegment] = []
        old_labels = {cluster}
        if merge_into:
            old_labels.add(merge_into)
        # If the embedder previously renamed this cluster to the real name,
        # past segments may carry the raw name. Include that too.
        for seg in self.segments:
            if seg.speaker in old_labels and seg.speaker != name:
                new_segments.append(SrtSegment(
                    index=seg.index, start=seg.start, end=seg.end,
                    text=seg.text, speaker=name,
                ))
                updated_indexes.append(seg.index)
            else:
                new_segments.append(seg)
        self.segments = new_segments

        if updated_indexes:
            await self.emit({
                "type": "speaker_update",
                "updates": [{"index": i, "speaker": name} for i in updated_indexes],
            })
        logger.info(
            f"Speaker confirmed: {cluster} → {name}"
            + (f" (merged with {merge_into})" if merge_into else "")
            + f", {len(updated_indexes)} segment(s) relabeled"
        )

        # If the meeting is already stopped (post-meeting batch or review mode),
        # persist the relabel to the saved session file so summaries and searches
        # reflect the real name.
        if not self.active and self.session_id and updated_indexes:
            try:
                await asyncio.to_thread(self._save_session)
            except Exception as e:
                logger.warning(f"Failed to re-save session after confirm_speaker: {e}")

        # Persistent voice registry (opt-in, §7): remember this person's voiceprint
        # so they're recognized from the first utterance in future meetings. No-op
        # unless SPEAKER_REGISTRY_ENABLED. After the rename above, the centroid lives
        # under the merge target (a cluster key) or the real name key.
        await self._maybe_enroll_speaker(name, merge_into)

    async def _maybe_enroll_speaker(self, name: str, merge_into: str | None) -> None:
        """Persist a confirmed speaker's centroid to the registry when enabled."""
        try:
            from web.backend import speaker_registry
            if not speaker_registry.enabled() or self.speaker_embedder is None:
                return
            centroid = self.speaker_embedder.get_centroid(merge_into or name)
            if centroid is None:
                centroid = self.speaker_embedder.get_centroid(name)
            if centroid is not None:
                await asyncio.to_thread(speaker_registry.enroll, name, centroid)
        except Exception as e:
            logger.warning(f"Speaker registry enroll failed ({name}): {e}")

    async def skip_speaker(self, cluster: str) -> None:
        """Mark a cluster as intentionally unlabeled — do not prompt again."""
        self.name_registry.skip(cluster)
        logger.info(f"Speaker skipped: {cluster}")

    async def defer_speaker(self, cluster: str) -> None:
        """Mark a cluster for batch resolution at meeting end."""
        self.name_registry.defer(cluster)
        logger.info(f"Speaker deferred: {cluster}")

    async def set_skip_all_speakers(self, on: bool) -> None:
        """Toggle the per-session 'stop prompting me' flag."""
        self.name_registry.set_skip_all(on)
        logger.info(f"Speaker skip_all set to {on}")

    def unresolved_speaker_clusters(self) -> list[dict]:
        """Clusters deferred or still un-prompted — used for batch/review view.

        Each entry: {cluster, samples, count}. Candidates list is computed fresh
        by the caller via name_registry.candidates().
        """
        out: list[dict] = []
        for cluster in self.name_registry.clusters_for_batch():
            out.append({
                "cluster": cluster,
                "samples": self.name_registry.samples_for(cluster),
                "count": self.name_registry.count_for(cluster),
            })
        return out

    async def reconcile_speaker_clusters(self) -> None:
        """Run end-of-meeting agglomerative re-clustering on diarization output.

        The live online clusterer fragments speakers across a meeting (short
        utterances, cross-talk, drifting centroids). Before we generate the
        summary, consolidate `Speaker 17` / `Speaker 25` style fragments into
        a smaller, more realistic set of merged clusters. Only touches unresolved
        segments — anything the user already named is preserved.
        """
        if self.speaker_embedder is None:
            return
        try:
            mapping = await asyncio.to_thread(self.speaker_embedder.reconcile_clusters)
        except Exception as e:
            logger.warning(f"Speaker re-clustering failed (continuing): {e}")
            return
        if not mapping:
            return

        # Rewrite segments and name-registry state for unresolved cluster keys only.
        # If the user already assigned a real name to a source cluster, keep that
        # name — don't let merging quietly overwrite user intent.
        updated: list[SrtSegment] = []
        updated_indexes: dict[int, str] = {}
        for seg in self.segments:
            label = seg.speaker
            if not _is_cluster_key(label):
                updated.append(seg)
                continue
            new_label = mapping.get(label, label)
            if new_label == label:
                updated.append(seg)
                continue
            updated.append(SrtSegment(
                index=seg.index, start=seg.start, end=seg.end,
                text=seg.text, speaker=new_label,
            ))
            updated_indexes[seg.index] = new_label
        self.segments = updated

        # Fold name-registry state into the merged target cluster so any
        # remaining batch prompts use the consolidated counts/samples.
        for src, dst in mapping.items():
            # If the source cluster was already assigned a real name, carry
            # that name over to the destination too (so future resolve() is
            # consistent). Unlikely in practice since assigned clusters rarely
            # re-appear in the mapping, but it keeps state coherent.
            if self.name_registry.is_assigned(src):
                self.name_registry.assign(dst, self.name_registry.resolve(src))
            self.name_registry.merge_cluster_state(src, dst)

        if updated_indexes:
            await self.emit({
                "type": "speaker_update",
                "updates": [{"index": i, "speaker": name} for i, name in updated_indexes.items()],
            })
        logger.info(
            f"Reconciled speaker clusters: {len(mapping)} merges, "
            f"{len(updated_indexes)} segment(s) relabeled"
        )

    async def emit_speaker_batch_prompt(self) -> None:
        """Emit a batch speaker_batch_prompt containing all unresolved clusters.

        Used at meeting end (for deferred / un-prompted clusters) and when
        a saved session is opened with Speaker N labels remaining.
        """
        clusters = self.unresolved_speaker_clusters()
        if not clusters:
            return
        await self.emit({
            "type": "speaker_batch_prompt",
            "clusters": clusters,
            "candidates": self.name_registry.candidates(),
        })

    def seed_registry_from_loaded_segments(self) -> None:
        """After load_session, populate the name registry from segment labels
        so the review-mode UI can prompt for any unresolved clusters.

        - Unresolved Speaker N segments contribute utterances + samples.
        - Already-real-name segments register as assigned (so those names are
          removed from the candidate pool and future prompts reflect reality).
        - Calendar attendees from meeting_context seed the candidate list.
        """
        self.name_registry.reset()
        self.name_registry.add_known_names(self.meeting_context.attendees or [])
        seen_real_names: dict[str, str] = {}  # real_name → synthetic cluster key
        for seg in self.segments:
            label = seg.speaker
            if not label or label == "You":
                continue
            if _is_cluster_key(label):
                self.name_registry.track_utterance(label, seg.index, seg.text)
            else:
                # Already a real name — synthesize a cluster key and mark assigned
                # so candidates() excludes it and future prompts are consistent.
                synthetic = seen_real_names.setdefault(label, f"_resolved_{label}")
                self.name_registry.assign(synthetic, label)

    @staticmethod
    def _clean_chat_history(history: list[dict]) -> list[dict]:
        """Strip toolUse/toolResult messages and image bytes from chat history."""
        clean = []
        for msg in history:
            content = msg.get("content", [])
            # Skip messages that contain toolResult
            if any("toolResult" in b for b in content if isinstance(b, dict)):
                continue
            # Keep text blocks; strip toolUse blocks and image bytes (too large for persistence)
            kept = [b for b in content if isinstance(b, dict) and "text" in b]
            if kept:
                clean.append({"role": msg["role"], "content": kept})
        return clean

    async def chat(self, text: str, images: list[dict] | None = None):
        if _LOG_CONTENT:
            logger.info(f"Chat received: {text[:80]}... images={len(images) if images else 0}")
        else:
            logger.info(f"Chat received: {len(text)} chars, images={len(images) if images else 0}")
        self.chat_history = self._clean_chat_history(self.chat_history)
        # Build user message with text + optional images
        content: list[dict] = []
        if images:
            import base64
            for img in images:
                try:
                    img_bytes = base64.b64decode(img["data"])
                    content.append({
                        "image": {
                            "format": img.get("format", "png"),
                            "source": {"bytes": img_bytes},
                        }
                    })
                except Exception as e:
                    logger.warning(f"Failed to decode image: {e}")
        content.append({"text": text})
        self.chat_history.append({"role": "user", "content": content})
        if len(self.chat_history) > 20:
            self.chat_history = self.chat_history[-20:]
        try:
            async def handle_tool(name, inp):
                if name == "search_sessions":
                    return {"results": search_sessions(inp.get("query", ""))}
                elif name == "load_session_details":
                    return load_session_details(inp.get("session_id", ""))
                elif name == "update_session_meeting_context":
                    result = update_session_meeting_context(
                        inp.get("session_id", ""),
                        title=inp.get("title"),
                        attendees=inp.get("attendees"),
                    )
                    if inp.get("session_id") == self.session_id and result.get("success"):
                        if inp.get("title") is not None:
                            self.meeting_context.title = inp["title"]
                        if inp.get("attendees") is not None:
                            self.meeting_context.attendees = inp["attendees"]
                    return result
                elif name == "get_full_transcript":
                    if not self.segments:
                        return {"transcript": "(no transcript yet)", "segment_count": 0}
                    return {
                        "transcript": segments_to_text(self.segments),
                        "segment_count": len(self.segments),
                    }
                elif name == "fs_read":
                    return _fs_read(inp.get("path", ""))
                elif name == "fs_write":
                    return _fs_write(inp.get("path", ""), inp.get("content", ""))
                return {"error": f"Unknown tool: {name}"}

            async def on_text(t):
                await self.emit({"type": "chat_chunk", "text": t})

            all_tools = list(CHAT_TOOLS) + VAULT_TOOLS

            # Pass a copy so tool messages don't pollute chat_history
            msgs = list(self.chat_history)
            # Wrap in a task so a timeout can explicitly cancel the inner call —
            # otherwise the Bedrock stream keeps consuming in the background (§2.9).
            chat_task = asyncio.create_task(
                self.llm.converse_stream_with_tools(
                    msgs, self._system(),
                    tools=all_tools, tool_handler=handle_tool, on_text=on_text,
                    use_cache=True,
                )
            )
            try:
                response_text, _ = await asyncio.wait_for(chat_task, timeout=300)
            except asyncio.TimeoutError:
                chat_task.cancel()
                raise
            # Only keep clean text in history
            if response_text:
                self.chat_history.append(LLMService.msg("assistant", response_text))
                # Chat-insight mining is opt-in (§3.7): an extra LLM call per chat
                # answer, low value. Off by default; skipped in vault-only chat (no
                # transcript to mine).
                if not self.is_vault_chat_only and _env_bool("CHAT_INSIGHT_ENABLED", False):
                    self._spawn_task(self._extract_chat_insights(response_text))
        except asyncio.TimeoutError:
            await self.emit({"type": "chat_chunk", "text": "\n⚠️ Response timed out (300s). Bedrock may be overloaded — try again."})
        except Exception as e:
            await self.emit({"type": "chat_chunk", "text": f"Error: {e}"})
        await self.emit({"type": "chat_done"})
        # Auto-save session after post-meeting chat
        if not self.active and self.session_id:
            await asyncio.to_thread(self._save_session)

    async def _extract_chat_insights(self, response: str):
        """Analyze chat response for new terms/context to add to insights."""
        prompt = PROMPTS.render(
            "chat-insight",
            response=response[:2000],
            known_terms=self._known_terms_str(),
            no_alert=NO_ALERT,
        )
        try:
            result = await self._fast_analyze(
                [LLMService.msg("user", prompt)], max_tokens=512,
            )
            if NO_ALERT in result:
                return
            self._parse_insights(result)
        except Exception as e:
            logger.debug(f"Chat insight extraction failed: {e}")

    def load_session(self, session_id: str) -> dict | None:
        data = _load_session_from_store(session_id)
        if not data:
            return None
        self.segments = [SrtSegment(**s) for s in data.get("segments", [])]
        self.insights = data.get("insights", [])
        # Continue insight ids past the highest restored one so post-meeting chat
        # insights don't collide with loaded ids (§2.4). Default 0 when empty.
        self._next_insight_id = max((i["id"] for i in self.insights), default=-1) + 1
        self.explained_terms = set(data.get("explained_terms", []))
        self.chat_history = self._clean_chat_history(data.get("chat_history", []))
        self.summary = data.get("summary", "")
        self.meeting_context = MeetingContext.from_dict(data.get("meeting_context"))
        self.user_profile = load_user_profile()
        self.srt_path = data.get("srt_path", "")
        self.started_at = data.get("started_at", "")
        self.ended_at = data.get("ended_at", "")
        self.session_id = session_id
        self.active = False
        self.chat_ready = True
        return data

    async def on_new_segments(self, batch: list[SrtSegment]):
        """Called by the capture/transcription pipeline with new transcript segments.

        Deduplicates mic segments that are audio bleed from system audio.
        Mic segments are buffered briefly; system audio segments emit immediately.
        """
        try:
            system_segs = [s for s in batch if s.speaker != "You"]
            mic_segs = [s for s in batch if s.speaker == "You"]
            emit_batch: list[SrtSegment] = []

            # 1. System segments: dedup mic buffer against them, emit both
            if system_segs:
                joined_system = bleed_dedup.join_batch(system_segs)
                remaining_mic = []
                for mic_seg in self._mic_pending:
                    if bleed_dedup.is_mic_bleed(mic_seg, system_segs, joined_system):
                        if _LOG_CONTENT:
                            logger.info(f"Dedup: discarded mic bleed: {mic_seg.text[:60]}")
                        else:
                            logger.info(f"Dedup: discarded mic bleed: {len(mic_seg.text)} chars")
                    else:
                        remaining_mic.append(mic_seg)
                self._mic_pending = []
                if self._mic_flush_task and not self._mic_flush_task.done():
                    self._mic_flush_task.cancel()
                    self._mic_flush_task = None
                emit_batch.extend(remaining_mic)
                emit_batch.extend(system_segs)

            # 2. Mic segments: check against recent system segs, buffer remainder
            if mic_segs:
                # Build a joined synthetic from the last contiguous run of
                # non-"You" segments in self.segments — handles the case where
                # system arrived first and got split into sub-segments.
                recent_system_run = []
                for ex in reversed(self.segments[-30:]):
                    if ex.speaker == "You":
                        if recent_system_run:
                            break
                        continue
                    recent_system_run.append(ex)
                recent_system_run.reverse()
                joined_recent = bleed_dedup.join_batch(recent_system_run) if recent_system_run else None

                for mic_seg in mic_segs:
                    is_bleed = bleed_dedup.is_mic_bleed(mic_seg, recent_system_run, joined_recent)
                    if is_bleed:
                        if _LOG_CONTENT:
                            logger.info(f"Dedup: discarded mic bleed (immediate): {mic_seg.text[:60]}")
                        else:
                            logger.info(f"Dedup: discarded mic bleed (immediate): {len(mic_seg.text)} chars")
                    else:
                        self._mic_pending.append(mic_seg)
                if self._mic_pending:
                    if self._mic_flush_task and not self._mic_flush_task.done():
                        self._mic_flush_task.cancel()
                    self._mic_flush_task = asyncio.create_task(self._flush_mic_buffer())

            # 3. Emit confirmed segments
            if emit_batch:
                emit_batch = [self._resolve_segment_speaker(s) for s in emit_batch]
                self.segments.extend(emit_batch)
                self._last_segment_at = time.monotonic()
                await self.emit({"type": "transcript", "segments": [s.to_dict() for s in emit_batch]})
                self._pending_segments.extend(emit_batch)
                await self._maybe_prompt_unknown_speakers(emit_batch)

            # Analysis / curation / rolling summary triggers
            self._maybe_spawn_analysis()
            if len(self.segments) - self._last_curate_at >= 50 and len(self.insights) > 5:
                self._last_curate_at = len(self.segments)
                self._spawn_task(self._curate_insights())
            if len(self.segments) - self._last_rolling_summary_at >= ROLLING_SUMMARY_INTERVAL:
                # Advance the counter *before* spawning so segments arriving while
                # the summary is in flight don't spawn a duplicate call (§2.6).
                prev_at = self._last_rolling_summary_at
                self._last_rolling_summary_at = len(self.segments)
                self._spawn_task(self._update_rolling_summary(prev_at, self._last_rolling_summary_at))
            # Mid-meeting cluster reconciliation — collapse same-voice fragments
            # so the user sees ~N real speakers instead of 10+ Speaker labels.
            if len(self.segments) - self._last_reconcile_at >= SPEAKER_RECONCILE_INTERVAL:
                self._last_reconcile_at = len(self.segments)
                self._spawn_task(self.reconcile_speaker_clusters())
        except Exception as e:
            logger.error(f"Segment processing error (continuing): {e}", exc_info=True)

    async def _flush_mic_buffer(self):
        """After delay, emit buffered mic segments as confirmed user speech."""
        await asyncio.sleep(bleed_dedup.DEDUP_WINDOW_S)
        if not self._mic_pending:
            return
        confirmed = [self._resolve_segment_speaker(s) for s in self._mic_pending]
        self._mic_pending = []
        logger.info(f"Flushing {len(confirmed)} confirmed mic segment(s)")
        self.segments.extend(confirmed)
        self._last_segment_at = time.monotonic()
        await self.emit({"type": "transcript", "segments": [s.to_dict() for s in confirmed]})
        self._pending_segments.extend(confirmed)
        await self._maybe_prompt_unknown_speakers(confirmed)
        self._maybe_spawn_analysis()

    def _resolve_segment_speaker(self, seg: SrtSegment) -> SrtSegment:
        """Rewrite a segment's speaker through the name registry, with fallback
        to the most recent same-channel speaker for short 'Others' segments.

        - Cluster keys ("Speaker 3") become real names if assigned; otherwise stay as-is.
        - "Others" fallback label (short buffer with no embedding) inherits the
          previous system-audio segment's speaker if that segment is within
          SPEAKER_INHERIT_WINDOW_S. Short utterances are almost always a
          continuation of whoever was just speaking.
        - "You" and other labels pass through unchanged.
        """
        # "Others" is the channel default for system audio when identify() returned None.
        # Inherit the previous non-"You"/non-"Others" speaker if the gap is small.
        if seg.speaker == "Others":
            prior = self._recent_non_you_speaker(seg)
            if prior:
                return SrtSegment(
                    index=seg.index, start=seg.start, end=seg.end,
                    text=seg.text, speaker=prior,
                )
            return seg
        if not _is_cluster_key(seg.speaker):
            return seg
        resolved = self.name_registry.resolve(seg.speaker)
        if resolved == seg.speaker:
            return seg
        return SrtSegment(
            index=seg.index, start=seg.start, end=seg.end,
            text=seg.text, speaker=resolved,
        )

    def _recent_non_you_speaker(self, seg: SrtSegment) -> str | None:
        """Return the most recent system-audio speaker label within the inherit window.

        Walks back through self.segments skipping 'You' and 'Others' segments,
        returning the first real speaker label found if its end time is within
        SPEAKER_INHERIT_WINDOW_S of this segment's start time.
        """
        try:
            seg_start_s = bleed_dedup.ts_to_seconds(seg.start)
        except Exception:
            return None
        for prior in reversed(self.segments):
            if not prior.speaker or prior.speaker in ("You", "Others"):
                continue
            try:
                prior_end_s = bleed_dedup.ts_to_seconds(prior.end)
            except Exception:
                return None
            if seg_start_s - prior_end_s <= SPEAKER_INHERIT_WINDOW_S:
                return prior.speaker
            return None
        return None

    async def _maybe_prompt_unknown_speakers(self, batch: list[SrtSegment]) -> None:
        """Track utterance counts per unresolved cluster; emit a speaker_prompt
        when a cluster crosses the threshold for the first time.
        """
        prompted_now: set[str] = set()
        for seg in batch:
            # Only cluster keys are candidates for prompting.
            if not _is_cluster_key(seg.speaker):
                continue
            cluster = seg.speaker
            self.name_registry.track_utterance(cluster, seg.index, seg.text)
            if cluster in prompted_now:
                continue
            if self.name_registry.should_prompt(cluster, SPEAKER_PROMPT_THRESHOLD):
                self.name_registry.mark_prompted(cluster)
                prompted_now.add(cluster)
                await self.emit({
                    "type": "speaker_prompt",
                    "cluster": cluster,
                    "samples": self.name_registry.samples_for(cluster),
                    "candidates": self.name_registry.candidates(),
                })

    def _maybe_spawn_analysis(self) -> None:
        """Spawn an analysis pass on the pending segments, coalescing calls.

        Trigger (§3.4): fire when pending >= ANALYSIS_MIN_SEGMENTS (default 8), OR
        when pending >= 3 and it's been >= ANALYSIS_MIN_INTERVAL_S (default 45s)
        since the last analysis. Batches whose combined new text is under
        ANALYSIS_MIN_CHARS (greetings, "yeah", backchannels) are dropped without a
        call. This is far less aggressive than the old every-3-segments trigger.

        Coalescing (§2.1) is preserved: only one _analyze runs at a time. While a
        call is in flight, new segments keep accumulating in _pending_segments and
        no second call is queued; when the in-flight call finishes it re-checks.
        """
        if self._analysis_in_flight:
            return
        pending = len(self._pending_segments)
        if pending == 0:
            return
        now = time.monotonic()
        since = now - self._last_analysis_at
        fire = pending >= _analysis_min_segments() or (
            pending >= ANALYSIS_SEGMENT_THRESHOLD and since >= _analysis_min_interval_s()
        )
        if not fire:
            return
        batch = self._pending_segments
        self._pending_segments = []
        # Reset the timer whether or not we make the call, so a run of tiny
        # batches doesn't re-trigger on every segment.
        self._last_analysis_at = now
        # Skip trivially short batches — not worth an LLM round trip. Pending is
        # already cleared (the batch was taken), so these segments are dropped.
        if len(segments_to_text(batch).strip()) < ANALYSIS_MIN_CHARS:
            return
        self._analysis_in_flight = True
        self._spawn_task(self._run_analysis(batch))

    async def _run_analysis(self, batch: list[SrtSegment]) -> None:
        """Run one _analyze call, then re-check for more pending work."""
        try:
            await self._analyze(batch)
        finally:
            self._analysis_in_flight = False
            # Fire again if enough segments accumulated while we were running.
            self._maybe_spawn_analysis()

    async def _analyze(self, segments: list[SrtSegment]):
        new_text = segments_to_text(segments)
        # Recent window moves into the USER message (§3.7); the system prompt is
        # the compact, cacheable analysis-system prompt, not full soul.md.
        recent = segments_to_text(self.segments[-ANALYSIS_WINDOW:]) if self.segments else "(no transcript yet)"
        prompt = PROMPTS.render(
            "analysis",
            recent_transcript=recent,
            new_text=new_text,
            known_terms=self._known_terms_str(),
            no_alert=NO_ALERT,
        )
        try:
            # Fast tier + cacheable compact system prompt + tight output cap (§3.1/§3.2/§3.7).
            response = await self._fast_analyze(
                [LLMService.msg("user", prompt)], self._analysis_system(),
                use_cache=True, max_tokens=512,
            )
            if NO_ALERT in response:
                return
            # Attach the first segment index of the analyzed batch so the UI can
            # jump to (and timestamp) the moment that prompted each insight (§5.2).
            self._parse_insights(response, seg=segments[0].index if segments else None)
        except Exception as e:
            logger.warning(f"Analysis failed: {e}")

    async def _update_rolling_summary(self, start: int, end: int):
        """Fold the newly-arrived segment range into the rolling summary.

        Incremental (§2.6): only the segments in [start, end) since the previous
        update are sent, along with the previous rolling summary, so early
        content isn't lost as the meeting grows (the old behavior re-summarized
        only the last 6000 chars of the pre-window transcript from scratch).
        """
        new_chunk = segments_to_text(self.segments[start:end])
        if not new_chunk.strip():
            return
        prompt = PROMPTS.render(
            "rolling-summary",
            previous=self._rolling_summary or "(none yet)",
            transcript=new_chunk,
        )
        try:
            self._rolling_summary = await self._fast_analyze(
                [LLMService.msg("user", prompt)], max_tokens=512,
            )
            logger.info(f"Rolling summary updated through {end} segments")
        except Exception as e:
            logger.warning(f"Rolling summary failed: {e}")

    def _parse_insights(self, response: str, seg: int | None = None):
        """Parse an analysis JSON response into insights.

        Each insight gains two fields the frontend's live-notes feed relies on
        (§5.2):
        - "priority": "alert" | "note" — the model marks decisions, commitments,
          deadlines, dollar amounts and risks as "alert"; everything else is a
          "note". Defaults to "note" when the model omits it or supplies garbage.
        - "seg": the transcript index of the first segment in the analyzed batch,
          so the UI can jump to (and timestamp) the moment that prompted it. The
          model does not supply this — the engine knows the batch it sent. Omitted
          for insights mined from post-meeting chat (no batch).
        """
        try:
            start = response.find("[")
            end = response.rfind("]") + 1
            if start >= 0 and end > start:
                items = json.loads(response[start:end])
                for item in items:
                    category = item.get("category", "key_point")
                    text = item.get("text", "")
                    label = item.get("label", "")
                    priority = "alert" if item.get("priority") == "alert" else "note"
                    # Skip duplicate terms
                    if category == "term" and label:
                        if label.lower() in {t.lower() for t in self.explained_terms}:
                            continue
                        self.explained_terms.add(label)
                    iid = self._next_insight_id
                    self._next_insight_id += 1
                    insight = {"id": iid, "category": category, "text": text,
                               "priority": priority}
                    if label:
                        insight["label"] = label
                    if seg is not None:
                        insight["seg"] = seg
                    self.insights.append(insight)
                    self._spawn_task(self.emit({"type": "insight", **insight}))
                return
        except (json.JSONDecodeError, KeyError):
            pass
        iid = self._next_insight_id
        self._next_insight_id += 1
        insight = {"id": iid, "category": "key_point", "text": response.strip(),
                   "priority": "note"}
        if seg is not None:
            insight["seg"] = seg
        self.insights.append(insight)
        self._spawn_task(self.emit({"type": "insight", **insight}))

    async def _curate_insights(self):
        """Periodically prune stale/redundant insights."""
        if not self.insights:
            return
        recent = segments_to_text(self.segments[-100:])
        listing = "\n".join(f"{i['id']}: {i['category']} — {i['text'][:120]}" for i in self.insights)
        prompt = PROMPTS.render("curation", recent_transcript=recent[:3000], insights_list=listing)
        try:
            result = await self._fast_analyze(
                [LLMService.msg("user", prompt)], max_tokens=512,
            )
            start = result.find("[")
            end = result.rfind("]") + 1
            if start >= 0 and end > start:
                ids_to_remove = set(json.loads(result[start:end]))
                if not ids_to_remove:
                    return
                self.insights = [i for i in self.insights if i["id"] not in ids_to_remove]
                for rid in ids_to_remove:
                    await self.emit({"type": "insight_remove", "id": rid})
                logger.info(f"Curated {len(ids_to_remove)} insights")
        except Exception as e:
            logger.debug(f"Curation failed: {e}")

    async def _generate_summary(self):
        if not self.transcript.strip():
            return
        # The summary stays on the primary (frontier) model — it's the meeting's
        # highest-value output (§3.1). Default: a single summary from the terse
        # org-grouped prompt (summary-b), no variant header. Set SUMMARY_AB_TEST to
        # run both the narrative (summary-a) and terse (summary-b) variants
        # side-by-side under headers (§3.3).
        kwargs = dict(
            transcript=self.transcript,
            user_profile=self.user_profile,
        )
        parts: list[str] = []
        if _env_bool("SUMMARY_AB_TEST", False):
            variants = (("summary-a", "Variant A — narrative"),
                        ("summary-b", "Variant B — terse"))
        else:
            variants = (("summary-b", None),)
        for variant_name, variant_label in variants:
            if variant_label is not None:
                header = f"\n\n# {variant_label}\n\n"
                parts.append(header)
                await self.emit({"type": "summary_chunk", "text": header})
            try:
                prompt = PROMPTS.render(variant_name, **kwargs)
                async for chunk in self.llm.stream(
                    [LLMService.msg("user", prompt)], max_tokens=8192,
                ):
                    await self.emit({"type": "summary_chunk", "text": chunk})
                    parts.append(chunk)
            except Exception as e:
                label = variant_label or "summary"
                err = f"Error generating {label}: {e}\n"
                await self.emit({"type": "summary_chunk", "text": err})
                parts.append(err)
        self.summary = "".join(parts).strip()
        await self.emit({"type": "summary_done"})

    def _save_session(self):
        if not self.segments:
            return
        if not self.session_id:
            self.session_id = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        data = {
            "id": self.session_id,
            "srt_path": self.srt_path,
            "started_at": self.started_at,
            "ended_at": datetime.now().isoformat(),
            "segment_count": len(self.segments),
            "segments": [s.to_dict() for s in self.segments],
            "insights": self.insights,
            "explained_terms": sorted(self.explained_terms),
            "chat_history": self.chat_history,
            "summary": self.summary,
            "meeting_context": self.meeting_context.to_dict(),
        }
        save_session(data)


