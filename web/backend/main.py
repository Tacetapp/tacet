"""FastAPI WebSocket server for meeting assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

# On Intel Macs the faster-whisper backend (CTranslate2) and torch (silero-vad,
# speechbrain) each bundle their own Intel OpenMP runtime; loading both aborts
# with "libiomp5.dylib already initialized". Allow the duplicate runtime before
# either library is imported. No-op on Apple Silicon. Override by exporting the
# var yourself.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# Beyond the abort, the two OpenMP copies segfault if torch initialises first and
# CTranslate2 second. Importing ctranslate2 up front (before any torch-loading
# module below) makes the coexistence stable. Absent on Apple Silicon, where
# faster-whisper isn't installed — the import simply no-ops there.
try:
    import ctranslate2  # noqa: F401
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from web.backend.audio_capture import AudioCaptureSource
from web.backend.auth_check import check_auth
from web.backend.bedrock_service import VISION_MODEL_ID
from web.backend.llm_service import create_llm_service, LLMService
from web.backend.engine import MeetingEngine
from web.backend.screen_analyzer import ScreenAnalyzer
from web.backend.session_store import list_sessions, search_sessions
from web.backend.speaker_embedder import SpeakerEmbedder
from web.backend.transcriber import ChannelActivityTracker, WhisperTranscriber
from web.backend.voice_profile import VoiceProfile

# Configure logging: always to stdout; optionally to a rotating file when LOG_FILE is set.
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
_log_file = os.environ.get("LOG_FILE", "").strip()
if _log_file:
    _log_handlers.append(
        RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
    )
logging.basicConfig(level=logging.INFO, handlers=_log_handlers)
logger = logging.getLogger(__name__)

# WS authentication token — if set, every /ws connection must present it (§4.1).
WS_AUTH_TOKEN = os.environ.get("WS_AUTH_TOKEN", "").strip()

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the background maintenance loops for the life of the app (§6).

    Replaces the deprecated ``@app.on_event("startup")`` handler with a FastAPI
    lifespan context manager. Both loops sleep on long intervals and are
    cancelled on shutdown.
    """
    async def _cleanup_loop():
        while True:
            await asyncio.sleep(300)  # check every 5 minutes
            await _evict_idle_clients()

    async def _auth_check_loop():
        while True:
            await asyncio.sleep(AUTH_CHECK_INTERVAL)
            await _broadcast_auth_status()

    tasks = [asyncio.create_task(_cleanup_loop()), asyncio.create_task(_auth_check_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="Tacet", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    # Any localhost/127.0.0.1 port (the server port is chosen dynamically by the
    # Electron shell, and the Vite dev server runs on its own port).
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Server-side session registry ─────────────────────
# One ClientSession per client_id, in `_sessions`. Consolidates what used to be a
# dozen parallel `dict[str, …]` registries into a single object with one
# lifecycle (§6) — the parallel dicts were the root cause of lifecycle bugs
# (§2.2/§2.3) because they could drift out of sync. Sessions (and the engines
# they hold) persist across WebSocket reconnects.
@dataclass
class ClientSession:
    """All per-client server state, keyed by client_id in `_sessions`."""
    client_id: str
    engine: MeetingEngine | None = None
    # Primary (frontier) LLM tier — chat + summary.
    llm: LLMService | None = None
    # Cheap background tier (Haiku-class) — analysis/curation/rolling/ingest (§3.1).
    llm_fast: LLMService | None = None
    capture: AudioCaptureSource | None = None
    # (system_transcriber, mic_transcriber) tuple, or None when not capturing.
    transcribers: tuple[WhisperTranscriber, WhisperTranscriber] | None = None
    screen_analyzer: ScreenAnalyzer | None = None
    # Capture runtime reused across pause/resume: speaker embedder, whisper GPU
    # lock, and activity tracker so diarization/serialization survive a cycle.
    capture_runtime: dict | None = None
    # WebSocket tabs currently connected for this client (used by the WS-close
    # grace timer to know whether any tab is still attached).
    sockets: set[WebSocket] = field(default_factory=set)
    last_activity: float = 0.0  # monotonic timestamp
    # Background watchers.
    autostop_task: asyncio.Task | None = None
    wsclose_grace_task: asyncio.Task | None = None
    pending_start: dict | None = None

    def touch(self) -> None:
        """Update the last-activity timestamp."""
        self.last_activity = time.monotonic()

    def cancel_watchers(self) -> None:
        """Cancel the auto-stop + WS-close grace tasks if running."""
        for task in (self.autostop_task, self.wsclose_grace_task):
            if task is not None and not task.done():
                task.cancel()
        self.autostop_task = None
        self.wsclose_grace_task = None


_sessions: dict[str, ClientSession] = {}
# Every accepted WebSocket, regardless of client_id (used for broadcasts).
_active_websockets: set[WebSocket] = set()


def _get_session(client_id: str) -> ClientSession:
    """Return the ClientSession for `client_id`, creating it on first use."""
    sess = _sessions.get(client_id)
    if sess is None:
        sess = ClientSession(client_id=client_id)
        _sessions[client_id] = sess
    return sess


CLIENT_TTL_SECONDS = 3600  # evict idle clients after 1 hour
AUTH_CHECK_INTERVAL = 180  # check auth every 3 minutes

# Singleton voice profile for the process. Loaded lazily (first save or first
# runtime lookup) so server startup doesn't pay for ECAPA init if unused.
_voice_profile: VoiceProfile | None = None
_voice_profile_lock = asyncio.Lock()


async def _get_voice_profile() -> VoiceProfile:
    """Return the shared VoiceProfile, constructing it on first use."""
    global _voice_profile
    if _voice_profile is not None:
        return _voice_profile
    async with _voice_profile_lock:
        if _voice_profile is None:
            # Use a dedicated SpeakerEmbedder so the voice profile's ECAPA
            # model is independent of any meeting's diarization state.
            embedder = SpeakerEmbedder()
            _voice_profile = VoiceProfile(embedder)
        return _voice_profile

# Auto-stop thresholds (env-var tunable)
AUTOSTOP_WS_GRACE_S     = int(os.environ.get("AUTOSTOP_WS_GRACE_S", "60"))
AUTOSTOP_INACTIVITY_MIN = int(os.environ.get("AUTOSTOP_INACTIVITY_MIN", "10"))
AUTOSTOP_UNDO_WINDOW_S  = int(os.environ.get("AUTOSTOP_UNDO_WINDOW_S", "30"))
AUTOSTOP_POLL_S         = 15   # how often the watcher checks conditions


async def _evict_idle_clients():
    """Remove clients idle for longer than CLIENT_TTL_SECONDS.

    Never evicts a live or paused meeting (§2.3). Any unsaved segments on an
    evicted engine are persisted first so an idle-but-stopped meeting isn't lost.
    """
    now = time.monotonic()
    to_evict = [
        cid for cid, sess in _sessions.items()
        if sess.last_activity and now - sess.last_activity > CLIENT_TTL_SECONDS
    ]
    for cid in to_evict:
        sess = _sessions.get(cid)
        if sess is None:
            continue
        engine = sess.engine
        # Skip clients whose engine is still live or paused — those are real
        # meetings, not idle leftovers.
        if engine is not None and (engine.active or engine.paused):
            continue
        # Persist any unsaved segments before dropping the session reference.
        if engine is not None and engine.segments:
            try:
                await asyncio.to_thread(engine._save_session)
            except Exception as e:
                logger.warning(f"Failed to persist session during eviction of {cid}: {e}")
        if sess.capture is not None:
            asyncio.create_task(sess.capture.stop())
        sess.cancel_watchers()
        _sessions.pop(cid, None)
        logger.info(f"Evicted idle client: {cid}")


def _get_or_create_llm(session: ClientSession) -> LLMService:
    if session.llm is None:
        session.llm = create_llm_service()
    return session.llm


def _get_or_create_llm_fast(session: ClientSession) -> LLMService:
    """Cheap background-tier service (Haiku-class for Bedrock) — §3.1."""
    if session.llm_fast is None:
        session.llm_fast = create_llm_service(fast=True)
    return session.llm_fast


async def _broadcast_auth_status():
    """Send auth status to all connected WebSocket clients."""
    status = await asyncio.to_thread(check_auth)
    msg = {"type": "auth_status", **status.to_dict()}
    for ws in list(_active_websockets):
        try:
            await ws.send_json(msg)
        except Exception:
            pass


# ── Background task helpers (keep the read loop free) ─────

async def _handle_start(session: ClientSession, emit):
    """Launch capture + transcription in the background."""
    try:
        # Defensively reap any stale capture for this client before building a
        # new one, so a double-start can't orphan the previous capture-helper (§2.2).
        await _teardown_capture(session)

        # Speaker diarization: voice-based embedding on CPU (parallel with whisper on Metal GPU)
        speaker_embedder = SpeakerEmbedder()
        # Persistent speaker registry (opt-in, §7): preload known people as named
        # clusters so identify() labels them from their first utterance. No-op
        # unless SPEAKER_REGISTRY_ENABLED; a registry failure must not block start.
        try:
            from web.backend import speaker_registry
            known = await asyncio.to_thread(speaker_registry.load_centroids)
            if known:
                speaker_embedder.seed_known_speakers(known)
                logger.info(f"Seeded speaker registry: {len(known)} known voice(s)")
        except Exception as e:
            logger.warning(f"Speaker registry seeding failed (continuing): {e}")
        # Pre-load the model in background so first audio segment isn't delayed
        asyncio.create_task(asyncio.to_thread(speaker_embedder._ensure_model))

        # Create engine with speaker_embedder so it can rename/merge clusters on
        # user confirmation, plus a cheap fast tier for background analysis (§3.1).
        engine = MeetingEngine(
            llm=_get_or_create_llm(session), emit=emit,
            speaker_embedder=speaker_embedder,
            llm_fast=_get_or_create_llm_fast(session),
        )
        session.engine = engine
        await engine.start()
        session.pending_start = None

        # Per-client capture runtime so pause/resume can reuse the same
        # embedder, whisper GPU lock, and activity tracker.
        session.capture_runtime = {
            "speaker_embedder": speaker_embedder,
            "whisper_lock": asyncio.Lock(),
            "activity_tracker": ChannelActivityTracker(),
        }

        await _start_capture(session, engine, emit)

        # Kick off the auto-stop watcher (inactivity check).
        if session.autostop_task is not None and not session.autostop_task.done():
            session.autostop_task.cancel()
        session.autostop_task = asyncio.create_task(_autostop_watcher(session, emit))

    except Exception as e:
        logger.error(f"Error in _handle_start: {e}", exc_info=True)
        await emit({"type": "error", "text": f"Failed to start: {e}"})


async def _start_capture(session: ClientSession, engine: MeetingEngine, emit) -> None:
    """Build transcribers + capture for this client and start them.

    Reuses the speaker embedder, whisper lock, and activity tracker from
    `session.capture_runtime` so pause→resume preserves diarization state and
    GPU serialization across cycles.
    """
    runtime = session.capture_runtime
    if runtime is None:
        runtime = {
            "speaker_embedder": SpeakerEmbedder(),
            "whisper_lock": asyncio.Lock(),
            "activity_tracker": ChannelActivityTracker(),
        }
        session.capture_runtime = runtime

    speaker_embedder = runtime["speaker_embedder"]
    whisper_lock = runtime["whisper_lock"]
    activity_tracker = runtime["activity_tracker"]

    # Screen analyzer — optional, and only when the active provider can do
    # vision. Disabled, or a text-only provider (e.g. Venice), skips it so screen
    # frames are never sent to a model that can't read them.
    from web.backend.engine import PROMPTS
    screen_enabled = os.environ.get("SCREEN_ANALYSIS_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")
    vision_service = create_llm_service(model_id=VISION_MODEL_ID)
    screen_analyzer = None
    if screen_enabled and getattr(vision_service, "supports_vision", False):
        screen_analyzer = ScreenAnalyzer(
            llm=vision_service, prompts=PROMPTS, emit=emit,
            is_paused=lambda: engine.paused,
        )
        session.screen_analyzer = screen_analyzer
    else:
        reason = "disabled in settings" if not screen_enabled \
            else f"provider has no vision ({type(vision_service).__name__})"
        logger.info(f"Screen analysis off ({reason})")

    system_transcriber = WhisperTranscriber(
        on_segments=engine.on_new_segments, channel="Others",
        transcription_lock=whisper_lock,
        speaker_embedder=speaker_embedder,
        activity_tracker=activity_tracker,
        prompt_provider=engine.current_initial_prompt,
    )
    # Pass the process-wide voice profile to the mic transcriber. It may or
    # may not be active (is_active() returns False if the user hasn't opted in)
    # — the transcriber short-circuits cheaply in that case.
    voice_profile = await _get_voice_profile()
    mic_transcriber = WhisperTranscriber(
        on_segments=engine.on_new_segments, channel="You",
        transcription_lock=whisper_lock,
        activity_tracker=activity_tracker,
        is_mic=True,
        voice_profile=voice_profile,
        prompt_provider=engine.current_initial_prompt,
    )
    session.transcribers = (system_transcriber, mic_transcriber)

    capture = AudioCaptureSource()
    capture.on_audio = system_transcriber.feed_audio
    capture.on_mic_audio = mic_transcriber.feed_audio
    if screen_analyzer is not None:
        capture.on_frame = screen_analyzer.on_frame
    session.capture = capture

    async def on_capture_status(status: dict):
        status_type = status.get("type", "")
        if status_type == "apps_available":
            await emit({"type": "capture_apps", "apps": status.get("apps", [])})
        elif status_type == "started":
            await emit({"type": "capture_status", "capturing": True,
                        "system_audio": status.get("system_audio", True),
                        "mic": status.get("mic", True)})
        elif status_type == "stopped":
            await emit({"type": "capture_status", "capturing": False})
        elif status_type == "error":
            msg = status.get("message", "Unknown error")
            await emit({"type": "error", "text": f"Capture: {msg}"})
            # If the capture pipeline died mid-meeting (ScreenCaptureKit lost its
            # target, stream interrupted, etc.), there's no way to recover without
            # a user re-pick. Auto-stop the engine cleanly with an explicit reason
            # so the inactivity watcher doesn't misfire 10 min later and so the
            # user immediately sees why the session ended.
            eng = session.engine
            if eng is not None and eng.active and not eng.paused:
                logger.warning(f"Capture error — triggering auto-stop: {msg}")
                asyncio.create_task(_handle_stop(
                    session, eng, emit, auto_reason="capture_error"
                ))
        else:
            logger.debug(f"Capture status: {status}")

    capture.on_status = on_capture_status

    logger.info("Starting audio/screen capture...")
    await capture.start(screen_interval=20.0)
    logger.info("Capture started")


async def _teardown_capture(session: ClientSession) -> None:
    """Stop capture subprocess + transcribers without destroying the engine."""
    capture = session.capture
    session.capture = None
    if capture is not None:
        try:
            await capture.stop()
        except Exception as e:
            logger.warning(f"Capture stop error (continuing): {e}")
    transcribers = session.transcribers
    session.transcribers = None
    if transcribers is not None:
        system_t, mic_t = transcribers
        try:
            await system_t.flush()
            await mic_t.flush()
        except Exception as e:
            logger.warning(f"Transcriber flush error (continuing): {e}")
        system_t.stop()
        mic_t.stop()
    session.screen_analyzer = None


async def _run_ingest(session_id: str, provider, emit, *, force: bool = False) -> None:
    """Background worker: fold a finished meeting into the knowledge vault.

    Never raises into the task; surfaces the outcome via an `ingest_done` WS
    message (and ingest itself logs failures to knowledge/log.md).
    """
    try:
        from web.backend import ingest as ingest_mod
        result = await ingest_mod.ingest_session(session_id, provider, force=force)
        await emit({
            "type": "ingest_done",
            "session_id": session_id,
            "ok": bool(result.get("ok")),
            "project_slug": result.get("project_slug", ""),
            "skipped": bool(result.get("skipped")),
            "error": result.get("error", ""),
        })
    except Exception as e:
        logger.error(f"_run_ingest failed: {e}", exc_info=True)
        try:
            await emit({"type": "ingest_done", "session_id": session_id,
                        "ok": False, "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass


async def _run_sync(provider, emit, *, deep: bool = False) -> None:
    """Background worker: run a knowledge-vault sync, streaming progress over WS.

    Tier A (index/backlinks/broken-links) always runs; Tier B (LLM dedup + brief
    refresh) runs only when `deep` and a provider is supplied. Never raises.
    """
    async def _progress(step: str):
        await emit({"type": "sync_progress", "step": step})
    try:
        from web.backend import knowledge_sync
        report = await knowledge_sync.sync_vault(provider, deep=deep, progress=_progress)
        await emit({"type": "sync_done", **report})
    except Exception as e:
        logger.error(f"_run_sync failed: {e}", exc_info=True)
        try:
            await emit({"type": "sync_done", "ok": False, "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass


async def _handle_stop(session: ClientSession, engine: MeetingEngine, emit,
                         *, auto_reason: str | None = None):
    """Stop capture, transcribe remaining audio, then stop engine (summary generation).

    If `auto_reason` is set, the engine trims trailing silence before the
    summary prompt and the frontend is told why the stop happened.
    """
    try:
        # Cancel watcher tasks so they don't re-fire during stop.
        session.cancel_watchers()
        session.capture_runtime = None

        await _teardown_capture(session)

        # Consolidate fragmented diarization clusters before prompting the user
        # so the batch UI shows merged speakers, not 40 spurious fragments.
        await engine.reconcile_speaker_clusters()

        # Emit speaker batch prompt (deferred + unresolved clusters) before summary
        # so the user can name speakers and the summary reflects real names.
        await engine.emit_speaker_batch_prompt()

        # Give the user a brief window to name any remaining clusters before the
        # summary is generated, so it uses real names instead of "Speaker N" (§2.7).
        # Only wait when a client is connected to respond and clusters are unresolved.
        if session.sockets and engine.unresolved_speaker_clusters():
            await emit({"type": "info", "text": "Waiting briefly for speaker names…"})
            for _ in range(15):  # up to ~15s, ending early once all are resolved
                await asyncio.sleep(1)
                if not engine.unresolved_speaker_clusters():
                    break

        if auto_reason:
            engine._trim_on_summary = True
            await emit({"type": "auto_stop_fired", "reason": auto_reason})

        # Now stop engine (summary + save)
        await engine.stop()

        # Fold the finished meeting into the knowledge vault (non-blocking).
        # Ingest is extraction — run it on the cheap fast tier (§3.1).
        ingest_provider = getattr(engine, "llm_fast", None) or getattr(engine, "llm", None)
        if engine.session_id and ingest_provider is not None:
            asyncio.create_task(_run_ingest(engine.session_id, ingest_provider, emit))
    except Exception as e:
        logger.error(f"Error in _handle_stop: {e}", exc_info=True)
        await emit({"type": "error", "text": f"Failed to stop: {e}"})


async def _handle_pause(session: ClientSession, engine: MeetingEngine, emit) -> None:
    """Stop capture without ending the session; engine stays alive + paused."""
    try:
        await _teardown_capture(session)
        await engine.pause()
    except Exception as e:
        logger.error(f"Error in _handle_pause: {e}", exc_info=True)
        await emit({"type": "error", "text": f"Failed to pause: {e}"})


async def _handle_resume(session: ClientSession, engine: MeetingEngine, emit) -> None:
    """Relaunch capture and re-wire it to the existing engine."""
    try:
        await _start_capture(session, engine, emit)
        await engine.resume()
    except Exception as e:
        logger.error(f"Error in _handle_resume: {e}", exc_info=True)
        await emit({"type": "error", "text": f"Failed to resume: {e}"})


async def _handle_voice_profile_save(data: dict, emit) -> None:
    """Decode the base64 PCM sample, embed it, persist. Reports back on emit."""
    try:
        import base64
        raw = data.get("audio")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Missing audio payload")
        sample_rate = int(data.get("sample_rate", 16000))
        import numpy as np
        pcm_bytes = base64.b64decode(raw)
        # Frontend sends little-endian float32 PCM, 16kHz mono.
        audio = np.frombuffer(pcm_bytes, dtype=np.float32)
        if audio.size == 0:
            raise ValueError("Empty audio payload")
        vp = await _get_voice_profile()
        # Embedding extraction is CPU-bound (ECAPA); keep it off the loop.
        meta = await asyncio.to_thread(vp.embed_and_save, audio, sample_rate)
        await emit({"type": "voice_profile", **meta})
    except ValueError as e:
        await emit({"type": "voice_profile_error", "text": str(e)})
    except Exception as e:
        logger.error(f"voice_profile_save failed: {e}", exc_info=True)
        await emit({"type": "voice_profile_error", "text": f"Save failed: {e}"})


# ── Auto-stop watcher ──────────────────────────────────

async def _autostop_watcher(session: ClientSession, emit) -> None:
    """Periodically evaluate auto-stop triggers for this client.

    Fires once the inactivity condition is satisfied.
    Emits `auto_stop_pending` first to give the UI a chance to show an undo
    banner, then (after AUTOSTOP_UNDO_WINDOW_S) executes _handle_stop.
    """
    try:
        while True:
            await asyncio.sleep(AUTOSTOP_POLL_S)
            engine = session.engine
            if engine is None or not engine.active or engine.paused:
                continue

            reason = _evaluate_autostop(engine)
            if reason is None:
                continue

            # Signal the UI and wait out the undo window. If the user resumes
            # or the engine stops for another reason, we just bail out.
            await emit({
                "type": "auto_stop_pending",
                "reason": reason,
                "seconds_until": AUTOSTOP_UNDO_WINDOW_S,
            })
            logger.info(f"Auto-stop pending ({reason}); undo window {AUTOSTOP_UNDO_WINDOW_S}s")
            await asyncio.sleep(AUTOSTOP_UNDO_WINDOW_S)

            # Re-check: the user may have paused, resumed silence may have ended,
            # or someone may have manually stopped in the meantime.
            engine = session.engine
            if engine is None or not engine.active:
                return
            if engine.paused:
                await emit({"type": "auto_stop_cancelled", "reason": "paused"})
                continue
            if _evaluate_autostop(engine) is None:
                await emit({"type": "auto_stop_cancelled", "reason": "activity"})
                continue

            await _handle_stop(session, engine, emit, auto_reason=reason)
            return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Auto-stop watcher error for {session.client_id}: {e}", exc_info=True)


def _evaluate_autostop(engine: MeetingEngine) -> str | None:
    """Return a reason string if an auto-stop trigger fires now, else None."""
    age_s = engine.last_segment_age_s()
    if age_s >= AUTOSTOP_INACTIVITY_MIN * 60:
        return "inactivity"
    return None


async def _schedule_wsclose_grace(session: ClientSession, emit) -> None:
    """After the last WS for this client closes, wait GRACE seconds. If no
    reconnect and the engine is still active + not paused, auto-stop.
    """
    try:
        await asyncio.sleep(AUTOSTOP_WS_GRACE_S)
        # Bail if a browser reconnected during the grace window.
        if session.sockets:
            return
        engine = session.engine
        if engine is None or not engine.active or engine.paused:
            return
        await _handle_stop(session, engine, emit, auto_reason="websocket_closed")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"WS-close grace task error for {session.client_id}: {e}", exc_info=True)


# WebSocket origins must be local. Accept any localhost/127.0.0.1 port (the
# server port is dynamic under Electron) plus Electron's own non-HTTP origins
# (file://, null/empty when loaded from a local file).
_WS_ORIGIN_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")


def _ws_origin_allowed(origin: str) -> bool:
    if not origin:
        return True  # Electron / same-process clients send no Origin header
    if origin in ("null", "file://"):
        return True
    return bool(_WS_ORIGIN_RE.match(origin))


# ── WebSocket message handlers (dispatch table) ─────────
# Each handler is `async (session, data, emit)`; the dispatch dict maps a
# message `type` to its handler (§6, replacing the old ~250-line if/elif chain).
# Handlers preserve the original per-message semantics exactly, including which
# run as background tasks (asyncio.create_task) vs inline.

async def _msg_start(session: ClientSession, data: dict, emit) -> None:
    # Refuse a second start while a meeting is already live/paused so we don't
    # orphan the running capture or lose its segments (§2.2).
    engine = session.engine
    if engine is not None and (engine.active or engine.paused):
        await emit({"type": "error", "text": "Meeting already active"})
    else:
        session.pending_start = {}
        asyncio.create_task(_handle_start(session, emit))


async def _msg_stop(session: ClientSession, data: dict, emit) -> None:
    asyncio.create_task(_handle_stop(session, session.engine, emit))


async def _msg_reingest_session(session: ClientSession, data: dict, emit) -> None:
    sid = data.get("session_id", "")
    if not sid:
        await emit({"type": "error", "text": "reingest_session: session_id required"})
        return
    # Ingest runs on the cheap fast tier (§3.1).
    engine = session.engine
    provider = (engine.llm_fast if engine and getattr(engine, "llm_fast", None)
                else _get_or_create_llm_fast(session))
    asyncio.create_task(_run_ingest(sid, provider, emit, force=True))


async def _msg_sync_knowledge(session: ClientSession, data: dict, emit) -> None:
    deep = bool(data.get("deep"))
    # Tier A needs no LLM; only build a provider for a deep sync.
    provider = None
    if deep:
        engine = session.engine
        provider = (engine.llm if engine and getattr(engine, "llm", None)
                    else _get_or_create_llm(session))
    asyncio.create_task(_run_sync(provider, emit, deep=deep))


async def _msg_pause(session: ClientSession, data: dict, emit) -> None:
    asyncio.create_task(_handle_pause(session, session.engine, emit))


async def _msg_resume(session: ClientSession, data: dict, emit) -> None:
    asyncio.create_task(_handle_resume(session, session.engine, emit))


async def _msg_auto_stop_undo(session: ClientSession, data: dict, emit) -> None:
    # Cancel any pending auto-stop for this client. The watcher task is still
    # sleeping in its undo window; killing it stops the scheduled _handle_stop.
    # We then relaunch the watcher so a new silence condition can fire later.
    if session.autostop_task is not None and not session.autostop_task.done():
        session.autostop_task.cancel()
    # Reset last-activity so the inactivity check doesn't re-fire immediately.
    session.engine._last_segment_at = time.monotonic()
    session.autostop_task = asyncio.create_task(_autostop_watcher(session, emit))
    await emit({"type": "auto_stop_cancelled", "reason": "undo"})


async def _msg_voice_profile_status(session: ClientSession, data: dict, emit) -> None:
    vp = await _get_voice_profile()
    await emit({"type": "voice_profile", **vp.metadata()})


async def _msg_voice_profile_save(session: ClientSession, data: dict, emit) -> None:
    await _handle_voice_profile_save(data, emit)


async def _msg_voice_profile_delete(session: ClientSession, data: dict, emit) -> None:
    vp = await _get_voice_profile()
    vp.delete()
    await emit({"type": "voice_profile", **vp.metadata()})


async def _emit_speaker_registry(emit) -> None:
    """Emit the persistent speaker-registry status (§7).

    The payload only ever carries names/metadata — never centroids — and reports
    ``enabled: False`` with an empty list when the opt-in gate
    (SPEAKER_REGISTRY_ENABLED) is off, which the frontend uses to hide the UI
    entirely. status() itself no-ops (returns disabled) when the gate is off.
    """
    from web.backend import speaker_registry
    status = await asyncio.to_thread(speaker_registry.status)
    await emit({"type": "speaker_registry", **status})


async def _msg_speaker_registry_status(session: ClientSession, data: dict, emit) -> None:
    await _emit_speaker_registry(emit)


async def _msg_speaker_registry_delete(session: ClientSession, data: dict, emit) -> None:
    # Forget one named voice, then re-emit the updated status. delete() no-ops
    # when the registry is disabled, so this is safe regardless of the gate.
    from web.backend import speaker_registry
    name = (data.get("name") or "").strip()
    if name:
        await asyncio.to_thread(speaker_registry.delete, name)
    await _emit_speaker_registry(emit)


async def _msg_speaker_registry_clear(session: ClientSession, data: dict, emit) -> None:
    # Forget every named voice, then re-emit. clear() no-ops when disabled.
    from web.backend import speaker_registry
    await asyncio.to_thread(speaker_registry.clear)
    await _emit_speaker_registry(emit)


async def _msg_set_capture_target(session: ClientSession, data: dict, emit) -> None:
    # User manually selects a capture target app
    capture = session.capture
    if capture and capture.is_running:
        # Restart capture with new target
        await capture.stop()
        bundle_id = data.get("bundle_id")
        await capture.start(screen_app_bundle=bundle_id if bundle_id != "display" else None,
                            screen_interval=20.0)


async def _msg_new_meeting(session: ClientSession, data: dict, emit) -> None:
    # Cancel any running watcher/grace tasks for this client.
    session.cancel_watchers()
    await _teardown_capture(session)
    session.capture_runtime = None
    engine = session.engine
    if engine:
        if engine.active:
            engine.active = False
        engine.reset()
    session.engine = None
    await emit({"type": "status", "meeting_active": False, "paused": False,
                "chat_ready": False, "segment_count": 0})
    await emit({"type": "reset"})


async def _msg_chat(session: ClientSession, data: dict, emit) -> None:
    text = data.get("text", "").strip()
    images = data.get("images")  # [{format, data}] or None
    engine = session.engine
    if not text and not images:
        pass
    elif not engine:
        logger.warning(f"Chat received but no engine for client {session.client_id}")
        await emit({"type": "chat_chunk", "text": "No active session. Please start a new meeting or load a previous session."})
        await emit({"type": "chat_done"})
    elif not engine.chat_ready:
        logger.warning("Chat received but engine not ready yet")
        await emit({"type": "chat_chunk", "text": "Still initializing -- please wait a moment and try again."})
        await emit({"type": "chat_done"})
    else:
        asyncio.create_task(engine.chat(text, images=images))


async def _msg_list_sessions(session: ClientSession, data: dict, emit) -> None:
    sessions = await asyncio.to_thread(list_sessions)
    await emit({"type": "sessions", "sessions": sessions})


async def _msg_search_sessions(session: ClientSession, data: dict, emit) -> None:
    query = data.get("query", "")
    results = await asyncio.to_thread(search_sessions, query) if query.strip() \
        else await asyncio.to_thread(list_sessions)
    await emit({"type": "sessions", "sessions": results})


async def _msg_check_auth(session: ClientSession, data: dict, emit) -> None:
    auth_status = await asyncio.to_thread(check_auth)
    await emit({"type": "auth_status", **auth_status.to_dict()})


async def _msg_confirm_speaker(session: ClientSession, data: dict, emit) -> None:
    cluster = (data.get("cluster") or "").strip()
    name = (data.get("name") or "").strip()
    if cluster and name:
        await session.engine.confirm_speaker(cluster, name)


async def _msg_skip_speaker(session: ClientSession, data: dict, emit) -> None:
    cluster = (data.get("cluster") or "").strip()
    if cluster:
        await session.engine.skip_speaker(cluster)


async def _msg_defer_speaker(session: ClientSession, data: dict, emit) -> None:
    cluster = (data.get("cluster") or "").strip()
    if cluster:
        await session.engine.defer_speaker(cluster)


async def _msg_skip_all_speakers(session: ClientSession, data: dict, emit) -> None:
    await session.engine.set_skip_all_speakers(bool(data.get("on", True)))


async def _msg_load_session(session: ClientSession, data: dict, emit) -> None:
    session_id = data.get("session_id", "")
    engine = MeetingEngine(
        llm=_get_or_create_llm(session), emit=emit,
        llm_fast=_get_or_create_llm_fast(session),
    )
    session.engine = engine
    session_data = await asyncio.to_thread(engine.load_session, session_id)
    if session_data:
        await emit({
            "type": "session_loaded",
            "segments": [s.to_dict() for s in engine.segments],
            "insights": engine.insights,
            "summary": engine.summary,
            "chat_history": engine.chat_history,
            "started_at": session_data.get("started_at", ""),
            "ended_at": session_data.get("ended_at", ""),
            "meeting_title": engine.meeting_context.title,
        })
        await emit({"type": "status", "meeting_active": False, "chat_ready": True,
                    "segment_count": len(engine.segments)})
        # Review-mode: seed registry from saved labels and prompt for any remaining Speaker N clusters
        engine.seed_registry_from_loaded_segments()
        await engine.emit_speaker_batch_prompt()
    else:
        session.engine = None
        await emit({"type": "error", "text": f"Session {session_id} not found"})


# msg_type → handler. `ping` is handled inline before dispatch.
_DISPATCH = {
    "start": _msg_start,
    "stop": _msg_stop,
    "reingest_session": _msg_reingest_session,
    "sync_knowledge": _msg_sync_knowledge,
    "pause": _msg_pause,
    "resume": _msg_resume,
    "auto_stop_undo": _msg_auto_stop_undo,
    "voice_profile_status": _msg_voice_profile_status,
    "voice_profile_save": _msg_voice_profile_save,
    "voice_profile_delete": _msg_voice_profile_delete,
    "speaker_registry_status": _msg_speaker_registry_status,
    "speaker_registry_delete": _msg_speaker_registry_delete,
    "speaker_registry_clear": _msg_speaker_registry_clear,
    "set_capture_target": _msg_set_capture_target,
    "new_meeting": _msg_new_meeting,
    "chat": _msg_chat,
    "list_sessions": _msg_list_sessions,
    "search_sessions": _msg_search_sessions,
    "check_auth": _msg_check_auth,
    "confirm_speaker": _msg_confirm_speaker,
    "skip_speaker": _msg_skip_speaker,
    "defer_speaker": _msg_defer_speaker,
    "skip_all_speakers": _msg_skip_all_speakers,
    "load_session": _msg_load_session,
}

# Handlers that require a live engine; skipped silently when none exists, exactly
# like the original `elif msg_type == ... and engine:` guards.
_ENGINE_REQUIRED = frozenset({
    "stop", "pause", "resume", "auto_stop_undo",
    "confirm_speaker", "skip_speaker", "defer_speaker", "skip_all_speakers",
})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Validate origin to prevent cross-site WebSocket hijacking
    origin = (ws.headers.get("origin") or "").rstrip("/")
    if not _ws_origin_allowed(origin):
        logger.warning(f"Rejected WebSocket from origin: {origin}")
        await ws.close(code=4003, reason="Origin not allowed")
        return

    # Token auth (§4.1): if WS_AUTH_TOKEN is configured, require a matching
    # token in the ?token= query param or the x-ws-token request header.
    if WS_AUTH_TOKEN:
        presented = (
            ws.query_params.get("token")
            or ws.headers.get("x-ws-token")
            or ""
        )
        if presented != WS_AUTH_TOKEN:
            logger.warning("Rejected WebSocket: invalid or missing token")
            await ws.close(code=4001, reason="Unauthorized")
            return

    await ws.accept()
    _active_websockets.add(ws)
    client_id: str | None = None

    # Thread-safe emit that checks connection state
    async def emit(msg: dict):
        try:
            await ws.send_json(msg)
        except Exception as e:
            logger.debug(f"emit failed (client may have disconnected): {type(e).__name__}")

    # Send auth status immediately on connect
    try:
        auth_status = await asyncio.to_thread(check_auth)
        await emit({"type": "auth_status", **auth_status.to_dict()})
    except Exception:
        pass

    session: ClientSession | None = None
    try:
        while True:
            data = json.loads(await ws.receive_text())
            msg_type = data.get("type")

            # Extract client_id from the first message that carries it (including ping)
            if not client_id and "client_id" in data:
                client_id = data["client_id"]
                logger.info(f"Client connected: {client_id}")
                session = _get_session(client_id)
                session.sockets.add(ws)
                # A fresh connection cancels any pending WS-close grace timer.
                if session.wsclose_grace_task is not None and not session.wsclose_grace_task.done():
                    session.wsclose_grace_task.cancel()
                session.wsclose_grace_task = None
                # Reattach emit to existing engine if reconnecting
                engine = session.engine
                if engine is not None:
                    engine.emit = emit
                    logger.info(f"Reattached engine for client {client_id} (session={engine.session_id}, chat_ready={engine.chat_ready})")
                    # Send current state back to client
                    if engine.segments:
                        await emit({
                            "type": "session_loaded",
                            "segments": [s.to_dict() for s in engine.segments],
                            "insights": engine.insights,
                            "summary": engine.summary,
                            "chat_history": engine.chat_history,
                            "started_at": engine.started_at,
                            "ended_at": getattr(engine, "ended_at", ""),
                            "meeting_title": engine.meeting_context.title,
                        })
                        await emit({
                            "type": "status",
                            "meeting_active": engine.active,
                            "paused": engine.paused,
                            "chat_ready": engine.chat_ready,
                            "segment_count": len(engine.segments),
                        })
                        if engine.meeting_context.title != "Unknown Meeting":
                            await emit({"type": "meeting_info", "title": engine.meeting_context.title,
                                        "attendees": engine.meeting_context.attendees})

            # Assign a random client_id if the first message didn't carry one
            if not client_id:
                client_id = str(uuid.uuid4())
                logger.info(f"Client connected (no id provided): {client_id}")
                session = _get_session(client_id)
                session.sockets.add(ws)

            session = _get_session(client_id)
            session.touch()

            if msg_type == "ping":
                await emit({"type": "pong"})
                continue

            # Ensure the primary LLM tier is populated (matches the original
            # per-message `_get_or_create_llm`); handlers read session.llm.
            # Provider construction can fail on misconfiguration (e.g. botocore
            # NoRegionError "You must specify a region."). That must NOT escape the
            # receive loop and kill the connection (the production 66× reconnect
            # storm) — skip this message with an error and keep the loop alive.
            try:
                _get_or_create_llm(session)
            except Exception as e:
                logger.error(f"LLM provider construction failed for {client_id}: {e}")
                await emit({
                    "type": "error",
                    "text": f"LLM provider not configured: {e} — check Settings → AI Provider",
                })
                continue
            engine = session.engine

            handler = _DISPATCH.get(msg_type)
            if handler is None:
                continue
            if msg_type in _ENGINE_REQUIRED and engine is None:
                # Preserve the old `and engine` guards: silently ignore.
                continue
            try:
                await handler(session, data, emit)
            except Exception as e:
                logger.error(f"Error handling '{msg_type}': {e}", exc_info=True)
                await emit({"type": "error", "text": f"Server error: {e}"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {client_id} (engine preserved for reconnect)")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}", exc_info=True)
    finally:
        _active_websockets.discard(ws)
        # Detach from the per-client socket set. If this was the last tab for
        # a client with an active meeting, schedule the WS-close grace auto-stop.
        if client_id:
            session = _sessions.get(client_id)
            if session is not None and ws in session.sockets:
                session.sockets.discard(ws)
                if not session.sockets:
                    engine = session.engine
                    if engine is not None and engine.active and not engine.paused:
                        # Avoid doubling up: cancel any prior grace task first.
                        if session.wsclose_grace_task is not None and not session.wsclose_grace_task.done():
                            session.wsclose_grace_task.cancel()
                        # Grace task uses a no-op emit — there's no active WS.
                        async def _noop_emit(_msg):
                            return
                        session.wsclose_grace_task = asyncio.create_task(
                            _schedule_wsclose_grace(session, _noop_emit)
                        )


# Static files MUST be mounted after routes
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
