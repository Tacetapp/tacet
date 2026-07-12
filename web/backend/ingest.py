"""Ingest pipeline — fold a saved meeting into the knowledge vault.

Triggered by main.py after `engine.stop()` completes (auto-ingest), or by a
`reingest_session` WS message (manual). One LLM call per ingest:

  1. Load the saved session from session_store.
  2. Snapshot existing projects + people from the vault.
  3. Render prompts/ingest-meeting.md with the meeting + the snapshot.
  4. Ask the model to emit a JSON block describing the project + people +
     source page + touchpoint.
  5. Apply the JSON to the vault under _VAULT_LOCK.

Failure modes are logged and surfaced via knowledge_store.append_log; this
module never raises into the caller's task.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from web.backend import knowledge_store as ks
from web.backend import session_store as ss
from web.backend.llm_service import LLMService, InvalidModelError, create_llm_service

logger = logging.getLogger(__name__)

INGEST_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "prompts", "ingest-meeting.md",
)
TRANSCRIPT_CHAR_CAP = 4000  # last N chars of transcript fed to the model


# ── Prompt rendering ────────────────────────────────────────

def _read_prompt() -> str:
    with open(INGEST_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _format_attendees(attendees: list[str]) -> str:
    return ", ".join(attendees) if attendees else "(none on calendar)"


def _format_insights(insights: list[dict]) -> str:
    if not insights:
        return "(none)"
    lines = []
    for ins in insights[:30]:
        cat = ins.get("category", "")
        label = ins.get("label", "")
        text = ins.get("text", "")
        prefix = f"[{cat}] {label}: " if label else f"[{cat}] "
        lines.append(f"- {prefix}{text}")
    return "\n".join(lines)


def _format_transcript_window(segments: list[dict]) -> str:
    if not segments:
        return "(no transcript)"
    lines = []
    for seg in segments:
        speaker = seg.get("speaker") or ""
        prefix = f"{speaker}: " if speaker else ""
        lines.append(f"{prefix}{seg.get('text', '')}")
    text = "\n".join(lines)
    if len(text) > TRANSCRIPT_CHAR_CAP:
        text = "…\n" + text[-TRANSCRIPT_CHAR_CAP:]
    return text


def _format_existing_projects(projects: list[dict]) -> str:
    if not projects:
        return "(none yet — propose a new project unless this is clearly an off-topic 1:1)"
    lines = []
    for p in projects:
        aliases = ", ".join(p.get("aliases", []) or [])
        alias_part = f" (aliases: {aliases})" if aliases else ""
        brief = p.get("brief", "") or ""
        lines.append(f"- **{p['slug']}** — {p['title']}{alias_part}: {brief}")
    return "\n".join(lines)


def _format_existing_people(people: list[dict]) -> str:
    if not people:
        return "(none yet)"
    lines = []
    for p in people:
        aliases = ", ".join(p.get("aliases", []) or [])
        alias_part = f" (aliases: {aliases})" if aliases else ""
        lines.append(f"- **{p['slug']}** — {p['title']}{alias_part}")
    return "\n".join(lines)


def _render_ingest_prompt(session: dict) -> str:
    """Substitute meeting + vault snapshot into the prompt template.

    Uses str.replace() (not str.format) because the prompt's JSON schema
    example contains literal {…} braces that would crash str.format.
    """
    template = _read_prompt()
    ctx = session.get("meeting_context", {}) or {}
    fields = {
        "{session_id}": session.get("id", ""),
        "{title}": ctx.get("title", "Unknown Meeting"),
        "{started_at}": session.get("started_at", ""),
        "{attendees}": _format_attendees(ctx.get("attendees") or []),
        "{agenda}": ctx.get("agenda") or "(none)",
        "{summary}": session.get("summary") or "(no summary generated)",
        "{insights_block}": _format_insights(session.get("insights") or []),
        "{transcript_window}": _format_transcript_window(session.get("segments") or []),
        "{existing_projects_block}": _format_existing_projects(ks.list_projects()),
        "{existing_people_block}": _format_existing_people(ks.list_people()),
    }
    out = template
    for k, v in fields.items():
        out = out.replace(k, str(v))
    return out


# ── JSON extraction ─────────────────────────────────────────

_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Find the first JSON object in the model output. Lenient.

    Tries: (1) a fenced ```json block, (2) the first top-level {…}.
    """
    if not text:
        return None
    m = _FENCED_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: brace-balance scan from the first '{'.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── Apply ───────────────────────────────────────────────────

# Slug validation pattern (§4.3): must start with a letter/digit, then only
# lowercase alphanumerics and hyphens, max 64 chars total.
_VALID_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


def _safe_slug(s: str) -> str:
    return ks.slugify(s) if s else ""


def _is_valid_slug(slug: str) -> bool:
    return bool(slug) and bool(_VALID_SLUG_RE.match(slug))


def _apply(session: dict, parsed: dict, *, force: bool) -> dict:
    """Apply a parsed ingest payload to the vault. Returns a result dict.

    Caller should hold ks._VAULT_LOCK.
    """
    session_id = session.get("id", "")
    if not session_id:
        return {"ok": False, "error": "session has no id"}

    proj = parsed.get("project") or {}
    proj_slug = _safe_slug(proj.get("slug", ""))
    proj_title = proj.get("title", "") or proj_slug
    proj_match = proj.get("match", "")
    proj_brief = proj.get("brief", "") or ""

    if not proj_slug:
        return {"ok": False, "error": "project.slug missing"}

    # Reject slugs that don't match the safe pattern — guards against path
    # traversal or other injection via model-generated slugs (§4.3).
    if not _is_valid_slug(proj_slug):
        logger.warning(f"ingest: invalid project slug from model: {proj_slug!r}; skipping session {session_id}")
        return {"ok": False, "error": f"invalid project slug: {proj_slug!r}"}

    # Ensure project page exists (no-op if already there).
    if proj_match == "new" or ks.load_project(proj_slug) is None:
        ks.ensure_project(proj_slug, title=proj_title, brief=proj_brief)
    ks.add_session_to_project(proj_slug, session_id)

    # People.
    people_slugs: list[str] = []
    for person in parsed.get("people") or []:
        slug = _safe_slug(person.get("slug", ""))
        title = person.get("title", "") or slug
        aliases = person.get("aliases") or []
        if not slug:
            continue
        if not _is_valid_slug(slug):
            logger.warning(f"ingest: invalid person slug from model: {slug!r}; skipping")
            continue
        if person.get("match") == "new" or ks.load_person(slug) is None:
            ks.ensure_person(slug, title=title, aliases=aliases)
        ks.add_session_to_person(slug, session_id, project_slugs=[proj_slug])
        people_slugs.append(slug)

    # Source page.
    source_page = parsed.get("source_page") or {}
    ctx = session.get("meeting_context", {}) or {}
    source_fm = {
        "title": ctx.get("title", "Unknown Meeting"),
        "type": "source",
        "subtype": "meeting",
        "session_id": session_id,
        "meeting_path": _meeting_relpath(session_id),
        "started_at": session.get("started_at", ""),
        "projects": [proj_slug],
        "people": people_slugs,
        "created": _today(),
        "updated": _today(),
    }
    source_body = _render_source_body(source_page)
    ks.write_source(session_id, source_fm, source_body)

    # Touchpoint on the project page.
    touchpoint = (parsed.get("touchpoint") or "").strip()
    if touchpoint:
        ks.append_touchpoint(proj_slug, touchpoint)

    # Optional brief refresh.
    if proj.get("refresh_brief") and proj_brief:
        page = ks.load_project(proj_slug)
        if page is not None:
            fm, body = page
            fm["brief"] = proj_brief
            fm["updated"] = _today()
            ks.save_project(proj_slug, fm, body)

    # Write the project slug back into the meeting frontmatter.
    update = ss.update_session_projects(session_id, [proj_slug])
    if "error" in update:
        logger.warning(f"update_session_projects: {update['error']}")

    ks.append_log(
        f"ingest session={session_id} project={proj_slug} "
        f"people=[{','.join(people_slugs)}]"
        + (" force" if force else "")
    )

    return {
        "ok": True,
        "project_slug": proj_slug,
        "people": people_slugs,
        "source_path": ks._page_path("source", session_id),
    }


def _today() -> str:
    return datetime.now().date().isoformat()


def _meeting_relpath(session_id: str) -> str:
    """Relative path from sources/<id>.md to the raw meeting file.

    Computed from KNOWLEDGE_DIR + SESSIONS_DIR so users with custom paths still
    get a sensible backlink. The exact slug suffix isn't known without scanning,
    so we glob for an `<id>-*.md` match before falling back to `<id>.md`.
    """
    sources_dir = os.path.join(ks.KNOWLEDGE_DIR, "sources")
    raw_dir = ss.SESSIONS_DIR
    candidate = os.path.join(raw_dir, f"{session_id}.md")
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, sources_dir)
    if os.path.isdir(raw_dir):
        for fname in os.listdir(raw_dir):
            if fname.startswith(session_id + "-") and fname.endswith(".md"):
                return os.path.relpath(os.path.join(raw_dir, fname), sources_dir)
    return os.path.relpath(candidate, sources_dir)


def _render_source_body(source_page: dict) -> str:
    """Build the markdown body for sources/<id>.md."""
    summary = (source_page.get("summary") or "").strip() or "_(no summary)_"
    sections = [("## Summary", summary)]
    for header, key in (
        ("## Claims", "claims"),
        ("## Decisions", "decisions"),
        ("## Action Items", "action_items"),
        ("## Open Questions", "open_questions"),
    ):
        items = source_page.get(key) or []
        if not items:
            sections.append((header, "_(none)_"))
            continue
        bullets = "\n".join(f"- {str(item).strip()}" for item in items if str(item).strip())
        sections.append((header, bullets or "_(none)_"))
    return "\n\n".join(f"{h}\n\n{b}" for h, b in sections) + "\n"


# ── Public API ──────────────────────────────────────────────

async def ingest_session(
    session_id: str, provider: LLMService, *, force: bool = False,
) -> dict:
    """Run the ingest pipeline for a single saved meeting.

    Never raises. Returns {"ok": bool, ...} either way; failures are also
    logged to knowledge/log.md.
    """
    try:
        ks.init_vault()

        session = ss.load_session(session_id)
        if session is None:
            ks.append_log(f"ingest session={session_id} skipped: not found")
            return {"ok": False, "error": f"session {session_id} not found"}

        if not session.get("segments"):
            ks.append_log(f"ingest session={session_id} skipped: empty transcript")
            return {"ok": False, "error": "empty transcript"}

        if not force and (session.get("projects") or []):
            existing = session["projects"]
            ks.append_log(
                f"ingest session={session_id} skipped: already in projects={existing}"
            )
            return {"ok": True, "skipped": True, "project_slug": existing[0]}

        prompt = _render_ingest_prompt(session)
        try:
            response = await provider.analyze(
                [LLMService.msg("user", prompt)], system_prompt=None, max_tokens=4096,
            )
        except InvalidModelError as e:
            # main.py passes the engine's fast tier as the provider. If that fast
            # model ID is invalid, degrade gracefully: retry once on a fresh
            # primary-tier service (fast=False) so ingest still runs (§ fallback).
            logger.warning(f"ingest fast model invalid ({e}); retrying on primary-tier service")
            ks.append_log(f"ingest session={session_id} fast_model_invalid; retrying on primary tier")
            try:
                primary = create_llm_service(fast=False)
                response = await primary.analyze(
                    [LLMService.msg("user", prompt)], system_prompt=None, max_tokens=4096,
                )
            except Exception as e2:
                logger.error(f"ingest primary-tier retry failed: {e2}", exc_info=True)
                ks.append_log(f"ingest session={session_id} llm_error: {type(e2).__name__}: {e2}")
                return {"ok": False, "error": f"LLM call failed: {e2}"}
        except Exception as e:
            logger.error(f"ingest LLM call failed: {e}", exc_info=True)
            ks.append_log(f"ingest session={session_id} llm_error: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"LLM call failed: {e}"}

        parsed = _extract_json(response)
        if not parsed:
            preview = (response or "")[:300].replace("\n", " ")
            ks.append_log(f"ingest session={session_id} parse_error response_preview=\"{preview}\"")
            return {"ok": False, "error": "could not parse JSON from model response"}

        async with ks._VAULT_LOCK:
            return _apply(session, parsed, force=force)

    except Exception as e:
        logger.error(f"ingest_session failed: {e}", exc_info=True)
        try:
            ks.append_log(f"ingest session={session_id} fatal: {type(e).__name__}: {e}")
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def reingest_session(session_id: str, provider: LLMService) -> dict:
    """Re-run ingest for a session that already has a project assignment."""
    return await ingest_session(session_id, provider, force=True)


# ── Backfill CLI ────────────────────────────────────────────

async def _backfill_all(provider: LLMService, *, force: bool = False) -> None:
    """Walk meetings/ and ingest every session sequentially. Used by the CLI."""
    sessions = ss.list_sessions()
    if not sessions:
        logger.info("backfill: no sessions found")
        return
    logger.info(f"backfill: ingesting {len(sessions)} session(s)")
    for s in sessions:
        sid = s["id"]
        result = await ingest_session(sid, provider, force=force)
        logger.info(f"backfill {sid}: {result}")


def _cli() -> None:
    """Run as `python -m web.backend.ingest --backfill [--force]` or `--session <id>`.

    Uses the configured LLM provider (LLM_PROVIDER env, default bedrock). Set the
    matching credentials (AWS SSO for bedrock, OPENROUTER_API_KEY, …) first.
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="ingest all saved meetings")
    parser.add_argument("--force", action="store_true", help="re-ingest even if already assigned to a project")
    parser.add_argument("--session", help="ingest a single session by id")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    async def _run():
        provider = create_llm_service()
        if args.backfill:
            await _backfill_all(provider, force=args.force)
        elif args.session:
            result = await ingest_session(args.session, provider, force=args.force)
            print(json.dumps(result, indent=2))
        else:
            parser.error("nothing to do; pass --backfill or --session <id>")

    asyncio.run(_run())


if __name__ == "__main__":
    _cli()
