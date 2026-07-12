"""Tests for the Bedrock provider (Anthropic SDK Bedrock client).

Fully offline — the AnthropicBedrockMantle async client is mocked, so no network
or AWS credentials are touched. Covers the Bedrock↔Anthropic message translation
(both directions), system stable/volatile caching, the tool-use loop, stream()
chunk yielding, factory selection, model-ID normalization, and temperature
omission.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from web.backend.llm_service import system_prompt_blocks
from web.backend.bedrock_service import (
    BedrockService,
    _normalize_model_id,
    to_anthropic_messages,
    to_anthropic_system,
    to_anthropic_tools,
    _from_anthropic_content,
)


# ── Fakes for the async streaming client ────────────────────────────────────

def _text_delta(text: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _final(content, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class _FakeStream:
    """Stand-in for the async context manager returned by client.messages.stream()."""

    def __init__(self, events, final):
        self._events = list(events)
        self._final = final
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev

    async def get_final_message(self):
        return self._final


def _svc(model_id: str = "anthropic.claude-opus-4-8") -> BedrockService:
    svc = BedrockService.__new__(BedrockService)
    svc.model_id = model_id
    svc.client = MagicMock()
    return svc


def _wire(svc, events, final, captured: dict | None = None):
    def fake_stream(**req):
        if captured is not None:
            captured.update(req)
        return _FakeStream(events, final)

    svc.client.messages.stream.side_effect = fake_stream


class _RaisingStream:
    """A stream context manager whose entry raises — simulates an auth failure."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


def _wire_error(svc, exc):
    svc.client.messages.stream.side_effect = lambda **req: _RaisingStream(exc)


def _anthropic_error(cls, status: int):
    resp = httpx.Response(status, request=httpx.Request("POST", "http://x"))
    return cls("auth failed", response=resp, body=None)


def _anthropic_error_msg(cls, status: int, message: str):
    resp = httpx.Response(status, request=httpx.Request("POST", "http://x"))
    return cls(message, response=resp, body=None)


def _wire_sequence(svc, sequence):
    """sequence: list of (events, final) — one per streamed round."""
    state = {"i": 0}

    def fake_stream(**req):
        events, final = sequence[state["i"]]
        state["i"] += 1
        return _FakeStream(events, final)

    svc.client.messages.stream.side_effect = fake_stream


# ── Translation: Bedrock → Anthropic ────────────────────────────────────────

def test_text_and_cachepoint_to_cache_control():
    msgs = [{"role": "user", "content": [{"text": "hi"}, {"cachePoint": {"type": "default"}}]}]
    out = to_anthropic_messages(msgs, caching=True)
    assert out == [{"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
    ]}]
    # caching off → cachePoint dropped, no cache_control
    out_off = to_anthropic_messages(msgs, caching=False)
    assert out_off == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_image_block_becomes_base64():
    msg = {"role": "user", "content": [
        {"text": "look"},
        {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}},
    ]}
    out = to_anthropic_messages([msg])
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[1]["source"]["data"]  # non-empty base64


def test_tooluse_becomes_tool_use():
    msg = {"role": "assistant", "content": [
        {"text": "calling"},
        {"toolUse": {"toolUseId": "t1", "name": "search", "input": {"q": "x"}}},
    ]}
    out = to_anthropic_messages([msg])
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "calling"}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}


def test_toolresult_becomes_tool_result():
    msg = {"role": "user", "content": [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "result-data"}]}},
    ]}
    out = to_anthropic_messages([msg])
    assert out == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "result-data"}]},
    ]}]


def test_string_content_passthrough():
    out = to_anthropic_messages([{"role": "assistant", "content": "ok"}])
    assert out == [{"role": "assistant", "content": "ok"}]


# ── Translation: system stable/volatile ─────────────────────────────────────

def test_system_stable_volatile_to_array_with_cache_control():
    struct = system_prompt_blocks("STABLE", "VOLATILE")
    out = to_anthropic_system(struct, caching=True)
    assert out[0] == {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}
    assert out[1] == {"type": "text", "text": "VOLATILE"}  # no cache_control on volatile

    out_off = to_anthropic_system(struct, caching=False)
    assert out_off == [{"type": "text", "text": "STABLE"}, {"type": "text", "text": "VOLATILE"}]


def test_system_plain_string():
    assert to_anthropic_system("SYS", caching=True) == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]
    assert to_anthropic_system("SYS", caching=False) == "SYS"
    assert to_anthropic_system(None, caching=True) is None


# ── Translation: tools + reverse (Anthropic → Bedrock) ──────────────────────

def test_tools_translation():
    tools = [{"toolSpec": {
        "name": "search", "description": "find things",
        "inputSchema": {"json": {"type": "object", "properties": {"q": {"type": "string"}}}},
    }}]
    out = to_anthropic_tools(tools)
    assert out == [{
        "name": "search", "description": "find things",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]
    assert to_anthropic_tools(None) is None


def test_from_anthropic_content_reverse():
    blocks = [
        SimpleNamespace(type="text", text="hello"),
        SimpleNamespace(type="tool_use", id="t1", name="f", input={"a": 1}),
        SimpleNamespace(type="thinking", thinking="ignored"),
    ]
    assert _from_anthropic_content(blocks) == [
        {"text": "hello"},
        {"toolUse": {"toolUseId": "t1", "name": "f", "input": {"a": 1}}},
    ]
    # also accepts plain dicts
    assert _from_anthropic_content([{"type": "text", "text": "x"}]) == [{"text": "x"}]


# ── stream() / analyze() ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_yields_chunks(monkeypatch):
    monkeypatch.delenv("BEDROCK_TEMPERATURE", raising=False)
    svc = _svc()
    _wire(svc, [_text_delta("Hello"), _text_delta(" world")],
          _final([{"type": "text", "text": "Hello world"}]))
    chunks = [c async for c in svc.stream([BedrockService.msg("user", "hi")])]
    assert chunks == ["Hello", " world"]


@pytest.mark.asyncio
async def test_analyze_returns_full(monkeypatch):
    monkeypatch.delenv("BEDROCK_TEMPERATURE", raising=False)
    svc = _svc()
    _wire(svc, [_text_delta("A"), _text_delta("B")], _final([{"type": "text", "text": "AB"}]))
    result = await svc.analyze([BedrockService.msg("user", "hi")])
    assert result == "AB"


# ── tool-use loop round-trip ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_loop_round_trip(monkeypatch):
    monkeypatch.delenv("BEDROCK_TEMPERATURE", raising=False)
    svc = _svc()
    _wire_sequence(svc, [
        # round 1 — model asks for a tool
        ([_text_delta("let me check")], _final(
            [SimpleNamespace(type="text", text="let me check"),
             SimpleNamespace(type="tool_use", id="call_1", name="get_x", input={"q": "hi"})],
            stop_reason="tool_use",
        )),
        # round 2 — final answer
        ([_text_delta("Answer")], _final(
            [SimpleNamespace(type="text", text="Answer")], stop_reason="end_turn",
        )),
    ])

    seen = {}

    async def handler(name, inp):
        seen["name"], seen["inp"] = name, inp
        return {"ok": True}

    original = [BedrockService.msg("user", "question")]
    text, msgs = await svc.converse_stream_with_tools(
        original, tools=None, tool_handler=handler,
    )

    assert text == "let me check Answer"
    assert seen == {"name": "get_x", "inp": {"q": "hi"}}
    # History returned in the engine's Bedrock shape: an assistant toolUse + a
    # user toolResult (never the Anthropic tool_use/tool_result shape).
    flat = [b for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])]
    assert any(isinstance(b, dict) and "toolUse" in b for b in flat)
    assert any(isinstance(b, dict) and "toolResult" in b for b in flat)
    # Input messages must not be mutated (deep-copied internally, §2.8).
    assert original == [{"role": "user", "content": [{"text": "question"}]}]


# ── temperature omission ────────────────────────────────────────────────────

def test_temperature_omitted_by_default(monkeypatch):
    monkeypatch.delenv("BEDROCK_TEMPERATURE", raising=False)
    svc = _svc()
    req = svc._build_request(
        [BedrockService.msg("user", "hi")], None, None,
        use_cache=False, max_tokens=512,
    )
    assert req["max_tokens"] == 512
    assert "temperature" not in req


def test_temperature_included_when_env_set(monkeypatch):
    monkeypatch.setenv("BEDROCK_TEMPERATURE", "0.3")
    svc = _svc()
    req = svc._build_request(
        [BedrockService.msg("user", "hi")], None, None,
        use_cache=False, max_tokens=512,
    )
    assert req["temperature"] == 0.3


# ── auth-error mapping (api-key primary, SSO fallback) ──────────────────────

@pytest.mark.asyncio
async def test_auth_error_api_key_mode(monkeypatch):
    """401 (AuthenticationError) in api-key mode → check-your-key message."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc")
    svc = _svc()
    _wire_error(svc, _anthropic_error(anthropic.AuthenticationError, 401))
    with pytest.raises(RuntimeError, match="Bedrock API key"):
        _ = [c async for c in svc.stream([BedrockService.msg("user", "hi")])]


@pytest.mark.asyncio
async def test_auth_error_sso_mode(monkeypatch):
    """403 (PermissionDeniedError) in SSO mode keeps the `aws sso login` hint."""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    svc = _svc()
    _wire_error(svc, _anthropic_error(anthropic.PermissionDeniedError, 403))
    with pytest.raises(RuntimeError, match="aws sso login"):
        _ = [c async for c in svc.stream([BedrockService.msg("user", "hi")])]


# ── invalid-model-id mapping → InvalidModelError (conservative) ──────────────

@pytest.mark.asyncio
async def test_invalid_model_mapped(monkeypatch):
    """A NotFoundError/BadRequestError clearly about the model → InvalidModelError."""
    from web.backend.llm_service import InvalidModelError
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    svc = _svc()
    _wire_error(svc, _anthropic_error_msg(
        anthropic.NotFoundError, 404, "The provided model identifier is invalid.",
    ))
    with pytest.raises(InvalidModelError) as ei:
        _ = [c async for c in svc.stream([BedrockService.msg("user", "hi")])]
    assert ei.value.model_id == svc.model_id


@pytest.mark.asyncio
async def test_unrelated_notfound_propagates(monkeypatch):
    """A NotFoundError NOT about the model propagates unchanged (conservative map)."""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    svc = _svc()
    _wire_error(svc, _anthropic_error_msg(
        anthropic.NotFoundError, 404, "resource is unavailable",
    ))
    with pytest.raises(anthropic.NotFoundError):
        _ = [c async for c in svc.stream([BedrockService.msg("user", "hi")])]


def test_construction_does_not_inject_credentials(monkeypatch):
    """Client is built with only aws_region — no api_key/aws_* creds/profile that
    would force an auth mode and defeat env-var bearer-token resolution."""
    captured = {}

    def fake_ctor(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrockMantle", fake_ctor)
    svc = BedrockService(region="us-west-2")
    assert captured == {"aws_region": "us-west-2"}
    for k in ("api_key", "aws_access_key", "aws_secret_key",
              "aws_session_token", "aws_profile"):
        assert k not in captured


# ── factory selection ───────────────────────────────────────────────────────

def test_factory_selects_bedrock(monkeypatch):
    """LLM_PROVIDER=bedrock builds the Anthropic-SDK BedrockService (no longer the
    default — that's openrouter now)."""
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrockMantle", lambda **kw: MagicMock())
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")

    from web.backend.llm_service import create_llm_service
    from web.backend.bedrock_service import (
        BedrockService, DEFAULT_MODEL_ID, FAST_MODEL_ID,
    )

    svc = create_llm_service()
    assert isinstance(svc, BedrockService)
    assert svc.model_id == DEFAULT_MODEL_ID
    # fast-tier parity: fast=True selects the Haiku-class default
    fast = create_llm_service(fast=True)
    assert fast.model_id == FAST_MODEL_ID


# ── model-ID normalization (legacy converse IDs → bare anthropic. form) ──────

def test_normalize_model_id_strips_region_and_v1():
    assert _normalize_model_id("us.anthropic.claude-opus-4-6-v1") == "anthropic.claude-opus-4-6"
    assert _normalize_model_id("eu.anthropic.claude-sonnet-4-6-v1") == "anthropic.claude-sonnet-4-6"
    assert _normalize_model_id("global.anthropic.claude-haiku-4-5-v1") == "anthropic.claude-haiku-4-5"
    # Already-bare IDs (and the new defaults) pass through unchanged.
    assert _normalize_model_id("anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"
    assert _normalize_model_id("") == ""


def test_constructor_normalizes_model_id(monkeypatch):
    """__init__ normalizes an explicit legacy model_id (e.g. the vision path)."""
    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrockMantle", lambda **kw: MagicMock())
    svc = BedrockService(model_id="us.anthropic.claude-opus-4-6-v1", region="us-east-1")
    assert svc.model_id == "anthropic.claude-opus-4-6"
