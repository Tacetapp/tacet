"""Knowledge vault — markdown-wiki-style knowledge base built from meetings.

Sibling to session_store.py. Where session_store owns raw `meetings-raw/*.md`
records, this module owns the synthesized vault. Default location is
`~/meetings-kb/`; override with the `KNOWLEDGE_DIR` env var. Layout:

    knowledge/
      AGENTS.md            schema/contract any agent reads to navigate
      index.md             navigation hub
      log.md               audit trail of ingest/compile runs
      projects/<slug>.md   project hub — synthesized state
      people/<slug>.md     person page
      sources/<id>.md      one per meeting; links back to meetings/<id>.md
      outputs/             generated artifacts (briefs, architecture, cost)

The vault is plain markdown so external agents (Claude Code, Cursor, or any
compatible AI agent)
can `cd` in and use it without going through this app. AGENTS.md is the
contract that documents the layout for them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, date

import yaml

logger = logging.getLogger(__name__)

# Reused from session_store conventions — same slug rules so a project slug
# never collides with a session_id (sessions are date-prefixed).
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---\n", re.DOTALL)

# Concurrency: two meetings ending at the same time would race on the same
# project page. A single asyncio.Lock per process is sufficient — single-host,
# single-user.
_VAULT_LOCK = asyncio.Lock()


def _resolve_vault_dir() -> str:
    """Where the knowledge vault lives.

    KNOWLEDGE_DIR overrides. Default: a `knowledge/` subfolder inside the
    meetings dir (co-located with the raw meeting records), mirroring
    session_store's MEETINGS_DIR resolution so the two stay siblings.
    """
    env = os.environ.get("KNOWLEDGE_DIR", "").strip()
    if env:
        return os.path.normpath(os.path.expanduser(env))
    meetings = os.environ.get("MEETINGS_DIR", "").strip()
    base = (
        os.path.expanduser(meetings) if meetings
        else os.path.join(os.path.dirname(__file__), "..", "..", "meetings")
    )
    return os.path.normpath(os.path.join(base, "knowledge"))


KNOWLEDGE_DIR = _resolve_vault_dir()

PROJECTS_DIR = os.path.join(KNOWLEDGE_DIR, "projects")
PEOPLE_DIR = os.path.join(KNOWLEDGE_DIR, "people")
SOURCES_DIR = os.path.join(KNOWLEDGE_DIR, "sources")
OUTPUTS_DIR = os.path.join(KNOWLEDGE_DIR, "outputs")
AGENTS_MD = os.path.join(KNOWLEDGE_DIR, "AGENTS.md")
INDEX_MD = os.path.join(KNOWLEDGE_DIR, "index.md")
LOG_MD = os.path.join(KNOWLEDGE_DIR, "log.md")


# ── Slug + frontmatter helpers ──────────────────────────────

def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe kebab-case slug. Empty input -> empty string."""
    if not text:
        return ""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    meta = yaml.safe_load(m.group(1)) or {}
    body = content[m.end():]
    return meta, body


def _dump_page(frontmatter: dict, body: str) -> str:
    """Render a page: YAML frontmatter + blank line + body."""
    fm = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip()
    body = body.strip("\n")
    return f"---\n{fm}\n---\n\n{body}\n"


def _today_iso() -> str:
    return date.today().isoformat()


# ── Vault initialization ────────────────────────────────────

_AGENTS_MD_TEMPLATE_NAME = "agents-md.md"
_INDEX_MD_DEFAULT = """# Knowledge Vault

This vault holds synthesized knowledge built from the meetings in
`../meetings/`. It is plain markdown — open it in Obsidian, read it from
any AI agent, or browse via your editor.

Start with [AGENTS.md](AGENTS.md) for the schema and workflows.

## Sections
- `projects/` — one page per project; the synthesized hub.
- `people/` — one page per person mentioned across meetings.
- `sources/` — one page per ingested meeting.
- `outputs/` — generated artifacts (briefs, architecture, cost estimates).

## Log
See [log.md](log.md) for an audit trail of every ingest and artifact run.
"""

_LOG_MD_HEADER = "# Vault log\n\n"


def init_vault() -> str:
    """Idempotently create the vault directory tree and seed AGENTS.md / index.md / log.md.

    Safe to call on every save. Returns the vault root path.
    """
    for d in (KNOWLEDGE_DIR, PROJECTS_DIR, PEOPLE_DIR, SOURCES_DIR, OUTPUTS_DIR):
        os.makedirs(d, exist_ok=True)

    if not os.path.isfile(AGENTS_MD):
        _atomic_write(AGENTS_MD, _load_agents_md_template())
    if not os.path.isfile(INDEX_MD):
        _atomic_write(INDEX_MD, _INDEX_MD_DEFAULT)
    if not os.path.isfile(LOG_MD):
        _atomic_write(LOG_MD, _LOG_MD_HEADER)

    return KNOWLEDGE_DIR


def _load_agents_md_template() -> str:
    """Read prompts/agents-md.md as the seed for knowledge/AGENTS.md.

    Static content — the vault layout is fixed, so we don't template anything.
    Falls back to a minimal stub if the prompt file is missing.
    """
    prompt_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "prompts", _AGENTS_MD_TEMPLATE_NAME,
    )
    try:
        with open(prompt_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"prompts/{_AGENTS_MD_TEMPLATE_NAME} missing — writing stub AGENTS.md")
        return (
            "# Knowledge Vault — Agent Contract\n\n"
            "See README; the full contract template is missing.\n"
        )


# ── Generic page I/O ────────────────────────────────────────

def _page_path(kind: str, slug: str) -> str:
    if kind == "project":
        return os.path.join(PROJECTS_DIR, f"{slug}.md")
    if kind == "person":
        return os.path.join(PEOPLE_DIR, f"{slug}.md")
    if kind == "source":
        return os.path.join(SOURCES_DIR, f"{slug}.md")
    raise ValueError(f"unknown page kind: {kind}")


def _load_page(kind: str, slug: str) -> tuple[dict, str] | None:
    path = _page_path(kind, slug)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return _parse_frontmatter(content)


def _save_page(kind: str, slug: str, frontmatter: dict, body: str) -> str:
    init_vault()
    path = _page_path(kind, slug)
    _atomic_write(path, _dump_page(frontmatter, body))
    return path


def _list_pages(kind: str) -> list[dict]:
    """Return [{slug, frontmatter}] for all pages of `kind`. Sorted by slug."""
    if kind == "project":
        d = PROJECTS_DIR
    elif kind == "person":
        d = PEOPLE_DIR
    elif kind == "source":
        d = SOURCES_DIR
    else:
        raise ValueError(f"unknown page kind: {kind}")

    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        path = os.path.join(d, fname)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read(8192)  # frontmatter is small
            meta, _ = _parse_frontmatter(content)
            out.append({"slug": slug, "frontmatter": meta})
        except Exception:
            continue
    return out


# ── Project pages ───────────────────────────────────────────

def list_projects() -> list[dict]:
    """[{slug, title, status, brief, updated, members, aliases}]."""
    out: list[dict] = []
    for entry in _list_pages("project"):
        fm = entry["frontmatter"]
        out.append({
            "slug": entry["slug"],
            "title": fm.get("title", entry["slug"]),
            "status": fm.get("status", "active"),
            "brief": fm.get("brief", ""),
            "updated": fm.get("updated", ""),
            "members": fm.get("members", []) or [],
            "aliases": fm.get("aliases", []) or [],
        })
    return out


def load_project(slug: str) -> tuple[dict, str] | None:
    return _load_page("project", slug)


def save_project(slug: str, frontmatter: dict, body: str) -> str:
    return _save_page("project", slug, frontmatter, body)


# ── People pages ────────────────────────────────────────────

def list_people() -> list[dict]:
    out: list[dict] = []
    for entry in _list_pages("person"):
        fm = entry["frontmatter"]
        out.append({
            "slug": entry["slug"],
            "title": fm.get("title", entry["slug"]),
            "aliases": fm.get("aliases", []) or [],
            "projects": fm.get("projects", []) or [],
        })
    return out


def load_person(slug: str) -> tuple[dict, str] | None:
    return _load_page("person", slug)


def save_person(slug: str, frontmatter: dict, body: str) -> str:
    return _save_page("person", slug, frontmatter, body)


# ── Source pages (one per meeting) ──────────────────────────

def write_source(session_id: str, frontmatter: dict, body: str) -> str:
    return _save_page("source", session_id, frontmatter, body)


def load_source(session_id: str) -> tuple[dict, str] | None:
    return _load_page("source", session_id)


# ── Outputs (artifacts) ─────────────────────────────────────

def write_output(filename: str, body: str) -> str:
    """Write to outputs/<filename>. Filename is caller-supplied (already slugged).

    No frontmatter wrapping — artifacts are freeform markdown so external
    agents that generate them via fs_write can drop content directly.
    """
    init_vault()
    if "/" in filename or filename.startswith("."):
        raise ValueError(f"unsafe output filename: {filename!r}")
    path = os.path.join(OUTPUTS_DIR, filename)
    _atomic_write(path, body)
    return path


# ── Log ─────────────────────────────────────────────────────

def append_log(line: str) -> None:
    """Append a single line to log.md, prefixed with an ISO timestamp.

    Errors are swallowed — the log is best-effort.
    """
    try:
        init_vault()
        ts = datetime.now().isoformat(timespec="seconds")
        entry = f"- {ts} — {line.strip()}\n"
        # Append (not atomic; the log is sequential and small)
        with open(LOG_MD, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        logger.warning(f"append_log failed: {e}")


# ── Helpers used by ingest ──────────────────────────────────

def append_touchpoint(project_slug: str, touchpoint_md: str) -> None:
    """Append a line to a project page's `## Touchpoints` section.

    If the page doesn't exist, no-op (caller is expected to ensure_project first).
    If `## Touchpoints` is missing, append the section.
    """
    page = load_project(project_slug)
    if page is None:
        return
    fm, body = page
    line = touchpoint_md.strip()
    if not line:
        return
    if "## Touchpoints" in body:
        # Append at end of the Touchpoints section (i.e. end of file — it's
        # always the last section per the schema).
        body = body.rstrip() + "\n- " + line + "\n"
    else:
        body = body.rstrip() + "\n\n## Touchpoints\n\n- " + line + "\n"
    fm["updated"] = _today_iso()
    save_project(project_slug, fm, body)


_DEFAULT_PROJECT_BODY = (
    "## Brief\n\n{brief}\n\n"
    "## Status\n\n_New project — status not yet recorded._\n\n"
    "## Open Questions\n\n"
    "## Decisions\n\n"
    "## Touchpoints\n\n"
)


def ensure_project(slug: str, *, title: str, brief: str = "",
                   aliases: list[str] | None = None) -> tuple[dict, str]:
    """Create the project page if missing; return (frontmatter, body).

    Existing pages are returned untouched (no aliasing/title overwrites).
    """
    page = load_project(slug)
    if page is not None:
        return page
    today = _today_iso()
    fm = {
        "title": title or slug,
        "type": "project",
        "status": "active",
        "slug": slug,
        "created": today,
        "updated": today,
        "aliases": list(aliases or []),
        "members": [],
        "sources": [],
        "brief": brief or "",
        "tags": [],
    }
    body = _DEFAULT_PROJECT_BODY.format(brief=brief or "_(brief not yet written)_")
    save_project(slug, fm, body)
    return fm, body


_DEFAULT_PERSON_BODY = "## Notes\n\n## Touchpoints\n\n"


def ensure_person(slug: str, *, title: str,
                  aliases: list[str] | None = None) -> tuple[dict, str]:
    """Create the person page if missing; return (frontmatter, body)."""
    page = load_person(slug)
    if page is not None:
        return page
    today = _today_iso()
    fm = {
        "title": title or slug,
        "type": "person",
        "slug": slug,
        "aliases": list(aliases or []),
        "created": today,
        "updated": today,
        "projects": [],
        "sources": [],
    }
    save_person(slug, fm, _DEFAULT_PERSON_BODY)
    return fm, _DEFAULT_PERSON_BODY


def add_session_to_project(project_slug: str, session_id: str) -> None:
    """Idempotently append session_id to the project's `sources` frontmatter list."""
    page = load_project(project_slug)
    if page is None:
        return
    fm, body = page
    sources = list(fm.get("sources") or [])
    if session_id not in sources:
        sources.append(session_id)
        fm["sources"] = sources
        fm["updated"] = _today_iso()
        save_project(project_slug, fm, body)


def add_session_to_person(person_slug: str, session_id: str,
                          project_slugs: list[str] | None = None) -> None:
    """Idempotently append session_id to the person's `sources` and project_slugs to `projects`."""
    page = load_person(person_slug)
    if page is None:
        return
    fm, body = page
    changed = False
    sources = list(fm.get("sources") or [])
    if session_id not in sources:
        sources.append(session_id)
        fm["sources"] = sources
        changed = True
    projects = list(fm.get("projects") or [])
    for ps in project_slugs or []:
        if ps not in projects:
            projects.append(ps)
            changed = True
    if changed:
        fm["projects"] = projects
        fm["updated"] = _today_iso()
        save_person(person_slug, fm, body)
