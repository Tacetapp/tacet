"""Tests for the knowledge-vault sync pass (web/backend/knowledge_sync.py).

Tier A (index/backlinks/broken-links) is deterministic; Tier B (dedup/merge)
is exercised with a FakeProvider.
"""

import importlib

import pytest


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    from web.backend import knowledge_store as ks
    importlib.reload(ks)
    from web.backend import knowledge_sync as sync
    importlib.reload(sync)
    return ks, sync


class FakeProvider:
    def __init__(self, response: str):
        self.response = response

    async def analyze(self, messages, system_prompt=None):
        return self.response


def _seed_source(ks, sid, projects, people, body="## Summary\n\nnotes\n"):
    ks.init_vault()
    for ps in projects:
        ks.ensure_project(ps, title=ps.replace("-", " ").title())
    for pe in people:
        ks.ensure_person(pe, title=pe.replace("-", " ").title())
    ks.write_source(sid, {
        "title": "Meeting", "type": "source", "session_id": sid,
        "projects": projects, "people": people,
    }, body)


# ── Tier A ──────────────────────────────────────────────────

def test_reconcile_backlinks_rebuilds_from_sources(vault):
    ks, sync = vault
    _seed_source(ks, "2026-01-01-000000", ["proj-a"], ["alice"])
    # Fresh project/person pages have empty derived lists.
    assert (ks.load_project("proj-a")[0].get("sources") or []) == []

    repaired = sync.reconcile_backlinks()
    assert repaired >= 1

    pfm, _ = ks.load_project("proj-a")
    assert "2026-01-01-000000" in pfm["sources"]
    assert "alice" in pfm["members"]
    afm, _ = ks.load_person("alice")
    assert "proj-a" in afm["projects"]
    assert "2026-01-01-000000" in afm["sources"]


def test_rebuild_index_lists_all_pages(vault):
    ks, sync = vault
    _seed_source(ks, "2026-01-01-000000", ["proj-a"], ["alice"])
    sync.rebuild_index()
    idx = open(ks.INDEX_MD, encoding="utf-8").read()
    assert "[[proj-a]]" in idx
    assert "[[alice]]" in idx
    assert "2026-01-01-000000" in idx


def test_scan_broken_wikilinks(vault):
    ks, sync = vault
    ks.ensure_project("proj-a", title="A")
    ks.write_source("2026-01-01-000000", {
        "title": "M", "session_id": "2026-01-01-000000",
        "projects": ["proj-a"], "people": [],
    }, "## Summary\n\nSee [[ghost-project]] and [[proj-a]].\n")
    broken = sync.scan_broken_wikilinks()
    targets = {b["target"] for b in broken}
    assert "ghost-project" in targets
    assert "proj-a" not in targets


# ── Merge / dedup (Tier B) ──────────────────────────────────

def test_merge_entity_relinks_and_deletes_dup(vault):
    ks, sync = vault
    ks.ensure_project("project-foo", title="Project Foo")
    ks.ensure_project("foo", title="Foo")
    ks.write_source("2026-01-01-000000", {
        "title": "M", "session_id": "2026-01-01-000000",
        "projects": ["foo"], "people": [],
    }, "## Summary\n\nWork on [[foo]].\n")

    assert sync.merge_entity("project", "project-foo", "foo") is True
    assert ks.load_project("foo") is None                    # dup gone

    can_fm, _ = ks.load_project("project-foo")
    assert "Foo" in (can_fm.get("aliases") or [])            # dup title folded in

    s_fm, s_body = ks.load_source("2026-01-01-000000")
    assert "[[project-foo]]" in s_body and "[[foo]]" not in s_body  # body relinked
    assert s_fm["projects"] == ["project-foo"]               # frontmatter relinked


@pytest.mark.asyncio
async def test_dedup_entities_auto_merges(vault):
    ks, sync = vault
    ks.ensure_project("project-foo", title="Project Foo")
    ks.ensure_project("foo", title="Foo")
    resp = '```json\n{"merge_projects": [["project-foo","foo"]], "merge_people": []}\n```'
    result = await sync.dedup_entities(FakeProvider(resp))
    assert result["merged_projects"] == 1
    assert ks.load_project("foo") is None


# ── Orchestrator ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_vault_tier_a_only(vault):
    ks, sync = vault
    _seed_source(ks, "2026-01-01-000000", ["proj-a"], ["alice"])
    report = await sync.sync_vault(deep=False)
    assert report["ok"] and report["index_rebuilt"]
    assert report["backlinks_repaired"] >= 1
    assert ks.load_project("proj-a")[0]["sources"] == ["2026-01-01-000000"]


@pytest.mark.asyncio
async def test_sync_vault_deep_merges(vault):
    ks, sync = vault
    ks.ensure_project("project-foo", title="Project Foo")
    ks.ensure_project("foo", title="Foo")
    resp = '{"merge_projects": [["project-foo","foo"]], "merge_people": []}'
    report = await sync.sync_vault(FakeProvider(resp), deep=True)
    assert report["ok"]
    assert report["merged_projects"] == 1
    assert ks.load_project("foo") is None
