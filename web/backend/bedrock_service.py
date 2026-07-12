"""AWS Bedrock provider via the official Anthropic SDK (``AnthropicBedrockMantle``).

The Bedrock provider. Selected with ``LLM_PROVIDER=bedrock``; the engine and
screen analyzer reach it through the provider-neutral ``LLMService`` factory.

The engine speaks the Bedrock "Converse" message shape — role + content blocks
(``{"text"}``, ``{"image"}``, ``{"toolUse"}``, ``{"toolResult"}``,
``{"cachePoint"}``). This provider translates that shape to/from the Anthropic
Messages API shape internally and streams over the Anthropic SDK's async Bedrock
client, so the engine never sees an Anthropic shape — messages go in and come
back out in Bedrock shape (a drop-in swap behind the factory, exactly like
``OpenAICompatService``).

Model IDs are the ``anthropic.``-prefixed Messages-API Bedrock IDs
(e.g. ``anthropic.claude-opus-4-8``). Legacy converse-style IDs
(``us.anthropic.claude-opus-4-6-v1``) configured before this consolidation are
normalized to the bare form by ``_normalize_model_id`` (§3.6).

Auth: a Bedrock API key (``AWS_BEARER_TOKEN_BEDROCK``) is the primary path — the
SDK's Bedrock client resolves it and uses bearer auth. The AWS SigV4 credential
chain (SSO / named profile) is the advanced fallback, used only when that env
var is unset. Auth failures map to a friendly, mode-aware message via
``bedrock_auth_error_message``.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import time
from typing import AsyncGenerator

import anthropic

from web.backend.auth_check import bedrock_auth_error_message, mark_bedrock_success
from web.backend.llm_service import LLMService, InvalidModelError

logger = logging.getLogger(__name__)

# ``anthropic.``-prefixed Messages-API Bedrock model IDs. Env-overridable so they
# can be set from the settings page without code changes.
DEFAULT_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-opus-4-8")
# Cheap "background" tier for extraction/classification (analysis, curation,
# rolling summary, chat-insight, ingest) — see §3.1.
FAST_MODEL_ID = os.environ.get("BEDROCK_FAST_MODEL_ID", "anthropic.claude-haiku-4-5")
# Vision tier for periodic screen-capture analysis. Imported by main.py and
# passed as an explicit model_id down the factory (the vision path).
VISION_MODEL_ID = os.environ.get("BEDROCK_VISION_MODEL_ID", "anthropic.claude-sonnet-4-6")
# Region resolution: BEDROCK_REGION / AWS_REGION.
DEFAULT_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or None

MAX_TOOL_ROUNDS = 10


def _normalize_model_id(model_id: str) -> str:
    """Normalize a Bedrock model ID to the bare ``anthropic.``-prefixed form.

    Backward compatibility: values configured for the old boto3 ``converse``
    path were region-qualified cross-region IDs with a trailing ``-v1``
    (e.g. ``us.anthropic.claude-opus-4-6-v1``). The Messages-API Bedrock client
    wants the bare ``anthropic.<model>`` form, so strip a leading region
    qualifier (``us|eu|apac|global.``) and a trailing ``-v<n>`` suffix:
    ``us.anthropic.claude-opus-4-6-v1`` → ``anthropic.claude-opus-4-6``. An
    already-bare ID passes through unchanged.
    """
    if not model_id:
        return model_id
    model_id = re.sub(r"^(us|eu|apac|global)\.(?=anthropic\.)", "", model_id)
    model_id = re.sub(r"-v\d+$", "", model_id)
    return model_id

# botocore SSO/token errors surface during SigV4 signing (SSO/profile fallback
# mode only). Build the tuple defensively — names vary across botocore versions,
# and botocore is an indirect dependency here.
_AWS_TOKEN_ERRORS: tuple = ()
try:  # pragma: no cover - import guard
    from botocore import exceptions as _boto_exc

    _AWS_TOKEN_ERRORS = tuple(
        e
        for e in (
            getattr(_boto_exc, "TokenRetrievalError", None),
            getattr(_boto_exc, "SSOTokenLoadError", None),
            getattr(_boto_exc, "UnauthorizedSSOTokenError", None),
        )
        if isinstance(e, type)
    )
except Exception:  # pragma: no cover - botocore always present via boto3
    pass


def _is_invalid_model_error(exc: Exception) -> bool:
    """True only when an Anthropic SDK error is clearly about an invalid/unknown model.

    The Messages-API Bedrock endpoint surfaces a bad model id as an
    ``anthropic.NotFoundError`` or ``anthropic.BadRequestError``. Both classes are
    also raised for unrelated reasons (a genuinely missing resource, a malformed
    request), so this stays conservative: it maps to InvalidModelError only when
    the message names a model *and* an invalid/unknown indicator. Anything else
    re-raises unchanged.
    """
    if not isinstance(exc, (anthropic.NotFoundError, anthropic.BadRequestError)):
        return False
    text = (getattr(exc, "message", "") or str(exc) or "").lower()
    if "model" not in text:
        return False
    return any(
        kw in text
        for kw in (
            "identifier is invalid",
            "invalid model",
            "unknown model",
            "model not found",
            "not found",
            "does not exist",
            "unsupported model",
        )
    )


def _temperature() -> float | None:
    """Optional temperature from BEDROCK_TEMPERATURE.

    Returns None when unset or unparseable so the request omits ``temperature``
    entirely — recent Claude models (Opus 4.7+) reject a temperature parameter,
    so omitting by default is correct (§3.7).
    """
    raw = os.environ.get("BEDROCK_TEMPERATURE", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ── Translation: Bedrock Converse shape → Anthropic Messages shape ────────────

def to_anthropic_tools(tools: list[dict] | None) -> list[dict] | None:
    """Bedrock toolConfig tools → Anthropic tool definitions.

    ``{"toolSpec": {name, description, inputSchema: {json}}}`` →
    ``{name, description, input_schema}``.
    """
    if not tools:
        return None
    out = []
    for t in tools:
        spec = t.get("toolSpec", {})
        out.append({
            "name": spec.get("name"),
            "description": spec.get("description", ""),
            "input_schema": spec.get("inputSchema", {}).get("json", {}),
        })
    return out


def _image_block(block: dict) -> dict | None:
    """Bedrock image block → Anthropic base64 image content block."""
    img = block.get("image", {})
    fmt = img.get("format", "png")
    data = img.get("source", {}).get("bytes")
    if data is None:
        return None
    b64 = base64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else str(data)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": f"image/{fmt}", "data": b64},
    }


def to_anthropic_messages(messages: list[dict], caching: bool = True) -> list[dict]:
    """Bedrock-shape messages → Anthropic Messages content blocks.

    - ``text`` → ``{"type": "text", ...}``
    - ``image`` → base64 image block
    - ``toolUse`` → ``{"type": "tool_use", "id", "name", "input"}``
    - ``toolResult`` → ``{"type": "tool_result", "tool_use_id", "content": [...]}``
    - ``cachePoint`` → ``cache_control: {"type": "ephemeral"}`` on the PRECEDING
      block (when ``caching``); dropped otherwise.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", [])
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks: list[dict] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if "text" in b:
                blocks.append({"type": "text", "text": b["text"]})
            elif "image" in b:
                ib = _image_block(b)
                if ib:
                    blocks.append(ib)
            elif "toolUse" in b:
                tu = b["toolUse"]
                blocks.append({
                    "type": "tool_use",
                    "id": tu["toolUseId"],
                    "name": tu["name"],
                    "input": tu.get("input", {}),
                })
            elif "toolResult" in b:
                tr = b["toolResult"]
                tr_content = [
                    {"type": "text", "text": c["text"]}
                    for c in tr.get("content", [])
                    if isinstance(c, dict) and "text" in c
                ]
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr["toolUseId"],
                    "content": tr_content,
                })
            elif "cachePoint" in b:
                # cachePoint marks a breakpoint AFTER the block it applies to.
                if caching and blocks:
                    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
            # anything else → dropped
        out.append({"role": role, "content": blocks})
    return out


def to_anthropic_system(system_prompt, caching: bool = True):
    """Normalize a system prompt into the Anthropic ``system`` parameter.

    Accepts a plain ``str`` or the structured block list from
    ``llm_service.system_prompt_blocks`` (stable prefix, cachePoint marker,
    volatile suffix). The stable/volatile split maps to a ``system`` array of
    text blocks with ``cache_control`` on the STABLE one only (never after the
    volatile transcript window — §3.2). Without caching, a plain string is passed
    through as-is and the structured form is concatenated with the marker dropped.
    """
    if not system_prompt:
        return None
    if isinstance(system_prompt, str):
        if caching:
            return [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        return system_prompt
    blocks: list[dict] = []
    for b in system_prompt:
        if not isinstance(b, dict):
            continue
        if "text" in b:
            blocks.append({"type": "text", "text": b["text"]})
        elif "cachePoint" in b:
            if caching and blocks:
                blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks


def _from_anthropic_content(content_blocks) -> list[dict]:
    """Anthropic response content blocks → Bedrock-shape content blocks.

    Accepts SDK block objects (``.type`` / ``.text`` / ``.id`` / ``.name`` /
    ``.input``) or plain dicts (so it's easy to unit-test). ``thinking`` and other
    block types are ignored — the engine only consumes text and tool use.
    """
    def _attr(block, name):
        if isinstance(block, dict):
            return block.get(name)
        return getattr(block, name, None)

    out: list[dict] = []
    for block in content_blocks:
        btype = _attr(block, "type")
        if btype == "text":
            out.append({"text": _attr(block, "text") or ""})
        elif btype == "tool_use":
            out.append({
                "toolUse": {
                    "toolUseId": _attr(block, "id"),
                    "name": _attr(block, "name"),
                    "input": _attr(block, "input") or {},
                }
            })
    return out


# ── Provider ──────────────────────────────────────────────────────────────────

class BedrockService(LLMService):
    """Bedrock provider backed by the Anthropic SDK's async Bedrock client."""

    # Bedrock (Anthropic models) supports all three.
    supports_vision = True
    supports_tools = True
    supports_cache = True

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, region: str | None = None):
        # Import lazily so the module has no hard import-time dependency wiring;
        # the factory already imports this module lazily.
        from anthropic import AsyncAnthropicBedrockMantle

        region = region or DEFAULT_REGION
        kwargs: dict = {}
        if region:
            kwargs["aws_region"] = region
        # Only the region is passed — no api_key/aws_access_key/aws_secret_key/
        # aws_profile. That's deliberate: passing any of those would force the
        # SDK client's auth mode instead of letting it resolve from the env.
        # With none of them set, it uses a Bedrock API key when
        # AWS_BEARER_TOKEN_BEDROCK is present (primary path) and otherwise falls
        # back to the SigV4 credential chain (AWS_PROFILE / default session).
        self.client = AsyncAnthropicBedrockMantle(**kwargs)
        # Normalize legacy converse-style IDs (region qualifier + -v1) to the bare
        # anthropic.<model> form the Messages-API client expects (§3.6). Covers
        # both the env defaults and explicit model_ids (e.g. the vision path).
        self.model_id = _normalize_model_id(model_id)

    def _build_request(
        self, messages: list[dict], system_prompt, anthropic_tools: list[dict] | None,
        use_cache: bool, max_tokens: int,
    ) -> dict:
        caching = use_cache and self.supports_cache
        request: dict = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            "messages": to_anthropic_messages(messages, caching=caching),
        }
        system = to_anthropic_system(system_prompt, caching=caching)
        if system is not None:
            request["system"] = system
        if anthropic_tools:
            request["tools"] = anthropic_tools
        temp = _temperature()
        if temp is not None:
            request["temperature"] = temp
        return request

    async def _consume(self, request: dict, on_text=None):
        """Stream one request; forward text deltas to on_text; return final message.

        Maps AWS-credential/auth errors to a friendly, mode-aware RuntimeError;
        other typed errors propagate.
        """
        try:
            async with self.client.messages.stream(**request) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            text = getattr(delta, "text", "")
                            if text and on_text:
                                await on_text(text)
                final = await stream.get_final_message()
            mark_bedrock_success()
            return final
        except _AWS_TOKEN_ERRORS:
            logger.error("[Bedrock] AWS SSO token expired")
            raise RuntimeError(bedrock_auth_error_message())
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            # 401/403 — rejected/expired bearer, missing model access, or region
            # mismatch. Keep the service's own reason: it distinguishes an
            # expired short-term key from an un-enabled model instantly.
            detail = str(e)[:200]
            logger.error(f"[Bedrock] auth/permission failure: {detail}")
            raise RuntimeError(f"{bedrock_auth_error_message()} (service said: {detail})")
        except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
            # Bad/unavailable model id → friendly, actionable message. Only remap
            # errors clearly about the model; everything else propagates as-is.
            if _is_invalid_model_error(e):
                logger.error(f"[Bedrock] invalid model id '{self.model_id}': {e}")
                raise InvalidModelError(self.model_id)
            raise

    async def converse_stream_with_tools(
        self, messages: list[dict], system_prompt=None,
        tools: list[dict] | None = None,
        tool_handler=None,
        on_text=None,
        use_cache: bool = False,
        max_tokens: int = 4096,
    ) -> tuple[str, list[dict]]:
        """Unified streaming + tool use. Returns (final_text, updated_messages).

        Messages are returned in the engine's Bedrock shape. Operates on a private
        deep copy so cachePoint insertion and the assistant/tool-result appends
        never mutate the caller's message objects (§2.8).
        """
        messages = copy.deepcopy(messages)
        anthropic_tools = to_anthropic_tools(tools)
        caching = use_cache and self.supports_cache

        # Add a cache point to earlier chat history so prior turns are cached
        # (translated to cache_control downstream).
        if caching and len(messages) >= 3:
            for i in range(len(messages) - 2, -1, -1):
                if messages[i]["role"] == "user":
                    content = messages[i]["content"]
                    if isinstance(content, list) and not any(
                        isinstance(b, dict) and "cachePoint" in b for b in content
                    ):
                        content.append({"cachePoint": {"type": "default"}})
                    break

        all_text_parts: list[str] = []

        for round_num in range(MAX_TOOL_ROUNDS):
            request = self._build_request(
                messages, system_prompt, anthropic_tools, use_cache, max_tokens,
            )
            logger.info(
                f"[Bedrock] round={round_num} model={self.model_id} "
                f"messages={len(messages)}"
            )
            t0 = time.monotonic()
            final = await self._consume(request, on_text=on_text)
            logger.info(
                f"[Bedrock] round={round_num} completed in "
                f"{time.monotonic() - t0:.1f}s stop={getattr(final, 'stop_reason', None)}"
            )

            content_blocks = _from_anthropic_content(getattr(final, "content", []))
            messages.append({"role": "assistant", "content": content_blocks})
            all_text_parts.extend(b["text"] for b in content_blocks if "text" in b)

            stop_reason = getattr(final, "stop_reason", None)
            tool_uses = [b for b in content_blocks if "toolUse" in b]
            if stop_reason != "tool_use" or not tool_uses or not tool_handler:
                return " ".join(all_text_parts), messages

            tool_results = []
            for block in tool_uses:
                tool = block["toolUse"]
                logger.info(
                    f"[Bedrock] tool_use: {tool['name']} "
                    f"input_keys={list(tool['input'].keys())}"
                )
                if on_text:
                    await on_text(f"\n🔧 Using {tool['name']}…\n")
                result = await tool_handler(tool["name"], tool["input"])
                serialized = json.dumps(result, default=str)
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool["toolUseId"],
                        "content": [{"text": serialized}],
                    }
                })
            messages.append({"role": "user", "content": tool_results})

        logger.warning(f"[Bedrock] hit max {MAX_TOOL_ROUNDS} tool-use rounds")
        return " ".join(all_text_parts), messages

    # ── Convenience wrappers ──────────────────────────────

    async def stream(
        self, messages: list[dict], system_prompt=None,
        use_cache: bool = False, max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """Simple streaming — yields text chunks. No tool use."""
        request = self._build_request(
            messages, system_prompt, None, use_cache, max_tokens,
        )
        logger.info(
            f"[Bedrock] model={self.model_id} messages={len(messages)}"
        )
        try:
            async with self.client.messages.stream(**request) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield text
            mark_bedrock_success()
        except _AWS_TOKEN_ERRORS:
            logger.error("[Bedrock] AWS SSO token expired")
            raise RuntimeError(bedrock_auth_error_message())
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            # 401/403 — rejected/expired bearer, missing model access, or region
            # mismatch. Keep the service's own reason: it distinguishes an
            # expired short-term key from an un-enabled model instantly.
            detail = str(e)[:200]
            logger.error(f"[Bedrock] auth/permission failure: {detail}")
            raise RuntimeError(f"{bedrock_auth_error_message()} (service said: {detail})")
        except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
            # Bad/unavailable model id → friendly, actionable message. Only remap
            # errors clearly about the model; everything else propagates as-is.
            if _is_invalid_model_error(e):
                logger.error(f"[Bedrock] invalid model id '{self.model_id}': {e}")
                raise InvalidModelError(self.model_id)
            raise

    async def analyze(
        self, messages: list[dict], system_prompt=None,
        use_cache: bool = False, max_tokens: int = 4096,
    ) -> str:
        """Non-streaming convenience: collect full response as string."""
        chunks = []
        async for chunk in self.stream(
            messages, system_prompt, use_cache=use_cache, max_tokens=max_tokens,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    @staticmethod
    def msg(role: str, text: str) -> dict:
        return {"role": role, "content": [{"text": text}]}
