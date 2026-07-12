"""Tests for meeting engine."""

import asyncio
import time
import pytest
from web.backend.engine import MeetingEngine
from web.backend.bedrock_service import BedrockService
from web.backend.srt_parser import SrtSegment


def _seg(i, text="line"):
    return SrtSegment(i, "00:00:01,000", "00:00:03,000", f"{text} {i}")


class FakeBedrock:
    def __init__(self, analysis_response="[NO_ALERT]", chat_chunks=None):
        self._analysis = analysis_response
        self._chat_chunks = chat_chunks or ["Hello", " there"]

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        for c in self._chat_chunks:
            yield c

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        return self._analysis

    async def converse_stream_with_tools(self, messages, system_prompt=None,
                                          tools=None, tool_handler=None, on_text=None,
                                          use_cache=False, max_tokens=4096):
        text = "".join(self._chat_chunks)
        if on_text:
            for c in self._chat_chunks:
                await on_text(c)
        return text, messages

    @staticmethod
    def msg(role, text):
        return BedrockService.msg(role, text)


@pytest.mark.asyncio
async def test_chat_emits_chunks_and_done():
    events = []
    async def emit(msg): events.append(msg)

    engine = MeetingEngine(llm=FakeBedrock(), emit=emit)
    engine.chat_ready = True
    await engine.chat("What was discussed?")

    types = [e["type"] for e in events]
    assert "chat_chunk" in types
    assert types[-1] == "chat_done"
    text_chunks = [e["text"] for e in events if e["type"] == "chat_chunk"]
    assert "".join(text_chunks) == "Hello there"


@pytest.mark.asyncio
async def test_analyze_emits_insights():
    events = []
    async def emit(msg): events.append(msg)

    analysis = '[{"category": "term", "label": "NERC CIP", "text": "NERC CIP — cybersecurity standards for electric utilities"}]'
    engine = MeetingEngine(llm=FakeBedrock(analysis_response=analysis), emit=emit)

    from web.backend.srt_parser import SrtSegment
    segs = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "We need to comply with NERC CIP")]
    await engine._analyze(segs)
    await asyncio.sleep(0.05)

    insight_events = [e for e in events if e["type"] == "insight"]
    assert len(insight_events) == 1
    assert insight_events[0]["category"] == "term"
    assert "NERC CIP" in insight_events[0]["text"]
    assert "NERC CIP" in engine.explained_terms


@pytest.mark.asyncio
async def test_no_alert_skips_insight():
    events = []
    async def emit(msg): events.append(msg)

    engine = MeetingEngine(llm=FakeBedrock(analysis_response="[NO_ALERT]"), emit=emit)

    from web.backend.srt_parser import SrtSegment
    segs = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "Just chatting")]
    await engine._analyze(segs)
    await asyncio.sleep(0.05)

    assert not any(e.get("type") == "insight" for e in events)


@pytest.mark.asyncio
async def test_duplicate_terms_skipped():
    events = []
    async def emit(msg): events.append(msg)

    analysis = '[{"category": "term", "label": "ADMS", "text": "ADMS — Advanced Distribution Management System"}]'
    engine = MeetingEngine(llm=FakeBedrock(analysis_response=analysis), emit=emit)

    from web.backend.srt_parser import SrtSegment
    segs = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "ADMS discussion")]

    await engine._analyze(segs)
    await asyncio.sleep(0.05)
    await engine._analyze(segs)  # same term again
    await asyncio.sleep(0.05)

    insight_events = [e for e in events if e["type"] == "insight"]
    assert len(insight_events) == 1  # only once


# ── §5.2 Insight priority + seg grounding ───────────────

@pytest.mark.asyncio
async def test_insight_priority_and_seg_parsed():
    events = []
    async def emit(msg): events.append(msg)

    analysis = ('[{"category": "key_point", "text": "Sarah committed to ship Friday",'
                ' "priority": "alert"}]')
    engine = MeetingEngine(llm=FakeBedrock(analysis_response=analysis), emit=emit)

    segs = [SrtSegment(7, "00:00:01,000", "00:00:03,000", "We ship Friday"),
            SrtSegment(8, "00:00:03,000", "00:00:05,000", "Agreed")]
    await engine._analyze(segs)
    await asyncio.sleep(0.05)

    ins = [e for e in events if e["type"] == "insight"]
    assert len(ins) == 1
    assert ins[0]["priority"] == "alert"
    assert ins[0]["seg"] == 7  # first index of the analyzed batch
    # And it is persisted on the engine's insight list with the same fields.
    assert engine.insights[0]["priority"] == "alert"
    assert engine.insights[0]["seg"] == 7


@pytest.mark.asyncio
async def test_insight_priority_defaults_to_note_when_absent():
    events = []
    async def emit(msg): events.append(msg)

    # Model omits "priority" entirely, and supplies a bogus value on a second item.
    analysis = ('[{"category": "context", "text": "Acme uses Databricks"},'
                ' {"category": "key_point", "text": "x", "priority": "URGENT!!"}]')
    engine = MeetingEngine(llm=FakeBedrock(analysis_response=analysis), emit=emit)

    await engine._analyze([SrtSegment(3, "00:00:01,000", "00:00:03,000", "hi")])
    await asyncio.sleep(0.05)

    ins = [e for e in events if e["type"] == "insight"]
    assert len(ins) == 2
    assert all(i["priority"] == "note" for i in ins)  # missing & garbage → note
    assert all(i["seg"] == 3 for i in ins)


@pytest.mark.asyncio
async def test_chat_insight_has_no_seg():
    """Insights mined from post-meeting chat have no analyzed batch, so no seg."""
    events = []
    async def emit(msg): events.append(msg)
    engine = MeetingEngine(llm=FakeBedrock(), emit=emit)
    engine._parse_insights('[{"category": "term", "label": "RAG", "text": "retrieval"}]')
    await asyncio.sleep(0.05)
    ins = [e for e in events if e["type"] == "insight"][0]
    assert "seg" not in ins
    assert ins["priority"] == "note"


def test_old_session_insights_load_without_priority(monkeypatch):
    """Sessions saved before priority/seg existed must still load (defaults)."""
    _fake_loaded(monkeypatch, [
        {"id": 0, "category": "term", "text": "a"},          # no priority, no seg
        {"id": 1, "category": "key_point", "text": "b"},
    ])
    e = MeetingEngine(llm=FakeBedrock(), emit=lambda m: None)
    data = e.load_session("x")
    assert data is not None
    assert e._next_insight_id == 2
    # Loaded insights keep whatever the file had; missing priority/seg is fine —
    # the frontend defaults priority to "note" and derives timestamps by arrival.
    assert "priority" not in e.insights[0]
    assert "seg" not in e.insights[0]


# ── §2.1 Analysis coalescing ────────────────────────────

@pytest.mark.asyncio
async def test_analysis_coalesces_never_runs_concurrently():
    """A second batch arriving while an _analyze is in flight must not spawn a
    concurrent call — it coalesces and fires exactly once when the first ends."""
    state = {"concurrent": 0, "max": 0, "calls": 0}
    gate = asyncio.Event()

    class BlockingBedrock:
        async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
            state["calls"] += 1
            state["concurrent"] += 1
            state["max"] = max(state["max"], state["concurrent"])
            await gate.wait()
            state["concurrent"] -= 1
            return "[NO_ALERT]"

        @staticmethod
        def msg(role, text):
            return BedrockService.msg(role, text)

    engine = MeetingEngine(llm=BlockingBedrock(), emit=lambda m: asyncio.sleep(0))

    # Segments long enough to clear the §3.4 short-batch skip (>=120 chars).
    def _long(i):
        return SrtSegment(i, "00:00:01,000", "00:00:03,000",
                          f"this is a reasonably substantive sentence number {i} about the roadmap")

    # First batch (3 segments, no recent analysis) → spawn one, which blocks.
    engine._last_analysis_at = 0.0  # long ago → interval trigger fires
    engine._pending_segments = [_long(1), _long(2), _long(3)]
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert state["calls"] == 1
    assert engine._analysis_in_flight is True
    assert engine._pending_segments == []  # the batch was taken, not queued

    # More arrive while in flight → no second (concurrent) call is spawned.
    engine._last_analysis_at = 0.0
    engine._pending_segments = [_long(4), _long(5), _long(6)]
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert state["calls"] == 1
    assert len(engine._pending_segments) == 3  # still accumulating

    # Release the in-flight call → exactly one follow-up fires for the backlog.
    gate.set()
    await asyncio.sleep(0.05)
    assert state["calls"] == 2
    assert state["max"] == 1  # never two at once
    assert engine._analysis_in_flight is False
    assert engine._pending_segments == []


# ── §2.4 Insight-ID continuation after load_session ─────

def _fake_loaded(monkeypatch, insights):
    from web.backend import engine as engine_mod
    data = {
        "segments": [], "insights": insights, "explained_terms": [],
        "chat_history": [], "summary": "", "meeting_context": {"title": "X"},
    }
    monkeypatch.setattr(engine_mod, "_load_session_from_store", lambda sid: data)


def test_next_insight_id_continues_past_loaded(monkeypatch):
    _fake_loaded(monkeypatch, [
        {"id": 0, "category": "term", "text": "a"},
        {"id": 1, "category": "key_point", "text": "b"},
        {"id": 2, "category": "key_point", "text": "c"},
    ])
    e = MeetingEngine(llm=FakeBedrock(), emit=lambda m: None)
    e.load_session("x")
    assert e._next_insight_id == 3


def test_next_insight_id_defaults_zero_when_no_insights(monkeypatch):
    _fake_loaded(monkeypatch, [])
    e = MeetingEngine(llm=FakeBedrock(), emit=lambda m: None)
    e.load_session("x")
    assert e._next_insight_id == 0


# ── §2.6 Incremental rolling summary ────────────────────

@pytest.mark.asyncio
async def test_rolling_summary_is_incremental():
    """Only the new segment range plus the previous summary are sent — earlier
    content is not re-summarized from scratch."""
    captured = {}

    class RecordingBedrock:
        async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
            captured["prompt"] = messages[0]["content"][0]["text"]
            captured["max_tokens"] = max_tokens
            return "NEW ROLLING SUMMARY"

        @staticmethod
        def msg(role, text):
            return BedrockService.msg(role, text)

    e = MeetingEngine(llm=RecordingBedrock(), emit=lambda m: None)
    e.segments = [_seg(i, "seg text") for i in range(1, 6)]
    e._rolling_summary = "PRIOR SUMMARY"

    await e._update_rolling_summary(2, 5)  # segments[2:5] → texts 3,4,5

    assert e._rolling_summary == "NEW ROLLING SUMMARY"
    assert "PRIOR SUMMARY" in captured["prompt"]     # previous folded in
    assert "seg text 3" in captured["prompt"]         # new range included
    assert "seg text 1" not in captured["prompt"]     # earlier range not re-sent
    assert captured["max_tokens"] == 512              # §3.7 tight cap for rolling summary


# ── §3.1 Model tiering / fast-tier routing ──────────────

class RecordingLLM:
    """Records analyze() calls; distinct instances stand in for the primary and
    fast tiers so tests can assert which service a call was routed to."""

    def __init__(self, resp="[]"):
        self.calls: list[dict] = []
        self._resp = resp

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        self.calls.append({"use_cache": use_cache, "max_tokens": max_tokens,
                           "system_prompt": system_prompt})
        return self._resp

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        if False:  # pragma: no cover — makes this a (never-yielding) async generator
            yield ""

    async def converse_stream_with_tools(self, messages, system_prompt=None, tools=None,
                                          tool_handler=None, on_text=None, use_cache=False,
                                          max_tokens=4096):
        return "", messages

    @staticmethod
    def msg(role, text):
        return BedrockService.msg(role, text)


def _rich(i):
    return SrtSegment(i, "00:00:01,000", "00:00:03,000",
                      f"a fairly detailed sentence number {i} covering the migration plan and budget")


@pytest.mark.asyncio
async def test_background_calls_use_fast_tier():
    """analysis / curation / rolling summary route through llm_fast, never the
    primary (frontier) service — §3.1."""
    primary = RecordingLLM()
    fast = RecordingLLM()
    engine = MeetingEngine(llm=primary, emit=lambda m: None, llm_fast=fast)

    seg = _rich(1)
    engine.segments = [seg]
    await engine._analyze([seg])
    engine.insights = [{"id": 0, "category": "key_point", "text": "x"}]
    await engine._curate_insights()
    await engine._update_rolling_summary(0, 1)

    assert primary.calls == []           # frontier model untouched by background work
    assert len(fast.calls) == 3          # analysis + curation + rolling all on fast tier
    assert all(c["max_tokens"] == 512 for c in fast.calls)
    # Analysis passes a cacheable compact system prompt (structured block list).
    assert fast.calls[0]["use_cache"] is True
    assert isinstance(fast.calls[0]["system_prompt"], list)


def test_fast_tier_defaults_to_primary_when_unwired():
    """Without an explicit llm_fast, background calls fall back to the primary
    service so pre-§3.1 callers keep working."""
    primary = RecordingLLM()
    engine = MeetingEngine(llm=primary, emit=lambda m: None)
    assert engine.llm_fast is primary


# ── Fast-tier graceful fallback on InvalidModelError ────

class _InvalidFast:
    """A fast tier whose model ID is invalid — analyze() always raises."""

    def __init__(self):
        self.calls = 0

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        from web.backend.llm_service import InvalidModelError
        self.calls += 1
        raise InvalidModelError("us.anthropic.claude-haiku-4-5")

    async def stream(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        if False:  # pragma: no cover
            yield ""

    @staticmethod
    def msg(role, text):
        return BedrockService.msg(role, text)


@pytest.mark.asyncio
async def test_fast_tier_falls_back_to_primary_on_invalid_model():
    """An invalid fast model ID: swap llm_fast → primary, retry on primary, emit
    exactly one user-visible info message — and no second emit afterward."""
    events = []
    async def emit(m): events.append(m)

    primary = RecordingLLM(resp="[NO_ALERT]")
    fast = _InvalidFast()
    engine = MeetingEngine(llm=primary, emit=emit, llm_fast=fast)

    seg = _rich(1)
    engine.segments = [seg]
    await engine._analyze([seg])

    assert fast.calls == 1                 # fast tried once and was rejected
    assert engine.llm_fast is primary      # permanently swapped for the session
    assert len(primary.calls) == 1         # retried on the primary
    infos = [e for e in events if e.get("type") == "info"]
    assert len(infos) == 1
    assert "Fast model ID invalid" in infos[0]["text"]
    assert "Settings" in infos[0]["text"]

    # A later background call goes straight to the primary; no repeated emit.
    await engine._analyze([seg])
    assert len(primary.calls) == 2
    assert len([e for e in events if e.get("type") == "info"]) == 1


@pytest.mark.asyncio
async def test_invalid_model_without_distinct_fast_tier_propagates():
    """When llm_fast IS the primary (both invalid), _fast_analyze re-raises and the
    caller's existing except-Exception handler swallows it — no info emit."""
    events = []
    async def emit(m): events.append(m)

    primary = _InvalidFast()
    engine = MeetingEngine(llm=primary, emit=emit)  # llm_fast defaults to primary
    assert engine.llm_fast is primary

    seg = _rich(1)
    engine.segments = [seg]
    await engine._analyze([seg])  # _analyze's except Exception swallows the raise

    assert primary.calls == 1                                       # no retry
    assert [e for e in events if e.get("type") == "info"] == []     # no fallback emit


# ── §3.3 Summary A/B gate ───────────────────────────────

@pytest.mark.asyncio
async def test_summary_single_variant_default(monkeypatch):
    monkeypatch.delenv("SUMMARY_AB_TEST", raising=False)
    events = []
    async def emit(m): events.append(m)
    engine = MeetingEngine(llm=FakeBedrock(chat_chunks=["Meeting notes body"]), emit=emit)
    engine.segments = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "we shipped the release")]
    await engine._generate_summary()
    assert "Variant" not in engine.summary
    assert engine.summary == "Meeting notes body"
    # No variant header chunks were emitted.
    headers = [e for e in events if e.get("type") == "summary_chunk" and e["text"].strip().startswith("#")]
    assert headers == []


@pytest.mark.asyncio
async def test_summary_ab_test_runs_both(monkeypatch):
    monkeypatch.setenv("SUMMARY_AB_TEST", "true")
    events = []
    async def emit(m): events.append(m)
    engine = MeetingEngine(llm=FakeBedrock(chat_chunks=["body"]), emit=emit)
    engine.segments = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "we shipped the release")]
    await engine._generate_summary()
    assert "Variant A" in engine.summary
    assert "Variant B" in engine.summary


# ── §3.4 Analysis batching thresholds ───────────────────

def _short(i):
    return SrtSegment(i, "00:00:01,000", "00:00:03,000", "ok")


@pytest.mark.asyncio
async def test_batching_fires_on_min_segments(monkeypatch):
    monkeypatch.setenv("ANALYSIS_MIN_SEGMENTS", "8")
    monkeypatch.setenv("ANALYSIS_MIN_INTERVAL_S", "45")
    fast = RecordingLLM()
    engine = MeetingEngine(llm=RecordingLLM(), emit=lambda m: None, llm_fast=fast)
    engine._last_analysis_at = time.monotonic()  # recent → interval branch won't fire

    engine._pending_segments = [_rich(i) for i in range(7)]  # < 8
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert len(fast.calls) == 0

    engine._pending_segments = [_rich(i) for i in range(8)]  # >= 8 → fire on count
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert len(fast.calls) == 1


@pytest.mark.asyncio
async def test_batching_fires_on_interval(monkeypatch):
    monkeypatch.setenv("ANALYSIS_MIN_SEGMENTS", "8")
    monkeypatch.setenv("ANALYSIS_MIN_INTERVAL_S", "45")
    fast = RecordingLLM()
    engine = MeetingEngine(llm=RecordingLLM(), emit=lambda m: None, llm_fast=fast)

    engine._last_analysis_at = time.monotonic()          # just analyzed
    engine._pending_segments = [_rich(i) for i in range(3)]
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert len(fast.calls) == 0                          # 3 seg but <45s → no fire

    engine._last_analysis_at = time.monotonic() - 100    # >45s ago
    engine._pending_segments = [_rich(i) for i in range(3)]
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert len(fast.calls) == 1                          # 3 seg + interval elapsed → fire


@pytest.mark.asyncio
async def test_batching_skips_short_batch(monkeypatch):
    monkeypatch.setenv("ANALYSIS_MIN_SEGMENTS", "8")
    fast = RecordingLLM()
    engine = MeetingEngine(llm=RecordingLLM(), emit=lambda m: None, llm_fast=fast)
    engine._last_analysis_at = time.monotonic()
    engine._pending_segments = [_short(i) for i in range(8)]  # count trigger, but tiny text
    engine._maybe_spawn_analysis()
    await asyncio.sleep(0.02)
    assert len(fast.calls) == 0             # combined text < 120 chars → skipped
    assert engine._pending_segments == []   # pending cleared even though skipped


# ── §3.2 System prompt stable/volatile split ────────────

def test_system_splits_stable_before_volatile():
    e = MeetingEngine(llm=FakeBedrock(), emit=lambda m: None)
    e.segments = [SrtSegment(1, "00:00:01,000", "00:00:03,000", "SECRET_TRANSCRIPT_MARKER was decided")]
    blocks = e._system()
    assert isinstance(blocks, list)
    ci = next(i for i, b in enumerate(blocks) if "cachePoint" in b)
    stable = " ".join(b.get("text", "") for b in blocks[:ci])
    volatile = " ".join(b.get("text", "") for b in blocks[ci:])
    # Live transcript is volatile (after the cache breakpoint); style rules are stable.
    assert "SECRET_TRANSCRIPT_MARKER" in volatile
    assert "SECRET_TRANSCRIPT_MARKER" not in stable
    assert "Writing style rules" in stable
