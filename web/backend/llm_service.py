"""Provider-agnostic LLM interface + factory.

The engine and screen analyzer talk to an ``LLMService``, not a specific vendor.
The active provider is selected by the ``LLM_PROVIDER`` env var (default
``openrouter``). Supported values:

- ``openrouter`` (default) — OpenRouter (OpenAI-compatible, wide model catalog)
- ``bedrock`` — AWS Bedrock via the official Anthropic SDK's Bedrock client
- ``venice`` — Venice (OpenAI-compatible, privacy-focused)
- ``ollama`` — Ollama local inference (OpenAI-compatible, no API key, fully
  on-device; screen analysis is unavailable because Ollama does not expose a
  vision endpoint)

Message format is the Bedrock "Converse" shape — role + content blocks
(``{"text"}``, ``{"image"}``, ``{"toolUse"}``, ``{"toolResult"}``). OpenAI-
compatible providers translate to/from this shape internally so the engine
never has to change.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import AsyncGenerator

# System prompts may be passed to the providers either as a plain ``str`` or as a
# structured "system blocks" list built by ``system_prompt_blocks`` below.
SystemPrompt = "str | list[dict] | None"


class InvalidModelError(RuntimeError):
    """A provider rejected a model ID as invalid for the user's region.

    Lives in this provider-neutral module (not bedrock_service) so the engine,
    ingest, and screen analyzer can catch it without importing a specific vendor
    client. Providers raise it when the backend reports the model identifier is
    not recognised (e.g. Bedrock's ValidationException "provided model identifier
    is invalid"). Carries the offending ``model_id`` so callers can name the exact
    Settings field in a user-facing message.
    """

    def __init__(self, model_id: str, message: str | None = None):
        self.model_id = model_id
        super().__init__(
            message
            or (
                f"Bedrock model ID '{model_id}' is not valid in your region — "
                "fix it in Settings → AI Provider."
            )
        )


def system_prompt_blocks(stable: str, volatile: str | None = None) -> list[dict]:
    """Build a structured system prompt with a stable/volatile split for caching.

    Returns a list of Bedrock "system block" dicts:

        [{"text": <stable>}, {"cachePoint": {"type": "default"}}, {"text": <volatile>}]

    The ``cachePoint`` marks the boundary between the STABLE prefix (assistant
    role, style rules, user profile — identical for the whole meeting) and the
    VOLATILE suffix (meeting context, rolling summary, transcript window — which
    changes every turn). Cache-capable providers (Bedrock) keep the cachePoint so
    only the stable prefix is cached and re-read; providers without prompt caching
    (OpenAI-compatible) drop the marker and concatenate the text blocks. Keeping
    the volatile content *after* the breakpoint is what makes the cache actually
    hit during a live meeting — a breakpoint after the transcript window would pay
    a fresh cache write every turn and never read it (§3.2). The marker is emitted
    unconditionally here; each provider decides, per request, whether to honour it
    (see ``use_cache`` / ``supports_cache``).
    """
    blocks: list[dict] = [{"text": stable}, {"cachePoint": {"type": "default"}}]
    if volatile:
        blocks.append({"text": volatile})
    return blocks


class LLMService(ABC):
    """Common interface for the chat / analysis / summary LLM calls."""

    # Set by the concrete provider.
    model_id: str = ""

    # Capability flags — callers (e.g. optional screen analysis) check these
    # instead of assuming a vendor's features.
    supports_vision: bool = False
    supports_tools: bool = False
    supports_cache: bool = False

    @abstractmethod
    async def converse_stream_with_tools(
        self, messages: list[dict], system_prompt=None,
        tools: list[dict] | None = None, tool_handler=None, on_text=None,
        use_cache: bool = False, max_tokens: int = 4096,
    ) -> tuple[str, list[dict]]:
        ...

    @abstractmethod
    def stream(
        self, messages: list[dict], system_prompt=None,
        use_cache: bool = False, max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        ...

    @abstractmethod
    async def analyze(
        self, messages: list[dict], system_prompt=None,
        use_cache: bool = False, max_tokens: int = 4096,
    ) -> str:
        ...

    @staticmethod
    def msg(role: str, text: str) -> dict:
        return {"role": role, "content": [{"text": text}]}


def provider_name() -> str:
    """Active provider id from LLM_PROVIDER (default 'openrouter')."""
    return os.environ.get("LLM_PROVIDER", "").strip().lower() or "openrouter"


def create_llm_service(
    model_id: str | None = None, region: str | None = None, fast: bool = False,
) -> LLMService:
    """Build the active LLM provider.

    Selected by LLM_PROVIDER (default 'openrouter'). Bedrock is imported lazily so
    this module has no hard dependency on the Anthropic SDK and there's no import
    cycle.

    ``fast=True`` builds the cheap "background" tier used for extraction/
    classification calls (analysis, curation, rolling summary, chat-insight,
    ingest) — see §3.1. For Bedrock that means the Haiku-class BEDROCK_FAST_MODEL_ID
    instead of the frontier BEDROCK_MODEL_ID. OpenAI-compatible providers reuse
    their normal model unless OPENROUTER_FAST_MODEL / VENICE_FAST_MODEL /
    OLLAMA_FAST_MODEL is set, so there is no tiering by default for those. An
    explicit ``model_id`` always wins (used by the vision path). The Bedrock
    provider normalizes legacy converse-style model IDs internally (§3.6).
    """
    provider = provider_name()
    if provider == "bedrock":
        from web.backend.bedrock_service import (
            BedrockService, DEFAULT_MODEL_ID, FAST_MODEL_ID,
        )
        default = FAST_MODEL_ID if fast else DEFAULT_MODEL_ID
        return BedrockService(model_id=model_id or default, region=region)
    if provider == "openrouter":
        # model_id arg is a Bedrock-ism (vision model id) — OpenAI-compatible
        # providers pick their model from env instead.
        from web.backend.openai_compat_service import OpenAICompatService
        model = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
        if fast:
            model = os.environ.get("OPENROUTER_FAST_MODEL", "").strip() or model
        return OpenAICompatService(
            model_id=model,
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            provider_label="OpenRouter",
            supports_vision=True,
            extra_headers={"X-Title": "Tacet"},
        )
    if provider == "venice":
        from web.backend.openai_compat_service import OpenAICompatService
        model = os.environ.get("VENICE_MODEL", "llama-3.3-70b")
        if fast:
            model = os.environ.get("VENICE_FAST_MODEL", "").strip() or model
        return OpenAICompatService(
            model_id=model,
            api_key=os.environ.get("VENICE_API_KEY", ""),
            base_url="https://api.venice.ai/api/v1",
            provider_label="Venice",
            supports_vision=False,
        )
    if provider == "ollama":
        from web.backend.openai_compat_service import OpenAICompatService
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        if fast:
            model = os.environ.get("OLLAMA_FAST_MODEL", "").strip() or model
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        return OpenAICompatService(
            model_id=model,
            api_key=os.environ.get("OLLAMA_API_KEY", ""),
            base_url=base_url,
            provider_label="Ollama",
            supports_vision=False,
            require_api_key=False,
        )
    if provider == "openai":
        raise NotImplementedError(
            "Direct OpenAI (ChatGPT OAuth) was dropped from scope; "
            "use OpenRouter to reach OpenAI models."
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")
