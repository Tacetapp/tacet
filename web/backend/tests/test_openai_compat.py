"""Tests for OpenAI-compatible provider: Bedrock↔OpenAI translation + tool loop.

All offline — the streaming HTTP is mocked.
"""

import pytest

from web.backend.llm_service import LLMService
from web.backend.openai_compat_service import (
    OpenAICompatService,
    to_openai_messages,
    to_openai_tools,
)


def test_system_prompt_prepended():
    out = to_openai_messages([LLMService.msg("user", "hi")], system_prompt="be terse")
    assert out[0] == {"role": "system", "content": "be terse"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_text_message_flattened_to_string():
    out = to_openai_messages([LLMService.msg("assistant", "ok")])
    assert out == [{"role": "assistant", "content": "ok"}]


def test_image_block_becomes_data_url():
    msg = {"role": "user", "content": [
        {"text": "look"},
        {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}},
    ]}
    out = to_openai_messages([msg])
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_tooluse_becomes_tool_calls():
    msg = {"role": "assistant", "content": [
        {"text": "calling"},
        {"toolUse": {"toolUseId": "t1", "name": "search", "input": {"q": "x"}}},
    ]}
    out = to_openai_messages([msg])
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "calling"
    assert out[0]["tool_calls"][0]["id"] == "t1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "search"
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'


def test_toolresult_becomes_tool_message():
    msg = {"role": "user", "content": [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "result-data"}]}},
    ]}
    out = to_openai_messages([msg])
    assert out == [{"role": "tool", "tool_call_id": "t1", "content": "result-data"}]


def test_cachepoint_dropped():
    msg = {"role": "user", "content": [{"text": "hi"}, {"cachePoint": {"type": "default"}}]}
    out = to_openai_messages([msg])
    assert out == [{"role": "user", "content": "hi"}]


def test_tools_translation():
    tools = [{"toolSpec": {
        "name": "search", "description": "find things",
        "inputSchema": {"json": {"type": "object", "properties": {"q": {"type": "string"}}}},
    }}]
    out = to_openai_tools(tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "search"
    assert out[0]["function"]["parameters"]["type"] == "object"
    assert to_openai_tools(None) is None


def _svc():
    return OpenAICompatService(model_id="m", api_key="k", base_url="http://x")


def _no_key_svc():
    """Service with no API key — mirrors the Ollama factory path."""
    return OpenAICompatService(
        model_id="llama3.1", api_key="", base_url="http://localhost:11434/v1",
        require_api_key=False,
    )


@pytest.mark.asyncio
async def test_no_api_key_raises_by_default():
    """Empty key with the default require_api_key=True must raise before any HTTP."""
    svc = OpenAICompatService(model_id="m", api_key="", base_url="http://x")
    with pytest.raises(RuntimeError, match="no API key"):
        async for _ in svc._post_stream({}):
            pass


def test_no_api_key_no_raise_when_not_required():
    """require_api_key=False is stored and does not raise at construction time."""
    svc = _no_key_svc()
    assert not svc._require_api_key


def test_headers_omit_auth_when_no_key():
    """No Authorization header when api_key is empty (Ollama path)."""
    svc = _no_key_svc()
    h = svc._headers()
    assert "Authorization" not in h
    assert h["Content-Type"] == "application/json"


def test_headers_include_auth_when_key_set():
    """Authorization header present when api_key is set."""
    svc = _svc()
    h = svc._headers()
    assert h["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_stream_accumulates_text():
    svc = _svc()

    async def fake(payload):
        for c in [
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]:
            yield c

    svc._post_stream = fake
    chunks = [c async for c in svc.stream([LLMService.msg("user", "hi")])]
    assert "".join(chunks) == "Hello"


@pytest.mark.asyncio
async def test_tool_loop_returns_bedrock_shape():
    svc = _svc()
    rounds = {"n": 0}

    async def fake(payload):
        rounds["n"] += 1
        if rounds["n"] == 1:
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "get_x", "arguments": ""}}]}}]}
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"q": "hi"}'}}]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "Answer"}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    svc._post_stream = fake
    seen = {}

    async def handler(name, inp):
        seen["name"], seen["inp"] = name, inp
        return {"ok": True}

    messages = [LLMService.msg("user", "question")]
    text, msgs = await svc.converse_stream_with_tools(
        messages, tools=None, tool_handler=handler,
    )
    assert text == "Answer"
    assert seen == {"name": "get_x", "inp": {"q": "hi"}}
    # History stays in Bedrock shape: an assistant toolUse + a user toolResult.
    assert any(
        isinstance(b, dict) and "toolUse" in b
        for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
    )
    assert any(
        isinstance(b, dict) and "toolResult" in b
        for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
    )
