"""Session storage — markdown wiki format with YAML frontmatter."""

from __future__ import annotations

import os
import re
import tempfile
import logging
from datetime import datetime

import yaml

logger = logging.getLogger(__name__)

def _resolve_sessions_dir() -> str:
    """Where saved meetings live. Configurable via MEETINGS_DIR env var.

    Default: `<repo>/meetings`. Users can point this at any path — the setup
    script prompts for it on first run and persists to `.env`.
    """
    env = os.environ.get("MEETINGS_DIR", "").strip()
    if env:
        return os.path.normpath(os.path.expanduser(env))
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "meetings"))


SESSIONS_DIR = _resolve_sessions_dir()

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 50) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug


def _find_session_path(session_id: str) -> str | None:
    """Find a session file by session_id prefix (handles slug suffix)."""
    if not os.path.isdir(SESSIONS_DIR):
        return None
    # Try exact match first (legacy or no-title files)
    exact = os.path.join(SESSIONS_DIR, f"{session_id}.md")
    if os.path.isfile(exact):
        return exact
    # Glob for session_id prefix
    for fname in os.listdir(SESSIONS_DIR):
        if fname.startswith(session_id + "-") and fname.endswith(".md"):
            return os.path.join(SESSIONS_DIR, fname)
    return None


def _session_filename(session_id: str, title: str) -> str:
    """Build filename from session_id and title."""
    if title and title != "Unknown Meeting":
        slug = _slugify(title)
        if slug:
            return f"{session_id}-{slug}.md"
    return f"{session_id}.md"

# ── Markdown generation ──────────────────────────────

_CATEGORY_ORDER = ["term", "context", "key_point", "suggested_question", "visual"]
_CATEGORY_TITLES = {
    "term": "Terms",
    "context": "Context",
    "key_point": "Key Points",
    "suggested_question": "Suggested Questions",
    "visual": "Visual",
}


def _session_to_markdown(data: dict) -> str:
    """Convert an engine session dict to a markdown string."""
    ctx = data.get("meeting_context", {})

    # Build frontmatter
    meta = {
        "id": data.get("id", ""),
        "title": ctx.get("title", "Unknown Meeting"),
        "started_at": data.get("started_at", ""),
        "ended_at": data.get("ended_at", ""),
        "srt_path": data.get("srt_path", ""),
        "segment_count": data.get("segment_count", 0),
        "attendees": ctx.get("attendees", []),
        "agenda": ctx.get("agenda", ""),
        "start_time": ctx.get("start_time", ""),
        "end_time": ctx.get("end_time", ""),
        "explained_terms": sorted(data.get("explained_terms", [])),
        "projects": data.get("projects", []) or [],
    }

    parts = ["---"]
    parts.append(yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())
    parts.append("---")
    parts.append("")

    # Summary
    summary = data.get("summary", "")
    parts.append("## Summary")
    parts.append("")
    parts.append(summary if summary else "(no summary)")
    parts.append("")

    # Insights
    insights = data.get("insights", [])
    parts.append("## Insights")
    parts.append("")
    if not insights:
        parts.append("(no insights)")
    else:
        by_cat: dict[str, list[dict]] = {}
        for ins in insights:
            cat = ins.get("category", "key_point")
            by_cat.setdefault(cat, []).append(ins)

        for cat in _CATEGORY_ORDER:
            items = by_cat.pop(cat, [])
            if not items:
                continue
            parts.append(f"### {_CATEGORY_TITLES.get(cat, cat)}")
            parts.append("")
            for ins in items:
                label = ins.get("label", "")
                text = ins.get("text", "")
                if cat == "term" and label:
                    parts.append(f"- **{label}** — {text}")
                else:
                    parts.append(f"- {text}")
            parts.append("")

        # Any unexpected categories
        for cat, items in by_cat.items():
            parts.append(f"### {cat}")
            parts.append("")
            for ins in items:
                parts.append(f"- {ins.get('text', '')}")
            parts.append("")
    parts.append("")

    # Transcript
    segments = data.get("segments", [])
    parts.append("## Transcript")
    parts.append("")
    if not segments:
        parts.append("(no transcript)")
    else:
        parts.append("```transcript")
        for seg in segments:
            start = seg.get("start", "00:00:00,000")
            end = seg.get("end", "00:00:00,000")
            text = seg.get("text", "")
            speaker = seg.get("speaker", "")
            prefix = f"{speaker}: " if speaker else ""
            parts.append(f"[{start} --> {end}] {prefix}{text}")
        parts.append("```")
    parts.append("")

    # Chat History
    chat = data.get("chat_history", [])
    parts.append("## Chat History")
    parts.append("")
    if not chat:
        parts.append("(no chat)")
    else:
        for msg in chat:
            role = msg.get("role", "user")
            content_blocks = msg.get("content", [])
            text = ""
            for block in content_blocks:
                if isinstance(block, dict) and "text" in block:
                    text += block["text"]
            if not text:
                continue
            parts.append(f"**{role}**: {text}")
            parts.append("")
    parts.append("")

    return "\n".join(parts)


# ── Markdown parsing ─────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---\n", re.DOTALL)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
# Timestamp bracket + remainder. The optional "speaker: " prefix is split off
# and validated separately so sentence text that merely contains a colon (e.g. a
# time like "10:30 works for me") isn't mis-parsed as a speaker (§2.9).
_TRANSCRIPT_LINE_RE = re.compile(r"\[([^\]]+?)\s*-->\s*([^\]]+?)\]\s*(.+)")
_SPEAKER_PREFIX_RE = re.compile(r"^(.{1,40}?):\s(.+)$", re.DOTALL)
_SPEAKER_CLUSTER_RE = re.compile(r"^Speaker \d+$")
_SPEAKER_NAME_RE = re.compile(r"^[^\W\d_][\w'’.\- ]*$")
_INSIGHT_TERM_RE = re.compile(r"^\*\*(.+?)\*\*\s*[—–-]\s*(.+)$")
_CHAT_MSG_RE = re.compile(r"^\*\*(user|assistant)\*\*:\s*(.*)$", re.MULTILINE)


def _is_plausible_speaker(candidate: str) -> bool:
    """Heuristic: is `candidate` a real speaker label, or just sentence text that
    happens to precede a colon (a time '10:30', 'Note', a numbered list…)?

    Accepts the channel labels ("You"/"Others"), diarization cluster keys
    ("Speaker 3"), and name-like strings (start with a letter, ≤4 words, no
    digits). This keeps the writer format backward compatible while rejecting
    time-like/numeric prefixes (§2.9).
    """
    c = candidate.strip()
    if not c or len(c) > 40:
        return False
    if c in ("You", "Others") or _SPEAKER_CLUSTER_RE.match(c):
        return True
    # Times and numbered fragments contain digits; real names don't.
    if any(ch.isdigit() for ch in c):
        return False
    if len(c.split()) > 4:
        return False
    return bool(_SPEAKER_NAME_RE.match(c))


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    meta = yaml.safe_load(m.group(1)) or {}
    body = content[m.end():]
    return meta, body


_KNOWN_SECTIONS = {"Summary", "Insights", "Transcript", "Chat History"}


def _split_sections(body: str) -> dict[str, str]:
    """Split markdown body into {section_title: content} dict.

    Only splits on known top-level sections to avoid breaking on ## headers
    inside chat messages or summaries.
    """
    sections: dict[str, str] = {}
    matches = [m for m in _SECTION_RE.finditer(body) if m.group(1).strip() in _KNOWN_SECTIONS]
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[title] = body[start:end].strip()
    return sections


def _parse_transcript(text: str) -> list[dict]:
    """Parse transcript fenced block back to segment dicts."""
    segments = []
    # Extract content between ```transcript and ```
    m = re.search(r"```transcript\n(.+?)```", text, re.DOTALL)
    if not m:
        return segments
    idx = 1
    for line in m.group(1).strip().split("\n"):
        lm = _TRANSCRIPT_LINE_RE.match(line.strip())
        if not lm:
            continue
        rest = lm.group(3).strip()
        speaker = None
        text = rest
        sm = _SPEAKER_PREFIX_RE.match(rest)
        if sm and _is_plausible_speaker(sm.group(1)):
            speaker = sm.group(1).strip()
            text = sm.group(2).strip()
        seg = {
            "index": idx,
            "start": lm.group(1).strip(),
            "end": lm.group(2).strip(),
            "text": text,
        }
        if speaker:
            seg["speaker"] = speaker
        segments.append(seg)
        idx += 1
    return segments


def _parse_insights(text: str) -> list[dict]:
    """Parse insights section back to insight dicts."""
    insights = []
    current_cat = "key_point"
    iid = 0

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("### "):
            title = line[4:].strip()
            # Reverse lookup category from title
            for cat, cat_title in _CATEGORY_TITLES.items():
                if cat_title == title:
                    current_cat = cat
                    break
            else:
                current_cat = title.lower().replace(" ", "_")
            continue

        if not line.startswith("- "):
            continue

        bullet = line[2:].strip()
        if not bullet:
            continue

        insight: dict = {"id": iid, "category": current_cat}

        if current_cat == "term":
            m = _INSIGHT_TERM_RE.match(bullet)
            if m:
                insight["label"] = m.group(1)
                insight["text"] = m.group(2)
            else:
                insight["text"] = bullet
        else:
            insight["text"] = bullet

        insights.append(insight)
        iid += 1

    return insights


def _parse_chat_history(text: str) -> list[dict]:
    """Parse chat history section back to message dicts."""
    messages = []
    matches = list(_CHAT_MSG_RE.finditer(text))
    for i, m in enumerate(matches):
        role = m.group(1)
        start = m.start() + len(m.group(0))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # First line is inline with **role**: , rest is the body
        first_line = m.group(2)
        rest = text[start:end].strip()
        full_text = (first_line + "\n" + rest).strip() if rest else first_line.strip()
        if full_text:
            messages.append({"role": role, "content": [{"text": full_text}]})
    return messages


def _markdown_to_session(content: str) -> dict:
    """Parse a markdown session file back to an engine session dict."""
    meta, body = _parse_frontmatter(content)
    sections = _split_sections(body)

    segments = _parse_transcript(sections.get("Transcript", ""))
    insights = _parse_insights(sections.get("Insights", ""))
    chat_history = _parse_chat_history(sections.get("Chat History", ""))
    summary = sections.get("Summary", "").strip()
    if summary == "(no summary)":
        summary = ""

    return {
        "id": meta.get("id", ""),
        "srt_path": meta.get("srt_path", ""),
        "started_at": meta.get("started_at", ""),
        "ended_at": meta.get("ended_at", ""),
        "segment_count": meta.get("segment_count", len(segments)),
        "segments": segments,
        "insights": insights,
        "explained_terms": meta.get("explained_terms", []),
        "chat_history": chat_history,
        "summary": summary,
        "projects": meta.get("projects", []) or [],
        "meeting_context": {
            "title": meta.get("title", "Unknown Meeting"),
            "attendees": meta.get("attendees", []),
            "agenda": meta.get("agenda", ""),
            "start_time": meta.get("start_time", ""),
            "end_time": meta.get("end_time", ""),
        },
    }


# ── File I/O ─────────────────────────────────────────

def _atomic_write_md(path: str, content: str):
    """Write markdown atomically via temp file + rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def save_session(data: dict) -> str:
    """Write session dict as a markdown file. Returns the file path."""
    session_id = data.get("id", datetime.now().strftime("%Y-%m-%d-%H%M%S"))
    title = data.get("meeting_context", {}).get("title", "")
    fname = _session_filename(session_id, title)
    path = os.path.join(SESSIONS_DIR, fname)
    md = _session_to_markdown(data)
    # Write new file first, then remove old if name changed
    old_path = _find_session_path(session_id)
    _atomic_write_md(path, md)
    if old_path and os.path.normpath(old_path) != os.path.normpath(path):
        os.unlink(old_path)
    logger.info(f"Session saved: {path}")
    return path


def load_session(session_id: str) -> dict | None:
    """Load a markdown session file, return parsed dict or None."""
    path = _find_session_path(session_id)
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return _markdown_to_session(content)


def update_session_projects(session_id: str, projects: list[str]) -> dict:
    """Set the `projects` frontmatter list on a saved session.

    Used by the ingest pipeline to write the inferred project slug back into the
    meeting's raw record. Idempotent — replaces the field wholesale.
    """
    old_path = _find_session_path(session_id)
    if not old_path:
        return {"error": f"Session {session_id} not found"}

    with open(old_path, encoding="utf-8") as f:
        content = f.read()

    meta, body = _parse_frontmatter(content)
    meta["projects"] = list(projects or [])

    parts = ["---"]
    parts.append(yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())
    parts.append("---")
    parts.append("")
    parts.append(body)

    _atomic_write_md(old_path, "\n".join(parts))
    return {"success": True, "projects": meta["projects"]}


def list_sessions() -> list[dict]:
    """List all sessions by reading YAML frontmatter only."""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    sessions = []
    for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read(4096)  # frontmatter is small, read first 4KB
            meta, _ = _parse_frontmatter(content)
            sessions.append({
                "id": meta.get("id", fname[:-3]),
                "started_at": meta.get("started_at", ""),
                "ended_at": meta.get("ended_at", ""),
                "segment_count": meta.get("segment_count", 0),
                "srt_path": meta.get("srt_path", ""),
                "title": meta.get("title", ""),
                "has_summary": True,  # markdown files always have summary section
            })
        except Exception:
            continue
    return sessions


def search_sessions(query: str) -> list[dict]:
    """Full-text search across markdown session files."""
    if not os.path.isdir(SESSIONS_DIR) or not query.strip():
        return []

    terms = query.lower().split()
    results = []

    for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()

            meta, body = _parse_frontmatter(content)
            title = meta.get("title", "")
            session_id = meta.get("id", fname[:-3])

            full_text = (title + " " + body).lower()
            matched_terms = [t for t in terms if t in full_text]
            if not matched_terms:
                continue

            # Find best snippet
            snippet = ""
            snippet_source = ""
            sections = _split_sections(body)
            search_order = [
                ("title", title),
                ("summary", sections.get("Summary", "")),
                ("transcript", sections.get("Transcript", "")),
                ("insight", sections.get("Insights", "")),
                ("chat", sections.get("Chat History", "")),
            ]
            for source, text in search_order:
                text_lower = text.lower()
                for t in matched_terms:
                    idx = text_lower.find(t)
                    if idx >= 0:
                        start = max(0, idx - 60)
                        end = min(len(text), idx + 60)
                        snippet = ("..." if start > 0 else "") + text[start:end].strip() + ("..." if end < len(text) else "")
                        snippet_source = source
                        break
                if snippet:
                    break

            results.append({
                "id": session_id,
                "started_at": meta.get("started_at", ""),
                "title": title,
                "segment_count": meta.get("segment_count", 0),
                "has_summary": True,
                "srt_path": meta.get("srt_path", ""),
                "score": len(matched_terms),
                "snippet": snippet,
                "snippet_source": snippet_source,
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def load_session_details(session_id: str) -> dict:
    """Load summarized session details for Bedrock tool use."""
    data = load_session(session_id)
    if not data:
        return {"error": f"Session {session_id} not found"}
    ctx = data.get("meeting_context", {})
    transcript = " ".join(s.get("text", "") for s in data.get("segments", []))
    return {
        "id": data.get("id"),
        "title": ctx.get("title", ""),
        "attendees": ctx.get("attendees", []),
        "started_at": data.get("started_at", ""),
        "summary": data.get("summary", "")[:2000],
        "transcript_preview": transcript[:3000],
        "segment_count": data.get("segment_count", 0),
        "insight_count": len(data.get("insights", [])),
    }


def update_session_meeting_context(
    session_id: str, title: str | None = None, attendees: list[str] | None = None
) -> dict:
    """Update meeting context in a session's frontmatter."""
    old_path = _find_session_path(session_id)
    if not old_path:
        return {"error": f"Session {session_id} not found"}

    with open(old_path, encoding="utf-8") as f:
        content = f.read()

    meta, body = _parse_frontmatter(content)
    if title is not None:
        meta["title"] = title
    if attendees is not None:
        meta["attendees"] = attendees

    # Rebuild the full file
    parts = ["---"]
    parts.append(yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())
    parts.append("---")
    parts.append("")
    parts.append(body)

    # Rename file if title changed
    new_fname = _session_filename(session_id, meta.get("title", ""))
    new_path = os.path.join(SESSIONS_DIR, new_fname)
    _atomic_write_md(new_path, "\n".join(parts))
    if os.path.normpath(old_path) != os.path.normpath(new_path):
        os.unlink(old_path)

    return {"success": True, "title": meta.get("title"), "attendees": meta.get("attendees", [])}
