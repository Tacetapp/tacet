"""WS-server lifecycle guards: double-start refusal (§2.2) and idle-eviction
skip/persist behavior (§2.3)."""

import time
import pytest

from web.backend import main as main_mod
from web.backend.engine import MeetingEngine
from web.backend.srt_parser import SrtSegment


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    # A single ClientSession registry replaces the old parallel dicts (§6).
    main_mod._sessions.clear()
    main_mod._active_websockets.clear()


def test_double_start_refused(monkeypatch):
    """A `start` for a client that already has a live engine is refused and does
    not build a new engine/capture."""
    from fastapi.testclient import TestClient

    called = {"n": 0}

    async def fake_start(session, emit):
        called["n"] += 1

    # Keep the handler hermetic (no boto3/region, no capture spawn).
    monkeypatch.setattr(main_mod, "_handle_start", fake_start)
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    cid = "test-double-start"
    eng = MeetingEngine(llm=None, emit=None)
    eng.active = True
    main_mod._sessions[cid] = main_mod.ClientSession(client_id=cid, engine=eng)

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start", "client_id": cid})
        error = None
        for _ in range(6):
            msg = ws.receive_json()
            if msg.get("type") == "error":
                error = msg
                break
        assert error is not None
        assert error["text"] == "Meeting already active"
    assert called["n"] == 0  # no new engine/capture was started


def test_ws_survives_llm_provider_construction_failure(monkeypatch):
    """Provider construction failure (botocore NoRegionError) must NOT kill the WS
    receive loop — the production 66× reconnect storm. The offending message is
    skipped with a clear error, and the socket still answers a later ping."""
    from fastapi.testclient import TestClient
    from botocore.exceptions import NoRegionError

    def boom(*a, **k):
        raise NoRegionError()  # "You must specify a region."

    # _get_or_create_llm references the module-global factory, so patch it here.
    monkeypatch.setattr(main_mod, "create_llm_service", boom)

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws") as ws:
        # A "chat" message triggers eager provider construction before dispatch.
        ws.send_json({"type": "chat", "client_id": "ws-resilience", "text": "hi"})
        error = None
        for _ in range(8):
            msg = ws.receive_json()
            if msg.get("type") == "error":
                error = msg
                break
        assert error is not None
        assert "LLM provider not configured" in error["text"]
        assert "Settings" in error["text"]

        # The loop is still alive — a subsequent ping still gets a pong.
        ws.send_json({"type": "ping", "client_id": "ws-resilience"})
        pong = None
        for _ in range(8):
            msg = ws.receive_json()
            if msg.get("type") == "pong":
                pong = msg
                break
        assert pong is not None


@pytest.mark.asyncio
async def test_eviction_skips_active_and_paused(monkeypatch):
    monkeypatch.setattr(main_mod, "CLIENT_TTL_SECONDS", 0)
    active = MeetingEngine(llm=None, emit=None)
    active.active = True
    paused = MeetingEngine(llm=None, emit=None)
    paused.active = True
    paused.paused = True
    main_mod._sessions["a"] = main_mod.ClientSession(
        client_id="a", engine=active, last_activity=time.monotonic() - 10)
    main_mod._sessions["p"] = main_mod.ClientSession(
        client_id="p", engine=paused, last_activity=time.monotonic() - 10)

    await main_mod._evict_idle_clients()

    assert "a" in main_mod._sessions  # live meeting preserved
    assert "p" in main_mod._sessions  # paused meeting preserved


@pytest.mark.asyncio
async def test_eviction_persists_unsaved_stopped_engine(monkeypatch):
    monkeypatch.setattr(main_mod, "CLIENT_TTL_SECONDS", 0)
    eng = MeetingEngine(llm=None, emit=None)
    eng.active = False
    eng.segments = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "hi")]
    saved = {"n": 0}
    monkeypatch.setattr(eng, "_save_session", lambda: saved.__setitem__("n", saved["n"] + 1))
    main_mod._sessions["s"] = main_mod.ClientSession(
        client_id="s", engine=eng, last_activity=time.monotonic() - 10)

    await main_mod._evict_idle_clients()

    assert saved["n"] == 1        # unsaved segments were persisted first
    assert "s" not in main_mod._sessions
