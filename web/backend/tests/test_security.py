"""Security hardening tests (§4):

- WS token auth: accept with correct token, reject with wrong/missing token (§4.1).
- fs_write containment: only vault/outputs/ is writable (§4.3).
- Slug validation: reject slugs that don't match ^[a-z0-9][a-z0-9-]{0,63}$ (§4.3).
"""

import importlib
import os

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registries():
    from web.backend import main as main_mod
    yield
    # A single ClientSession registry replaces the old parallel dicts (§6).
    main_mod._sessions.clear()
    main_mod._active_websockets.clear()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Reload knowledge_store with KNOWLEDGE_DIR pointing at a temp dir."""
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("MEETINGS_DIR", str(tmp_path / "meetings"))
    from web.backend import knowledge_store as ks
    importlib.reload(ks)
    ks.init_vault()
    return ks


@pytest.fixture
def ingest_modules(tmp_path, monkeypatch):
    """Reload ks + ss + ingest with isolated dirs."""
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("MEETINGS_DIR", str(tmp_path / "meetings"))
    from web.backend import knowledge_store as ks
    from web.backend import session_store as ss
    from web.backend import ingest as ig
    importlib.reload(ks)
    importlib.reload(ss)
    importlib.reload(ig)
    return ks, ss, ig


# ── §4.1 WebSocket token auth ────────────────────────────────────────────────


def test_ws_accepted_with_correct_token(monkeypatch):
    """A connection carrying the correct ?token= query param is accepted."""
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.setattr(main_mod, "WS_AUTH_TOKEN", "test-secret-token-abc")
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws?token=test-secret-token-abc") as ws:
        ws.send_json({"type": "ping", "client_id": "tok-accept-test"})
        msg = ws.receive_json()
        # Server responds (auth_status or pong) — either proves the connection
        # was accepted rather than rejected.
        assert msg.get("type") in ("auth_status", "pong")


def test_ws_rejected_with_wrong_token(monkeypatch):
    """A connection with an incorrect token is closed with code 4001."""
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.setattr(main_mod, "WS_AUTH_TOKEN", "real-secret")
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    client = TestClient(main_mod.app)
    with pytest.raises(Exception):
        # TestClient raises when the server closes the WS before the handshake
        # completes (close code 4001 = Unauthorized).
        with client.websocket_connect("/ws?token=wrong-token") as ws:
            ws.receive_json()


def test_ws_rejected_with_no_token(monkeypatch):
    """A connection with no token is rejected when WS_AUTH_TOKEN is configured."""
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.setattr(main_mod, "WS_AUTH_TOKEN", "required-token")
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    client = TestClient(main_mod.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_ws_accepted_no_auth_when_token_not_set(monkeypatch):
    """When WS_AUTH_TOKEN is empty, any connection is accepted (no-auth mode)."""
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.setattr(main_mod, "WS_AUTH_TOKEN", "")
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping", "client_id": "no-auth-test"})
        msg = ws.receive_json()
        assert msg.get("type") in ("auth_status", "pong")


# ── §4.3 fs_write outputs/ containment ──────────────────────────────────────


def test_fs_write_allowed_under_outputs(vault):
    """_fs_write accepts a path inside vault/outputs/."""
    from web.backend.engine import _fs_write

    target = os.path.join(vault.KNOWLEDGE_DIR, "outputs", "test-brief.md")
    result = _fs_write(target, "# Brief\n\nSome content.")
    assert result.get("ok") is True
    assert os.path.isfile(target)
    with open(target, encoding="utf-8") as f:
        assert "# Brief" in f.read()


def test_fs_write_denied_at_vault_root(vault):
    """_fs_write denies writes to the vault root (not inside outputs/)."""
    from web.backend.engine import _fs_write

    # Use a fresh file that doesn't exist yet (init_vault creates AGENTS.md;
    # pick a different path so the assertion isn't confused by pre-existing files).
    target = os.path.join(vault.KNOWLEDGE_DIR, "injected-root-file.md")
    result = _fs_write(target, "# Injected")
    assert "error" in result
    assert "denied" in result["error"].lower()
    # File must not have been created.
    assert not os.path.isfile(target)


def test_fs_write_denied_to_project_page(vault):
    """_fs_write denies writes to projects/ inside the vault."""
    from web.backend.engine import _fs_write

    target = os.path.join(vault.KNOWLEDGE_DIR, "projects", "my-project.md")
    result = _fs_write(target, "# Injected project")
    assert "error" in result
    assert not os.path.isfile(target)


def test_fs_write_denied_outside_vault(vault, tmp_path):
    """_fs_write denies writes to a path completely outside the vault."""
    from web.backend.engine import _fs_write

    target = str(tmp_path / "escape.md")
    result = _fs_write(target, "escaped content")
    assert "error" in result
    assert not os.path.isfile(target)


def test_fs_write_denied_via_path_traversal(vault):
    """_fs_write denies a ../traversal that escapes the outputs/ root."""
    from web.backend.engine import _fs_write

    # Try to escape outputs/ via ../new-file.md — realpath collapses this to the
    # vault root, which is outside outputs/, so it must be denied.
    target = os.path.join(vault.KNOWLEDGE_DIR, "outputs", "..", "escaped-inject.md")
    result = _fs_write(target, "injected via traversal")
    assert "error" in result
    assert "denied" in result["error"].lower()
    # The resolved target (vault root) must not have been written.
    escaped_path = os.path.join(vault.KNOWLEDGE_DIR, "escaped-inject.md")
    assert not os.path.isfile(escaped_path)


# ── §4.3 Slug validation ─────────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, response: str):
        self.response = response

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        return self.response

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        yield self.response

    async def converse_stream_with_tools(self, *args, **kwargs):
        return self.response, []


def _seed_session(ss, session_id="2026-06-01-120000"):
    ss.save_session({
        "id": session_id,
        "started_at": "2026-06-01T12:00:00",
        "ended_at": "2026-06-01T13:00:00",
        "segment_count": 1,
        "segments": [
            {"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
             "text": "Test content.", "speaker": "You"},
        ],
        "insights": [],
        "explained_terms": [],
        "chat_history": [],
        "summary": "Test meeting.",
        "projects": [],
        "meeting_context": {
            "title": "Test Meeting",
            "attendees": [],
            "agenda": "",
            "start_time": "", "end_time": "",
        },
    })
    return session_id


def _make_response(proj_slug: str, person_slug: str = "alice-chen") -> str:
    return f"""
```json
{{
  "project": {{
    "match": "new",
    "slug": "{proj_slug}",
    "title": "Test Project",
    "brief": "A test project.",
    "refresh_brief": false
  }},
  "people": [
    {{"match": "new", "slug": "{person_slug}", "title": "Alice Chen", "aliases": []}}
  ],
  "source_page": {{
    "summary": "Test meeting summary.",
    "claims": [],
    "decisions": [],
    "action_items": [],
    "open_questions": []
  }},
  "touchpoint": "2026-06-01 — [[2026-06-01-120000]] — Test."
}}
```
"""


@pytest.mark.asyncio
async def test_slug_valid_accepted(ingest_modules):
    """A valid slug like 'my-project' is accepted."""
    ks, ss, ig = ingest_modules
    sid = _seed_session(ss)
    provider = _FakeProvider(_make_response("my-project"))
    result = await ig.ingest_session(sid, provider)
    assert result["ok"] is True
    assert result["project_slug"] == "my-project"


def test_is_valid_slug_accepts_well_formed():
    """_is_valid_slug returns True for well-formed slugs."""
    from web.backend.ingest import _is_valid_slug

    valid = ["project-foo", "alice-chen", "a", "z0", "123", "my-big-project",
             "a" * 64, "0abc", "abc-123-def"]
    for s in valid:
        assert _is_valid_slug(s), f"expected valid: {s!r}"


def test_is_valid_slug_rejects_bad_patterns():
    """_is_valid_slug returns False for slugs with invalid patterns."""
    from web.backend.ingest import _is_valid_slug

    invalid = [
        "",               # empty
        "-starts-hyphen", # leading hyphen
        "a" * 65,         # too long (>64 chars)
        "has spaces",     # spaces
        "UPPERCASE",      # uppercase
        "has.dot",        # period
        "has/slash",      # slash
    ]
    for s in invalid:
        assert not _is_valid_slug(s), f"expected invalid: {s!r}"


@pytest.mark.asyncio
async def test_slug_traversal_normalized_safely(ingest_modules):
    """ks.slugify normalizes '../evil-path' to 'evil-path' so the ingest
    proceeds safely — the traversal characters never reach the filesystem."""
    ks, ss, ig = ingest_modules
    sid = _seed_session(ss)
    # '../evil-path' after slugify → 'evil-path' (valid, safe)
    response_with_traversal_slug = """
```json
{
  "project": {
    "match": "new",
    "slug": "../evil-path",
    "title": "Safe Project",
    "brief": "Traversal chars stripped by slugify.",
    "refresh_brief": false
  },
  "people": [],
  "source_page": {"summary": "x", "claims": [], "decisions": [],
                  "action_items": [], "open_questions": []},
  "touchpoint": "2026-06-01 — [[2026-06-01-120000]] — x."
}
```
"""
    provider = _FakeProvider(response_with_traversal_slug)
    result = await ig.ingest_session(sid, provider)
    # Slugify normalized the slug to 'evil-path' (valid); ingest proceeds.
    assert result["ok"] is True
    assert result["project_slug"] == "evil-path"
    # The project was created at the safe path, never at any parent-dir path.
    proj = ks.load_project("evil-path")
    assert proj is not None


@pytest.mark.asyncio
async def test_empty_slug_rejected(ingest_modules):
    """An all-special-char slug (normalizes to empty) is caught before vault writes."""
    ks, ss, ig = ingest_modules
    sid = _seed_session(ss)
    # All-hyphen slug normalizes to '' after slugify → "project.slug missing"
    bad_response = """
```json
{
  "project": {
    "match": "new",
    "slug": "---",
    "title": "Bad Slug Project",
    "brief": "Bad.",
    "refresh_brief": false
  },
  "people": [],
  "source_page": {"summary": "x", "claims": [], "decisions": [],
                  "action_items": [], "open_questions": []},
  "touchpoint": "2026-06-01 — [[2026-06-01-120000]] — x."
}
```
"""
    provider = _FakeProvider(bad_response)
    result = await ig.ingest_session(sid, provider)
    assert result["ok"] is False
    assert "slug" in result["error"].lower() or "missing" in result["error"].lower()


@pytest.mark.asyncio
async def test_person_with_empty_slug_skipped_project_proceeds(ingest_modules):
    """A person whose slug normalizes to empty is skipped; project still ingests."""
    ks, ss, ig = ingest_modules
    sid = _seed_session(ss)
    # Valid project slug; person slug that normalizes to empty.
    response = """
```json
{
  "project": {
    "match": "new",
    "slug": "valid-project",
    "title": "Valid Project",
    "brief": "A valid project.",
    "refresh_brief": false
  },
  "people": [
    {"match": "new", "slug": "---", "title": "Bad Actor", "aliases": []}
  ],
  "source_page": {"summary": "Meeting summary.", "claims": [], "decisions": [],
                  "action_items": [], "open_questions": []},
  "touchpoint": "2026-06-01 — [[2026-06-01-120000]] — Test."
}
```
"""
    provider = _FakeProvider(response)
    result = await ig.ingest_session(sid, provider)
    # Project should succeed; the empty-slug person is silently skipped.
    assert result["ok"] is True
    assert result["project_slug"] == "valid-project"
    # No person page for an empty slug.
    assert result.get("people") == []
