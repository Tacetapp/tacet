"""Persistent speaker registry + vault-vocab tests (§7).

Covers the opt-in "known voices" registry (save/load/blend/delete/clear,
corrupt-file resilience, disabled-gate no-ops), the SpeakerEmbedder accessor +
named-cluster canonical-label protection in reconcile, confirm_speaker
enrollment, the vault-vocabulary assembly order + per-call caching, and the WS
handlers.

Privacy gating is asserted at every touchpoint: with SPEAKER_REGISTRY_ENABLED
and WHISPER_VOCAB_FROM_VAULT unset, nothing is read/written and the feature is
invisible (status reports enabled=False, no file is created).
"""

import numpy as np
import pytest

from web.backend import speaker_registry
from web.backend.engine import MeetingEngine, _is_cluster_key
from web.backend.speaker_embedder import SpeakerEmbedder
from web.backend.srt_parser import SrtSegment


def _unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    return v / np.linalg.norm(v)


async def _noop_emit(_msg):
    return


# ── Registry module: save / load / blend / delete / clear / corrupt / gate ──


@pytest.fixture
def enabled_registry(tmp_path, monkeypatch):
    """Opt-in registry pointed at an isolated tmp file."""
    monkeypatch.setenv("SPEAKER_REGISTRY_ENABLED", "true")
    path = tmp_path / "speaker-registry.json"
    monkeypatch.setenv("SPEAKER_REGISTRY_PATH", str(path))
    return path


def test_enroll_load_roundtrip(enabled_registry):
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    centroids = speaker_registry.load_centroids()
    assert "Alice" in centroids
    assert abs(float(np.linalg.norm(centroids["Alice"])) - 1.0) < 1e-5
    assert enabled_registry.exists()  # persisted to disk


def test_enroll_blends_and_counts(enabled_registry):
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    speaker_registry.enroll("Alice", _unit([0.0, 1.0, 0.0]))
    people = {p["name"]: p for p in speaker_registry.status()["people"]}
    assert people["Alice"]["meeting_count"] == 2
    # stored = norm(0.7*x + 0.3*y): both components positive, x-weight dominates.
    c = speaker_registry.load_centroids()["Alice"]
    assert c[0] > c[1] > 0
    assert abs(float(np.linalg.norm(c)) - 1.0) < 1e-5


def test_delete_and_clear(enabled_registry):
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    speaker_registry.enroll("Bob", _unit([0.0, 1.0, 0.0]))
    assert speaker_registry.delete("Alice") is True
    assert speaker_registry.delete("Nobody") is False
    remaining = speaker_registry.load_centroids()
    assert "Alice" not in remaining and "Bob" in remaining
    speaker_registry.clear()
    assert speaker_registry.load_centroids() == {}


def test_status_never_leaks_centroids(enabled_registry):
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    status = speaker_registry.status()
    assert status["enabled"] is True
    assert status["people"][0]["name"] == "Alice"
    assert all("centroid" not in p for p in status["people"])


def test_corrupt_file_treated_as_empty(enabled_registry):
    enabled_registry.write_text("{ this is not valid json ", encoding="utf-8")
    # A malformed file never raises or bricks diarization — it reads as empty.
    assert speaker_registry.load_centroids() == {}
    assert speaker_registry.status() == {"enabled": True, "people": []}
    # A subsequent enroll cleanly overwrites the bad file.
    speaker_registry.enroll("Bob", _unit([1.0, 0.0, 0.0]))
    assert "Bob" in speaker_registry.load_centroids()


def test_disabled_gate_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEAKER_REGISTRY_ENABLED", raising=False)
    path = tmp_path / "speaker-registry.json"
    monkeypatch.setenv("SPEAKER_REGISTRY_PATH", str(path))
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    assert not path.exists()  # nothing written while disabled
    assert speaker_registry.load_centroids() == {}
    assert speaker_registry.status() == {"enabled": False, "people": []}
    assert speaker_registry.delete("Alice") is False
    speaker_registry.clear()  # no-op, no raise


# ── SpeakerEmbedder: get_centroid + named-cluster canonical protection ──────


def test_get_centroid_returns_copy_or_none():
    emb = SpeakerEmbedder()
    v = _unit([1.0, 0.0, 0.0])
    emb._known_speakers["Speaker 1"] = v.copy()
    got = emb.get_centroid("Speaker 1")
    assert got is not None
    np.testing.assert_allclose(got, v)
    got[0] = 999.0  # mutating the returned copy must not touch the stored one
    assert emb._known_speakers["Speaker 1"][0] == pytest.approx(v[0])
    assert emb.get_centroid("absent") is None


def test_reconcile_real_name_wins_canonical():
    """A merged group containing a real-name cluster keeps the real name — a
    registry-seeded/confirmed name is never demoted back to 'Speaker N'."""
    emb = SpeakerEmbedder()
    a = _unit([1.0, 0.02, 0.0])
    b = _unit([1.0, 0.0, 0.0])
    emb._history = [("Alice", a), ("Speaker 2", b)]
    emb._known_speakers = {"Alice": a.copy(), "Speaker 2": b.copy()}
    mapping = emb.reconcile_clusters()
    assert mapping == {"Speaker 2": "Alice"}
    assert emb.get_centroid("Alice") is not None
    assert emb.get_centroid("Speaker 2") is None  # merged away


def test_reconcile_speaker_n_lowest_wins():
    """With no real name present, the lowest 'Speaker N' label stays canonical."""
    emb = SpeakerEmbedder()
    a = _unit([1.0, 0.02, 0.0])
    b = _unit([1.0, 0.0, 0.0])
    emb._history = [("Speaker 3", a), ("Speaker 1", b)]
    mapping = emb.reconcile_clusters()
    assert mapping == {"Speaker 3": "Speaker 1"}


# ── Engine label handling: real names never trigger unknown-speaker prompt ──


def test_is_cluster_key_distinguishes_real_names():
    assert _is_cluster_key("Speaker 3") is True
    assert _is_cluster_key("Alice") is False
    assert _is_cluster_key("Speaker") is False
    assert _is_cluster_key("") is False
    assert _is_cluster_key(None) is False


@pytest.mark.asyncio
async def test_real_name_segments_never_prompt():
    events = []

    async def emit(m):
        events.append(m)

    engine = MeetingEngine(llm=None, emit=emit)
    segs = [SrtSegment(i, "00:00:01,000", "00:00:03,000", "hi", speaker="Alice")
            for i in range(1, 6)]  # well past SPEAKER_PROMPT_THRESHOLD
    await engine._maybe_prompt_unknown_speakers(segs)
    assert not any(e["type"] == "speaker_prompt" for e in events)


# ── confirm_speaker enrollment (mock embedder + tmp registry path) ──────────


class _FakeEmbedder:
    def __init__(self, centroid):
        self._centroid = centroid
        self.renamed = []

    def rename_cluster(self, old, new):
        self.renamed.append((old, new))
        return True

    def get_centroid(self, key):
        return self._centroid


class _RaisingEmbedder(_FakeEmbedder):
    def get_centroid(self, key):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_confirm_speaker_enrolls_when_enabled(enabled_registry):
    emb = _FakeEmbedder(_unit([0.6, 0.8, 0.0]))
    engine = MeetingEngine(llm=None, emit=_noop_emit, speaker_embedder=emb)
    await engine.confirm_speaker("Speaker 1", "Alice")
    assert ("Speaker 1", "Alice") in emb.renamed
    assert "Alice" in speaker_registry.load_centroids()


@pytest.mark.asyncio
async def test_confirm_speaker_no_enroll_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEAKER_REGISTRY_ENABLED", raising=False)
    path = tmp_path / "speaker-registry.json"
    monkeypatch.setenv("SPEAKER_REGISTRY_PATH", str(path))
    emb = _FakeEmbedder(_unit([0.6, 0.8, 0.0]))
    engine = MeetingEngine(llm=None, emit=_noop_emit, speaker_embedder=emb)
    await engine.confirm_speaker("Speaker 1", "Alice")
    assert not path.exists()  # gate closed → no registry write


@pytest.mark.asyncio
async def test_confirm_speaker_enroll_failure_never_breaks(enabled_registry):
    emb = _RaisingEmbedder(None)
    engine = MeetingEngine(llm=None, emit=_noop_emit, speaker_embedder=emb)
    # get_centroid raising must be swallowed — confirm_speaker still completes.
    await engine.confirm_speaker("Speaker 1", "Alice")
    assert "Alice" not in speaker_registry.load_centroids()


# ── Vault vocabulary: assembly order + per-call caching + disabled default ──


def test_load_vault_vocab_names_order(monkeypatch):
    from web.backend import knowledge_store as ks
    monkeypatch.setattr(ks, "list_people",
                        lambda: [{"title": "Alice Chen"}, {"title": ""}])
    monkeypatch.setattr(ks, "list_projects", lambda: [{"title": "Q3 Pricing"}])
    # People first, then projects; blank titles dropped.
    assert MeetingEngine._load_vault_vocab_names() == ["Alice Chen", "Q3 Pricing"]


@pytest.mark.asyncio
async def test_current_prompt_appends_vault_after_attendees(monkeypatch):
    monkeypatch.setenv("WHISPER_PROMPT_INCLUDE_ATTENDEES", "true")
    monkeypatch.setenv("WHISPER_VOCAB_FROM_VAULT", "true")
    captured = {}
    from web.backend import transcript_prompt

    def fake_build(vocab, names=None, max_tokens=150):
        captured["names"] = list(names or [])
        return "PROMPT"

    monkeypatch.setattr(transcript_prompt, "build_initial_prompt", fake_build)
    engine = MeetingEngine(llm=None, emit=_noop_emit)
    engine.meeting_context.title = "Sync"
    engine.meeting_context.attendees = ["Bob Smith"]
    engine._vault_vocab_names = ["Alice Chen", "Q3 Pricing"]
    engine.current_initial_prompt()
    # Vault names come AFTER attendees so user-specific names win the token budget.
    assert captured["names"] == ["Bob Smith", "Alice Chen", "Q3 Pricing"]


@pytest.mark.asyncio
async def test_current_prompt_does_not_rescan_vault(monkeypatch):
    monkeypatch.setenv("WHISPER_VOCAB_FROM_VAULT", "true")
    from web.backend import transcript_prompt
    monkeypatch.setattr(transcript_prompt, "build_initial_prompt",
                        lambda *a, **k: "P")
    calls = {"n": 0}
    monkeypatch.setattr(MeetingEngine, "_load_vault_vocab_names",
                        staticmethod(lambda: calls.__setitem__("n", calls["n"] + 1) or ["X"]))
    engine = MeetingEngine(llm=None, emit=_noop_emit)
    engine._vault_vocab_names = ["Cached"]
    for _ in range(5):
        engine.current_initial_prompt()
    assert calls["n"] == 0  # per-transcription path never touches disk


@pytest.mark.asyncio
async def test_current_prompt_vault_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WHISPER_VOCAB_FROM_VAULT", raising=False)
    captured = {}
    from web.backend import transcript_prompt
    monkeypatch.setattr(transcript_prompt, "build_initial_prompt",
                        lambda vocab, names=None, max_tokens=150:
                        captured.__setitem__("names", list(names or [])) or "P")
    engine = MeetingEngine(llm=None, emit=_noop_emit)
    engine._vault_vocab_names = ["ShouldNotAppear"]
    engine.current_initial_prompt()
    assert "ShouldNotAppear" not in captured["names"]


@pytest.mark.asyncio
async def test_start_populates_vault_vocab_when_enabled(monkeypatch):
    monkeypatch.setenv("WHISPER_VOCAB_FROM_VAULT", "true")
    monkeypatch.setattr(MeetingEngine, "_load_vault_vocab_names",
                        staticmethod(lambda: ["Alice Chen"]))
    engine = MeetingEngine(llm=None, emit=_noop_emit)
    await engine.start()
    assert engine._vault_vocab_names == ["Alice Chen"]


@pytest.mark.asyncio
async def test_start_skips_vault_vocab_when_disabled(monkeypatch):
    monkeypatch.delenv("WHISPER_VOCAB_FROM_VAULT", raising=False)
    calls = {"n": 0}
    monkeypatch.setattr(MeetingEngine, "_load_vault_vocab_names",
                        staticmethod(lambda: calls.__setitem__("n", calls["n"] + 1) or ["X"]))
    engine = MeetingEngine(llm=None, emit=_noop_emit)
    await engine.start()
    assert engine._vault_vocab_names == []
    assert calls["n"] == 0  # gate closed → vault never scanned


# ── WS handlers: status / delete / clear ────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registries():
    from web.backend import main as main_mod
    yield
    main_mod._sessions.clear()
    main_mod._active_websockets.clear()


def _recv_until(ws, msg_type, tries=10):
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"never received {msg_type!r}")


def test_ws_speaker_registry_status_delete_clear(enabled_registry, monkeypatch):
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)
    speaker_registry.enroll("Alice", _unit([1.0, 0.0, 0.0]))
    speaker_registry.enroll("Bob", _unit([0.0, 1.0, 0.0]))

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "speaker_registry_status", "client_id": "reg-ws"})
        msg = _recv_until(ws, "speaker_registry")
        assert msg["enabled"] is True
        assert sorted(p["name"] for p in msg["people"]) == ["Alice", "Bob"]
        assert all("centroid" not in p for p in msg["people"])  # never leaked

        ws.send_json({"type": "speaker_registry_delete", "name": "Alice"})
        msg = _recv_until(ws, "speaker_registry")
        assert sorted(p["name"] for p in msg["people"]) == ["Bob"]

        ws.send_json({"type": "speaker_registry_clear"})
        msg = _recv_until(ws, "speaker_registry")
        assert msg["people"] == []


def test_ws_speaker_registry_hidden_when_disabled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web.backend import main as main_mod

    monkeypatch.delenv("SPEAKER_REGISTRY_ENABLED", raising=False)
    monkeypatch.setenv("SPEAKER_REGISTRY_PATH", str(tmp_path / "reg.json"))
    monkeypatch.setattr(main_mod, "_get_or_create_llm", lambda session: None)

    client = TestClient(main_mod.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "speaker_registry_status", "client_id": "reg-off"})
        msg = _recv_until(ws, "speaker_registry")
        assert msg["enabled"] is False
        assert msg["people"] == []
