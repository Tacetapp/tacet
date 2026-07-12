"""Tests for the ingest pipeline (web/backend/ingest.py).

Uses a fake InferenceProvider so the LLM call is deterministic. Verifies:
- well-formed JSON response → source page, project page, person page,
  touchpoint, and the meeting frontmatter all get the right shape.
- bad-JSON response → no crash, log entry, no vault corruption.
- empty session is skipped.
- existing-project guard (skip unless force=True).
"""

import importlib
import json
import os

import pytest


@pytest.fixture
def vault_and_meetings(tmp_path, monkeypatch):
    """Reload knowledge_store + session_store + ingest with isolated dirs."""
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("MEETINGS_DIR", str(tmp_path / "meetings"))
    from web.backend import knowledge_store as ks
    from web.backend import session_store as ss
    from web.backend import ingest as ig
    importlib.reload(ks)
    importlib.reload(ss)
    importlib.reload(ig)
    return ks, ss, ig


class FakeProvider:
    """Minimal InferenceProvider stand-in. analyze() returns a fixed string."""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        self.calls.append({"messages": messages, "system_prompt": system_prompt,
                           "max_tokens": max_tokens})
        return self.response

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        yield self.response

    async def converse_stream_with_tools(self, *args, **kwargs):
        return self.response, []


def _seed_session(ss, session_id="2026-05-18-153412", title="Q2 Planning Sync"):
    """Save a minimal session to disk so ingest_session() can find it."""
    ss.save_session({
        "id": session_id,
        "started_at": "2026-05-18T15:34:12",
        "ended_at": "2026-05-18T16:00:00",
        "segment_count": 3,
        "segments": [
            {"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
             "text": "Let's plan Q2 for Project Foo.", "speaker": "Alice"},
            {"index": 2, "start": "00:00:02,000", "end": "00:00:05,000",
             "text": "We should migrate the API by July.", "speaker": "You"},
            {"index": 3, "start": "00:00:05,000", "end": "00:00:08,000",
             "text": "Action: I'll draft the RFC.", "speaker": "Alice"},
        ],
        "insights": [
            {"id": 0, "category": "key_point",
             "text": "Migration target date is July."},
        ],
        "explained_terms": [],
        "chat_history": [],
        "summary": "Planned Q2 work on Project Foo, agreed on a July migration target.",
        "projects": [],
        "meeting_context": {
            "title": title,
            "attendees": ["Alice Chen", "Bob Jones"],
            "agenda": "Q2 planning",
            "start_time": "", "end_time": "",
        },
    })
    return session_id


GOOD_RESPONSE = """Here's the ingest payload:

```json
{
  "project": {
    "match": "new",
    "slug": "project-foo",
    "title": "Project Foo",
    "brief": "Migration of the Foo API by Q3.",
    "refresh_brief": false
  },
  "people": [
    {"match": "new", "slug": "alice-chen", "title": "Alice Chen", "aliases": ["Alice"]},
    {"match": "new", "slug": "bob-jones", "title": "Bob Jones", "aliases": []}
  ],
  "source_page": {
    "summary": "Planned Q2 milestones for [[project-foo]].",
    "claims": ["Target migration by July.", "Alice owns the RFC."],
    "decisions": ["Proceed with the July migration target."],
    "action_items": ["[[alice-chen]] — draft RFC by Friday."],
    "open_questions": ["Who reviews the RFC?"]
  },
  "touchpoint": "2026-05-18 — [[2026-05-18-153412]] — Agreed on July migration target."
}
```
"""


@pytest.mark.asyncio
async def test_ingest_good_response_writes_full_vault(vault_and_meetings):
    ks, ss, ig = vault_and_meetings
    sid = _seed_session(ss)

    provider = FakeProvider(GOOD_RESPONSE)
    result = await ig.ingest_session(sid, provider)

    assert result["ok"] is True
    assert result["project_slug"] == "project-foo"
    assert "alice-chen" in result["people"]

    # Source page exists with the expected sections.
    src = ks.load_source(sid)
    assert src is not None
    src_fm, src_body = src
    assert src_fm["session_id"] == sid
    assert src_fm["projects"] == ["project-foo"]
    assert "alice-chen" in src_fm["people"]
    assert "## Summary" in src_body
    assert "## Claims" in src_body
    assert "[[project-foo]]" in src_body
    assert "draft RFC" in src_body

    # Project page exists with the touchpoint appended.
    proj = ks.load_project("project-foo")
    assert proj is not None
    proj_fm, proj_body = proj
    assert proj_fm["title"] == "Project Foo"
    assert sid in proj_fm["sources"]
    assert "## Touchpoints" in proj_body
    assert "Agreed on July migration target" in proj_body

    # People pages exist with backlinks to the project + session.
    alice = ks.load_person("alice-chen")
    assert alice is not None
    alice_fm, _ = alice
    assert "project-foo" in alice_fm["projects"]
    assert sid in alice_fm["sources"]

    # Meeting frontmatter now carries the project slug.
    loaded = ss.load_session(sid)
    assert loaded["projects"] == ["project-foo"]

    # Log got an entry.
    with open(os.path.join(ks.KNOWLEDGE_DIR, "log.md"), encoding="utf-8") as f:
        log = f.read()
    assert "ingest" in log and "project-foo" in log


@pytest.mark.asyncio
async def test_ingest_bad_json_logs_and_does_not_crash(vault_and_meetings):
    ks, ss, ig = vault_and_meetings
    sid = _seed_session(ss)

    provider = FakeProvider("Sorry, I couldn't parse that meeting. No JSON here.")
    result = await ig.ingest_session(sid, provider)

    assert result["ok"] is False
    assert "JSON" in result["error"] or "parse" in result["error"].lower()

    # No project / source page / person pages were created.
    assert ks.list_projects() == []
    assert ks.load_source(sid) is None

    # Meeting frontmatter is unchanged.
    loaded = ss.load_session(sid)
    assert loaded["projects"] == []

    # Log captured the parse error.
    with open(os.path.join(ks.KNOWLEDGE_DIR, "log.md"), encoding="utf-8") as f:
        log = f.read()
    assert "parse_error" in log


@pytest.mark.asyncio
async def test_ingest_skipped_when_already_assigned(vault_and_meetings):
    ks, ss, ig = vault_and_meetings
    sid = _seed_session(ss)
    ss.update_session_projects(sid, ["existing-project"])

    provider = FakeProvider(GOOD_RESPONSE)
    result = await ig.ingest_session(sid, provider, force=False)

    assert result["ok"] is True
    assert result.get("skipped") is True
    # Provider was never called.
    assert provider.calls == []
    # Vault still empty.
    assert ks.list_projects() == []


@pytest.mark.asyncio
async def test_reingest_forces_apply(vault_and_meetings):
    ks, ss, ig = vault_and_meetings
    sid = _seed_session(ss)
    ss.update_session_projects(sid, ["existing-project"])

    provider = FakeProvider(GOOD_RESPONSE)
    result = await ig.reingest_session(sid, provider)

    assert result["ok"] is True
    assert result["project_slug"] == "project-foo"
    # The newer assignment overwrites the previous projects field.
    loaded = ss.load_session(sid)
    assert loaded["projects"] == ["project-foo"]


@pytest.mark.asyncio
async def test_ingest_empty_session_skipped(vault_and_meetings):
    ks, ss, ig = vault_and_meetings
    sid = "2026-05-18-000000"
    ss.save_session({
        "id": sid, "started_at": "", "ended_at": "", "segment_count": 0,
        "segments": [], "insights": [], "explained_terms": [],
        "chat_history": [], "summary": "", "projects": [],
        "meeting_context": {"title": "Empty", "attendees": [], "agenda": "",
                             "start_time": "", "end_time": ""},
    })
    provider = FakeProvider(GOOD_RESPONSE)
    result = await ig.ingest_session(sid, provider)
    assert result["ok"] is False
    assert "empty" in result["error"].lower()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_ingest_missing_session(vault_and_meetings):
    _, _, ig = vault_and_meetings
    provider = FakeProvider(GOOD_RESPONSE)
    result = await ig.ingest_session("not-a-real-id", provider)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert provider.calls == []


class InvalidFastProvider:
    """Fast-tier stand-in whose model ID is invalid — analyze() always raises."""

    def __init__(self):
        self.calls = 0

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        from web.backend.llm_service import InvalidModelError
        self.calls += 1
        raise InvalidModelError("us.anthropic.claude-haiku-4-5")

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        yield ""

    async def converse_stream_with_tools(self, *a, **k):
        return "", []


@pytest.mark.asyncio
async def test_ingest_retries_on_primary_when_fast_model_invalid(vault_and_meetings, monkeypatch):
    """When the fast-tier provider's model ID is invalid, ingest retries once on a
    fresh primary-tier service (create_llm_service(fast=False)) and succeeds."""
    ks, ss, ig = vault_and_meetings
    sid = _seed_session(ss)

    fast = InvalidFastProvider()
    primary = FakeProvider(GOOD_RESPONSE)
    # create_llm_service(fast=False) inside ingest returns the primary stand-in.
    monkeypatch.setattr(ig, "create_llm_service", lambda fast=False: primary)

    result = await ig.ingest_session(sid, fast)

    assert result["ok"] is True
    assert result["project_slug"] == "project-foo"
    assert fast.calls == 1            # fast tier tried once and was rejected
    assert len(primary.calls) == 1    # retried on the primary-tier service

    # The fallback was recorded in the vault log.
    with open(os.path.join(ks.KNOWLEDGE_DIR, "log.md"), encoding="utf-8") as f:
        log = f.read()
    assert "fast_model_invalid" in log


def test_extract_json_lenient(vault_and_meetings):
    _, _, ig = vault_and_meetings
    # Fenced ```json
    assert ig._extract_json('pre\n```json\n{"a": 1}\n```\npost') == {"a": 1}
    # Fenced bare ```
    assert ig._extract_json('```\n{"a": 2}\n```') == {"a": 2}
    # Bare object
    assert ig._extract_json('Sure: {"a": 3} done') == {"a": 3}
    # No JSON
    assert ig._extract_json("nothing here") is None
    # Truncated
    assert ig._extract_json('{"a": 1') is None
