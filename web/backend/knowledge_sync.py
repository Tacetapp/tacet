"""Knowledge vault sync — the Karpathy "lint / health-check" pass.

Ingest is forward-only (each meeting compiles into the wiki as it ends). Over
time the graph drifts: index.md goes stale, backlinks fall out of sync, and the
same project/person accumulates under slightly different slugs. This module is
the separate maintenance pass that keeps the vault consistent, triggered on
demand from the UI.

Two tiers:
  A. Deterministic lint (fast, no LLM, always runs)
     - rebuild_index()          : regenerate index.md from the actual pages
     - reconcile_backlinks()    : rebuild project/person cross-refs from the
                                  source pages (the ground truth)
     - scan_broken_wikilinks()  : find [[slug]] links with no target page
  B. LLM consolidation (opt-in "deep sync", one call each)
     - dedup_entities()         : merge duplicate projects/people (auto-merge)
     - refresh_briefs()         : re-synthesize each project brief/status

`sync_vault(provider, deep)` orchestrates and returns a change report.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict

from web.backend import knowledge_store as ks

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")  # [[slug]], [[slug|alias]], [[slug#h]]
_BRIEF_REFRESH_CAP = 25  # max projects to LLM-refresh in one deep sync


# ── Page enumeration ────────────────────────────────────────

def _all_slugs() -> dict[str, set[str]]:
    """All existing page slugs by kind (for wikilink resolution + dedup)."""
    return {
        "project": {e["slug"] for e in ks._list_pages("project")},
        "person": {e["slug"] for e in ks._list_pages("person")},
        "source": {e["slug"] for e in ks._list_pages("source")},
    }


def _iter_pages():
    """Yield (kind, slug, frontmatter, body) for every project/person/source page."""
    for kind in ("project", "person", "source"):
        for entry in ks._list_pages(kind):
            page = ks._load_page(kind, entry["slug"])
            if page is not None:
                fm, body = page
                yield kind, entry["slug"], fm, body


# ── Tier A: deterministic lint ──────────────────────────────

def rebuild_index() -> bool:
    """Regenerate index.md as a fresh catalog of all pages. Returns True."""
    projects = sorted(ks.list_projects(), key=lambda p: p["title"].lower())
    people = sorted(ks.list_people(), key=lambda p: p["title"].lower())
    sources = ks._list_pages("source")

    lines = [
        "# Knowledge Vault",
        "",
        "Auto-generated index (regenerated on sync). See [AGENTS.md](AGENTS.md) "
        "for the schema and [log.md](log.md) for the operation history.",
        "",
        f"## Projects ({len(projects)})",
        "",
    ]
    for p in projects:
        brief = f" — {p['brief']}" if p.get("brief") else ""
        lines.append(f"- [[{p['slug']}]] · {p['title']} ({p.get('status', 'active')}){brief}")
    lines += ["", f"## People ({len(people)})", ""]
    for p in people:
        projs = ", ".join(p.get("projects", []) or [])
        proj_part = f" — {projs}" if projs else ""
        lines.append(f"- [[{p['slug']}]] · {p['title']}{proj_part}")
    lines += ["", f"## Sources ({len(sources)})", ""]
    for s in sorted(sources, key=lambda e: e["slug"], reverse=True):
        title = s["frontmatter"].get("title", s["slug"])
        lines.append(f"- [[{s['slug']}]] · {title}")
    lines.append("")

    ks.init_vault()
    ks._atomic_write(ks.INDEX_MD, "\n".join(lines))
    return True


def reconcile_backlinks() -> int:
    """Rebuild project/person derived cross-refs from the source pages.

    Source pages are the ground truth (each = one meeting, listing its projects
    and people). We recompute project.sources/members and person.sources/projects
    from all sources and rewrite any page whose lists drifted. Returns the number
    of pages repaired.
    """
    proj_sources: dict[str, set[str]] = defaultdict(set)
    proj_members: dict[str, set[str]] = defaultdict(set)
    person_sources: dict[str, set[str]] = defaultdict(set)
    person_projects: dict[str, set[str]] = defaultdict(set)

    for entry in ks._list_pages("source"):
        sid = entry["slug"]
        fm = entry["frontmatter"]
        s_projects = [p for p in (fm.get("projects") or []) if p]
        s_people = [p for p in (fm.get("people") or []) if p]
        for ps in s_projects:
            proj_sources[ps].add(sid)
            proj_members[ps].update(s_people)
        for pe in s_people:
            person_sources[pe].add(sid)
            person_projects[pe].update(s_projects)

    repaired = 0
    for entry in ks._list_pages("project"):
        slug = entry["slug"]
        page = ks.load_project(slug)
        if page is None:
            continue
        fm, body = page
        want_sources = sorted(proj_sources.get(slug, set()))
        want_members = sorted(proj_members.get(slug, set()))
        if (fm.get("sources") or []) != want_sources or (fm.get("members") or []) != want_members:
            fm["sources"] = want_sources
            fm["members"] = want_members
            ks.save_project(slug, fm, body)
            repaired += 1

    for entry in ks._list_pages("person"):
        slug = entry["slug"]
        page = ks.load_person(slug)
        if page is None:
            continue
        fm, body = page
        want_sources = sorted(person_sources.get(slug, set()))
        want_projects = sorted(person_projects.get(slug, set()))
        if (fm.get("sources") or []) != want_sources or (fm.get("projects") or []) != want_projects:
            fm["sources"] = want_sources
            fm["projects"] = want_projects
            ks.save_person(slug, fm, body)
            repaired += 1

    return repaired


def scan_broken_wikilinks() -> list[dict]:
    """Find [[slug]] references in page bodies that don't resolve to a real page.

    Returns [{kind, slug, target}] — `target` is the unresolved wikilink.
    """
    slugs = _all_slugs()
    valid = slugs["project"] | slugs["person"] | slugs["source"]
    broken: list[dict] = []
    for kind, slug, _fm, body in _iter_pages():
        for m in _WIKILINK_RE.finditer(body or ""):
            target = m.group(1).strip()
            if target and target not in valid:
                broken.append({"kind": kind, "slug": slug, "target": target})
    return broken


# ── Merge (shared by Tier B dedup) ──────────────────────────

def _rewrite_slug_everywhere(old: str, new: str) -> None:
    """Rewrite [[old]] → [[new]] in bodies and replace `old` in the frontmatter
    list fields (projects/people/members/sources) of every page."""
    if old == new:
        return
    link_re = re.compile(r"\[\[" + re.escape(old) + r"(?=[\]|#])")
    list_fields = ("projects", "people", "members", "sources")
    for kind in ("project", "person", "source"):
        for entry in ks._list_pages(kind):
            page = ks._load_page(kind, entry["slug"])
            if page is None:
                continue
            fm, body = page
            changed = False
            new_body = link_re.sub(f"[[{new}", body or "")
            if new_body != body:
                body = new_body
                changed = True
            for field in list_fields:
                vals = fm.get(field)
                if isinstance(vals, list) and old in vals:
                    fm[field] = [new if v == old else v for v in vals]
                    # dedupe while preserving order
                    seen: list[str] = []
                    for v in fm[field]:
                        if v not in seen:
                            seen.append(v)
                    fm[field] = seen
                    changed = True
            if changed:
                ks._save_page(kind, entry["slug"], fm, body)


def merge_entity(kind: str, canonical: str, dup: str) -> bool:
    """Merge page `dup` into `canonical` (kind = project|person). Auto-merge:
    unions list fields + aliases, keeps canonical prose, appends dup Touchpoints,
    relinks the whole vault, deletes the dup page. Returns True on merge."""
    if kind not in ("project", "person") or canonical == dup:
        return False
    load = ks.load_project if kind == "project" else ks.load_person
    save = ks.save_project if kind == "project" else ks.save_person
    can_page = load(canonical)
    dup_page = load(dup)
    if can_page is None or dup_page is None:
        return False
    can_fm, can_body = can_page
    dup_fm, dup_body = dup_page

    # Union frontmatter list fields.
    for field in ("sources", "members", "projects"):
        merged = list(can_fm.get(field) or [])
        for v in (dup_fm.get(field) or []):
            if v not in merged:
                merged.append(v)
        if merged:
            can_fm[field] = merged
    # Fold the dup's title/aliases into the canonical aliases.
    aliases = list(can_fm.get("aliases") or [])
    for a in [dup_fm.get("title", ""), *(dup_fm.get("aliases") or [])]:
        if a and a not in aliases and a != can_fm.get("title"):
            aliases.append(a)
    can_fm["aliases"] = aliases
    can_fm["updated"] = ks._today_iso()

    # Append the dup's Touchpoints into the canonical body.
    if "## Touchpoints" in (dup_body or ""):
        tail = dup_body.split("## Touchpoints", 1)[1].strip()
        if tail:
            if "## Touchpoints" not in can_body:
                can_body = can_body.rstrip() + "\n\n## Touchpoints\n"
            can_body = can_body.rstrip() + "\n" + tail + "\n"
    save(canonical, can_fm, can_body)

    # Relink the vault, then delete the dup page.
    _rewrite_slug_everywhere(dup, canonical)
    try:
        os.remove(ks._page_path(kind, dup))
    except OSError:
        pass
    ks.append_log(f"sync merge {kind} {dup} -> {canonical}")
    return True


# ── Tier B: LLM consolidation ───────────────────────────────

_DEDUP_INSTRUCTIONS = (
    "You maintain a knowledge vault. Below are the current PROJECTS and PEOPLE "
    "as `slug — title (aliases): brief`. Identify groups that are the SAME real "
    "project or person recorded under different slugs (an abbreviation, rename, "
    "or typo). Only group entries you are confident are identical. For each "
    "group, list the best/most-complete slug FIRST (it becomes canonical).\n\n"
    "Return ONLY JSON:\n"
    '{"merge_projects": [["canonical","dup1"], ...], "merge_people": [["canonical","dup"], ...]}\n'
    "Use empty arrays if there is nothing to merge.\n\n"
)


def _entity_block(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        aliases = ", ".join(e.get("aliases", []) or [])
        alias_part = f" ({aliases})" if aliases else ""
        brief = f": {e['brief']}" if e.get("brief") else ""
        lines.append(f"- {e['slug']} — {e.get('title', e['slug'])}{alias_part}{brief}")
    return "\n".join(lines) if lines else "(none)"


async def dedup_entities(provider) -> dict:
    """LLM pass: find duplicate projects/people and auto-merge them."""
    projects = ks.list_projects()
    people = ks.list_people()
    if len(projects) + len(people) < 2:
        return {"merged_projects": 0, "merged_people": 0}

    from web.backend.ingest import _extract_json
    from web.backend.llm_service import LLMService

    prompt = (
        _DEDUP_INSTRUCTIONS
        + "PROJECTS:\n" + _entity_block(projects)
        + "\n\nPEOPLE:\n" + _entity_block(people)
    )
    try:
        response = await provider.analyze([LLMService.msg("user", prompt)], system_prompt=None)
    except Exception as e:
        logger.warning(f"dedup LLM call failed: {e}")
        return {"merged_projects": 0, "merged_people": 0, "error": str(e)}
    parsed = _extract_json(response) or {}

    merged_p = merged_pe = 0
    for group in parsed.get("merge_projects") or []:
        group = [ks.slugify(g) for g in group if g]
        if len(group) < 2:
            continue
        canonical = group[0]
        for dup in group[1:]:
            if merge_entity("project", canonical, dup):
                merged_p += 1
    for group in parsed.get("merge_people") or []:
        group = [ks.slugify(g) for g in group if g]
        if len(group) < 2:
            continue
        canonical = group[0]
        for dup in group[1:]:
            if merge_entity("person", canonical, dup):
                merged_pe += 1
    return {"merged_projects": merged_p, "merged_people": merged_pe}


async def refresh_briefs(provider) -> int:
    """Re-synthesize each project's brief + status from its source pages."""
    from web.backend.ingest import _extract_json
    from web.backend.llm_service import LLMService

    refreshed = 0
    for entry in ks._list_pages("project")[:_BRIEF_REFRESH_CAP]:
        slug = entry["slug"]
        page = ks.load_project(slug)
        if page is None:
            continue
        fm, body = page
        src_ids = fm.get("sources") or []
        if not src_ids:
            continue
        notes = []
        for sid in src_ids[-12:]:
            sp = ks.load_source(sid)
            if sp is not None:
                notes.append(sp[1][:1500])
        if not notes:
            continue
        prompt = (
            f"Project: {fm.get('title', slug)}\n"
            f"Current brief: {fm.get('brief', '') or '(none)'}\n\n"
            "Meeting notes for this project:\n" + "\n---\n".join(notes) + "\n\n"
            "Write an updated 1-2 sentence brief of this project's current state, "
            "and a one-word status (active, paused, or done). Return ONLY JSON:\n"
            '{"brief": "...", "status": "active"}'
        )
        try:
            response = await provider.analyze([LLMService.msg("user", prompt)], system_prompt=None)
        except Exception as e:
            logger.warning(f"brief refresh LLM call failed for {slug}: {e}")
            continue
        parsed = _extract_json(response) or {}
        new_brief = (parsed.get("brief") or "").strip()
        new_status = (parsed.get("status") or "").strip().lower()
        if new_brief and new_brief != fm.get("brief"):
            fm["brief"] = new_brief
            if new_status in ("active", "paused", "done"):
                fm["status"] = new_status
            fm["updated"] = ks._today_iso()
            ks.save_project(slug, fm, body)
            refreshed += 1
    return refreshed


# ── Orchestrator ────────────────────────────────────────────

async def sync_vault(provider=None, *, deep: bool = False, progress=None) -> dict:
    """Run a vault sync. Tier A always; Tier B when deep and a provider is given.

    `progress` is an optional async callable(step: str) for UI updates. Never
    raises — returns a report dict (and logs to the vault log).
    """
    report = {
        "ok": True, "deep": deep, "index_rebuilt": False, "backlinks_repaired": 0,
        "broken_links": [], "merged_projects": 0, "merged_people": 0,
        "briefs_refreshed": 0, "error": "",
    }

    async def _step(name: str):
        if progress is not None:
            try:
                await progress(name)
            except Exception:
                pass

    try:
        ks.init_vault()
        await _step("Rebuilding index")
        report["index_rebuilt"] = rebuild_index()
        await _step("Reconciling backlinks")
        report["backlinks_repaired"] = reconcile_backlinks()
        await _step("Scanning links")
        report["broken_links"] = scan_broken_wikilinks()

        if deep and provider is not None:
            await _step("Merging duplicates")
            merges = await dedup_entities(provider)
            report["merged_projects"] = merges.get("merged_projects", 0)
            report["merged_people"] = merges.get("merged_people", 0)
            await _step("Refreshing project briefs")
            report["briefs_refreshed"] = await refresh_briefs(provider)
            # A merge changes the graph — re-run the cheap deterministic passes.
            reconcile_backlinks()
            rebuild_index()

        ks.append_log(
            f"sync deep={deep} index={report['index_rebuilt']} "
            f"backlinks_repaired={report['backlinks_repaired']} "
            f"broken={len(report['broken_links'])} "
            f"merged_projects={report['merged_projects']} "
            f"merged_people={report['merged_people']} "
            f"briefs={report['briefs_refreshed']}"
        )
    except Exception as e:
        logger.error(f"sync_vault failed: {e}", exc_info=True)
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        try:
            ks.append_log(f"sync fatal: {report['error']}")
        except Exception:
            pass
    return report
