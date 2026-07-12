"""OpenAI-compatible chat-completions LLM provider (OpenRouter, Venice, Ollama).

Implements the same ``LLMService`` interface as ``BedrockService`` by translating
between the Bedrock "Converse" message shape the engine uses and the OpenAI
``/chat/completions`` format, then streaming over HTTP (httpx, SSE).

The engine never sees OpenAI shapes: messages go in and come back out in Bedrock
shape (text / image / toolUse / toolResult blocks), so this provider is a drop-in
swap behind the factory.

Ollama note: Ollama's OpenAI-compatible endpoint requires no Authorization header.
Pass ``require_api_key=False`` to skip the key check and omit the header entirely.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import AsyncGenerator

import httpx

from web.backend.llm_service import LLMService

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10
REQUEST_TIMEOUT = 120.0


# ── Translation: Bedrock Converse shape → OpenAI chat shape ───────────────────

def to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Bedrock toolConfig tools → OpenAI `tools` (function calling)."""
    if not tools:
        return None
    out = []
    for t in tools:
        spec = t.get("toolSpec", {})
        out.append({
            "type": "function",
            "function": {
                "name": spec.get("name"),
                "description": spec.get("description", ""),
                "parameters": spec.get("inputSchema", {}).get("json", {}),
            },
        })
    return out


def _image_data_url(block: dict) -> str | None:
    img = block.get("image", {})
    fmt = img.get("format", "png")
    data = img.get("source", {}).get("bytes")
    if data is None:
        return None
    b64 = base64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else str(data)
    return f"data:image/{fmt};base64,{b64}"


def _system_prompt_text(system_prompt) -> str:
    """Flatten a system prompt (plain str or structured block list) to text.

    The structured form (llm_service.system_prompt_blocks) carries a cachePoint
    marker splitting a stable prefix from a volatile suffix; OpenAI-compatible
    providers have no prompt caching, so we simply concatenate the text blocks and
    drop the marker.
    """
    if not system_prompt:
        return ""
    if isinstance(system_prompt, str):
        return system_prompt
    return "\n\n".join(
        b["text"] for b in system_prompt if isinstance(b, dict) and "text" in b
    )


def to_openai_messages(messages: list[dict], system_prompt=None) -> list[dict]:
    """Bedrock-shape messages → OpenAI chat messages.

    - text/image blocks → string or multimodal content parts;
    - assistant toolUse blocks → `tool_calls`;
    - user toolResult blocks → separate `{"role": "tool"}` messages;
    - cachePoint blocks → dropped (no OpenAI equivalent).
    """
    out: list[dict] = []
    sys_text = _system_prompt_text(system_prompt)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", [])
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        text_parts: list[dict] = []
        image_parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[tuple[str, str]] = []

        for b in content:
            if not isinstance(b, dict):
                continue
            if "text" in b:
                text_parts.append({"type": "text", "text": b["text"]})
            elif "image" in b:
                url = _image_data_url(b)
                if url:
                    image_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif "toolUse" in b:
                tu = b["toolUse"]
                tool_calls.append({
                    "id": tu["toolUseId"],
                    "type": "function",
                    "function": {"name": tu["name"], "arguments": json.dumps(tu.get("input", {}))},
                })
            elif "toolResult" in b:
                tr = b["toolResult"]
                txt = " ".join(
                    c.get("text", "") for c in tr.get("content", []) if isinstance(c, dict)
                )
                tool_results.append((tr["toolUseId"], txt))
            # cachePoint and anything else → dropped

        # Tool results become their own tool-role messages (OpenAI requirement).
        for tid, txt in tool_results:
            out.append({"role": "tool", "tool_call_id": tid, "content": txt})

        # A pure tool-result carrier (Bedrock wraps results in a user message)
        # has nothing else to emit.
        if tool_results and not text_parts and not image_parts and not tool_calls:
            continue

        msg: dict = {"role": role}
        if image_parts:
            msg["content"] = text_parts + image_parts  # multimodal: list form
        elif text_parts:
            msg["content"] = "".join(p["text"] for p in text_parts)
        else:
            msg["content"] = None  # e.g. assistant message that's only tool_calls
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)

    return out


# ── Provider ──────────────────────────────────────────────────────────────────

class OpenAICompatService(LLMService):
    """Any OpenAI-compatible chat-completions endpoint (OpenRouter, Venice)."""

    supports_tools = True
    supports_cache = False  # no Bedrock-style prompt caching

    def __init__(self, model_id: str, api_key: str, base_url: str,
                 provider_label: str = "OpenAI-compatible",
                 supports_vision: bool = False,
                 extra_headers: dict | None = None,
                 require_api_key: bool = True):
        self.model_id = model_id
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._label = provider_label
        self.supports_vision = supports_vision
        self._extra_headers = extra_headers or {}
        self._require_api_key = require_api_key

    def _headers(self) -> dict:
        h: dict = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        h.update(self._extra_headers)
        return h

    async def _post_stream(self, payload: dict) -> AsyncGenerator[dict, None]:
        """POST /chat/completions (stream=True) and yield decoded SSE JSON events."""
        if not self._api_key and self._require_api_key:
            raise RuntimeError(f"{self._label}: no API key configured.")
        url = f"{self._base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise RuntimeError(f"{self._label} API error {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue

    def _base_payload(self, messages: list[dict], system_prompt, max_tokens: int = 4096) -> dict:
        # Temperature: kept at 0.7 for OpenAI-compatible providers (they accept it
        # and it's the historical default here) — the Opus-4.7 temperature ban that
        # motivated dropping it on Bedrock doesn't apply to these models (§3.7).
        return {
            "model": self.model_id,
            "messages": to_openai_messages(messages, system_prompt),
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }

    async def stream(self, messages: list[dict], system_prompt=None,
                     use_cache: bool = False, max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        # use_cache is accepted for interface parity; OpenAI-compatible providers
        # have no prompt caching.
        t0 = time.monotonic()
        first = True
        async for chunk in self._post_stream(self._base_payload(messages, system_prompt, max_tokens)):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            text = (choices[0].get("delta") or {}).get("content")
            if text:
                if first:
                    logger.info(f"[{self._label}] first chunk in {time.monotonic() - t0:.1f}s")
                    first = False
                yield text

    async def analyze(self, messages: list[dict], system_prompt=None,
                      use_cache: bool = False, max_tokens: int = 4096) -> str:
        parts = []
        async for chunk in self.stream(messages, system_prompt, max_tokens=max_tokens):
            parts.append(chunk)
        return "".join(parts)

    async def _stream_round(self, payload: dict, on_text):
        """One streamed completion: accumulate text + (incremental) tool calls."""
        text_acc = ""
        calls: dict[int, dict] = {}
        finish_reason = None
        async for chunk in self._post_stream(payload):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text_acc += delta["content"]
                if on_text:
                    await on_text(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(idx, {"id": None, "name": None, "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
        ordered = [calls[i] for i in sorted(calls)]
        return text_acc, ordered, finish_reason

    async def converse_stream_with_tools(
        self, messages: list[dict], system_prompt=None,
        tools: list[dict] | None = None, tool_handler=None, on_text=None,
        use_cache: bool = False,  # accepted for parity; no caching here
        max_tokens: int = 4096,
    ) -> tuple[str, list[dict]]:
        oai_tools = to_openai_tools(tools)
        all_text: list[str] = []

        for round_num in range(MAX_TOOL_ROUNDS):
            payload = self._base_payload(messages, system_prompt, max_tokens)
            if oai_tools:
                payload["tools"] = oai_tools
            logger.info(f"[{self._label}] round={round_num} model={self.model_id} messages={len(messages)}")
            text, calls, finish = await self._stream_round(payload, on_text)
            if text:
                all_text.append(text)

            # Record the assistant turn back in *Bedrock* shape so engine history
            # stays vendor-neutral.
            assistant_content: list[dict] = []
            if text:
                assistant_content.append({"text": text})
            parsed_calls = []
            for c in calls:
                try:
                    inp = json.loads(c["args"]) if c["args"] else {}
                except json.JSONDecodeError:
                    inp = {}
                parsed_calls.append((c, inp))
                assistant_content.append({
                    "toolUse": {"toolUseId": c["id"], "name": c["name"], "input": inp}
                })
            messages.append({"role": "assistant", "content": assistant_content})

            if finish != "tool_calls" or not parsed_calls or not tool_handler:
                return " ".join(all_text), messages

            result_blocks: list[dict] = []
            for c, inp in parsed_calls:
                logger.info(f"[{self._label}] tool_use: {c['name']}")
                if on_text:
                    await on_text(f"\n🔧 Using {c['name']}…\n")
                result = await tool_handler(c["name"], inp)
                result_blocks.append({
                    "toolResult": {
                        "toolUseId": c["id"],
                        "content": [{"text": json.dumps(result, default=str)}],
                    }
                })
            messages.append({"role": "user", "content": result_blocks})

        logger.warning(f"[{self._label}] hit max {MAX_TOOL_ROUNDS} tool-use rounds")
        return " ".join(all_text), messages
