"""Tests for knowledge_store — vault scaffolding + page I/O."""

import importlib
import os

import pytest


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Reload knowledge_store with KNOWLEDGE_DIR pointed at a temp dir.

    Module-level path constants are computed at import time, so we need to
    re-import after setting the env var to make a fresh vault per test.
    """
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    from web.backend import knowledge_store as ks
    importlib.reload(ks)
    return ks


def test_init_vault_creates_layout(vault):
    root = vault.init_vault()
    assert os.path.isdir(root)
    for sub in ("projects", "people", "sources", "outputs"):
        assert os.path.isdir(os.path.join(root, sub))
    assert os.path.isfile(os.path.join(root, "AGENTS.md"))
    assert os.path.isfile(os.path.join(root, "index.md"))
    assert os.path.isfile(os.path.join(root, "log.md"))


def test_init_vault_idempotent(vault):
    vault.init_vault()
    # Mutate AGENTS.md and re-init — content must be preserved.
    agents = os.path.join(vault.KNOWLEDGE_DIR, "AGENTS.md")
    with open(agents, "w", encoding="utf-8") as f:
        f.write("custom content")
    vault.init_vault()
    with open(agents, encoding="utf-8") as f:
        assert f.read() == "custom content"


def test_slugify_rules(vault):
    assert vault.slugify("Project Foo Bar") == "project-foo-bar"
    assert vault.slugify("ACE Energy — Migration!") == "ace-energy-migration"
    assert vault.slugify("") == ""
    # Long input is truncated at the last word boundary that fits.
    long_in = "x" * 200
    assert len(vault.slugify(long_in)) <= 60


def test_project_save_load_roundtrip(vault):
    fm = {
        "title": "Project Foo",
        "type": "project",
        "status": "active",
        "slug": "project-foo",
        "created": "2026-05-18",
        "updated": "2026-05-18",
        "aliases": ["Foo Migration"],
        "members": [],
        "sources": [],
        "brief": "A test project.",
        "tags": [],
    }
    body = "## Brief\n\nA test project.\n\n## Touchpoints\n\n"
    path = vault.save_project("project-foo", fm, body)
    assert os.path.isfile(path)

    loaded = vault.load_project("project-foo")
    assert loaded is not None
    loaded_fm, loaded_body = loaded
    assert loaded_fm["title"] == "Project Foo"
    assert loaded_fm["aliases"] == ["Foo Migration"]
    assert "## Brief" in loaded_body


def test_list_projects(vault):
    vault.ensure_project("project-foo", title="Project Foo", brief="Foo brief")
    vault.ensure_project("project-bar", title="Project Bar")

    projects = vault.list_projects()
    slugs = sorted(p["slug"] for p in projects)
    assert slugs == ["project-bar", "project-foo"]
    foo = next(p for p in projects if p["slug"] == "project-foo")
    assert foo["title"] == "Project Foo"
    assert foo["brief"] == "Foo brief"


def test_ensure_project_does_not_overwrite(vault):
    fm1, _ = vault.ensure_project("project-foo", title="Project Foo")
    # Second call with a different title leaves the existing page untouched.
    fm2, _ = vault.ensure_project("project-foo", title="Different Name")
    assert fm2["title"] == "Project Foo"


def test_person_save_and_add_session(vault):
    vault.ensure_person("alice-chen", title="Alice Chen", aliases=["Alice"])
    vault.add_session_to_person(
        "alice-chen", "2026-05-18-153412", project_slugs=["project-foo"],
    )
    page = vault.load_person("alice-chen")
    assert page is not None
    fm, _ = page
    assert "2026-05-18-153412" in fm["sources"]
    assert "project-foo" in fm["projects"]

    # Idempotent: second call doesn't duplicate.
    vault.add_session_to_person(
        "alice-chen", "2026-05-18-153412", project_slugs=["project-foo"],
    )
    fm2, _ = vault.load_person("alice-chen")
    assert fm2["sources"].count("2026-05-18-153412") == 1
    assert fm2["projects"].count("project-foo") == 1


def test_source_write_and_load(vault):
    fm = {
        "title": "Q2 Planning Sync",
        "type": "source",
        "subtype": "meeting",
        "session_id": "2026-05-18-153412",
        "meeting_path": "../../meetings/2026-05-18-153412.md",
        "started_at": "2026-05-18T15:34:12",
        "projects": ["project-foo"],
        "people": ["alice-chen"],
        "created": "2026-05-18",
        "updated": "2026-05-18",
    }
    body = "## Summary\n\nDiscussed [[project-foo]] with [[alice-chen]].\n"
    vault.write_source("2026-05-18-153412", fm, body)

    loaded = vault.load_source("2026-05-18-153412")
    assert loaded is not None
    loaded_fm, loaded_body = loaded
    assert loaded_fm["projects"] == ["project-foo"]
    assert "[[project-foo]]" in loaded_body


def test_append_touchpoint(vault):
    vault.ensure_project("project-foo", title="Project Foo")
    vault.append_touchpoint(
        "project-foo", "2026-05-18 — [[2026-05-18-153412]] — Decided to migrate.",
    )
    fm, body = vault.load_project("project-foo")
    assert "## Touchpoints" in body
    assert "Decided to migrate" in body

    # Second append goes under the same section, not a new one.
    vault.append_touchpoint(
        "project-foo", "2026-05-19 — [[2026-05-19-101010]] — Drafted RFC.",
    )
    _, body2 = vault.load_project("project-foo")
    assert body2.count("## Touchpoints") == 1
    assert "Drafted RFC" in body2


def test_add_session_to_project_idempotent(vault):
    vault.ensure_project("project-foo", title="Project Foo")
    vault.add_session_to_project("project-foo", "2026-05-18-153412")
    vault.add_session_to_project("project-foo", "2026-05-18-153412")
    fm, _ = vault.load_project("project-foo")
    assert fm["sources"] == ["2026-05-18-153412"]


def test_write_output_safety(vault):
    path = vault.write_output("project-foo-brief-2026-05-18.md", "# Brief\n\nbody\n")
    assert path.startswith(vault.OUTPUTS_DIR)
    with open(path, encoding="utf-8") as f:
        assert "# Brief" in f.read()

    with pytest.raises(ValueError):
        vault.write_output("../escape.md", "x")
    with pytest.raises(ValueError):
        vault.write_output(".hidden", "x")


def test_append_log(vault):
    vault.append_log("ingest project-foo session=2026-05-18-153412 ok")
    log_path = os.path.join(vault.KNOWLEDGE_DIR, "log.md")
    with open(log_path, encoding="utf-8") as f:
        content = f.read()
    assert "ingest project-foo" in content
    # Header preserved across appends.
    assert content.startswith("# Vault log")


def test_session_store_projects_field_roundtrip(tmp_path, monkeypatch):
    """session_store must round-trip the new `projects` frontmatter field."""
    monkeypatch.setenv("MEETINGS_DIR", str(tmp_path / "meetings"))
    from web.backend import session_store as ss
    importlib.reload(ss)

    sid = "2026-05-18-153412"
    data = {
        "id": sid,
        "started_at": "2026-05-18T15:34:12",
        "ended_at": "2026-05-18T16:00:00",
        "segment_count": 2,
        "segments": [
            {"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
             "text": "hello", "speaker": "You"},
        ],
        "insights": [],
        "explained_terms": [],
        "chat_history": [],
        "summary": "test summary",
        "projects": ["project-foo"],
        "meeting_context": {
            "title": "Test Meeting",
            "attendees": ["Alice"],
            "agenda": "",
            "start_time": "",
            "end_time": "",
        },
    }
    path = ss.save_session(data)
    assert os.path.isfile(path)

    loaded = ss.load_session(sid)
    assert loaded is not None
    assert loaded["projects"] == ["project-foo"]


def test_update_session_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETINGS_DIR", str(tmp_path / "meetings"))
    from web.backend import session_store as ss
    importlib.reload(ss)

    sid = "2026-05-18-153412"
    ss.save_session({
        "id": sid,
        "started_at": "2026-05-18T15:34:12",
        "segment_count": 0,
        "segments": [],
        "insights": [],
        "explained_terms": [],
        "chat_history": [],
        "summary": "",
        "projects": [],
        "meeting_context": {"title": "Test", "attendees": [], "agenda": "",
                             "start_time": "", "end_time": ""},
    })

    result = ss.update_session_projects(sid, ["project-foo", "project-bar"])
    assert result.get("success") is True

    loaded = ss.load_session(sid)
    assert loaded["projects"] == ["project-foo", "project-bar"]

    # Unknown session -> error, no crash.
    err = ss.update_session_projects("nonexistent", ["x"])
    assert "error" in err
