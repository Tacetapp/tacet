"""Tests for the provider-agnostic LLM interface + factory."""

import pytest

from web.backend.llm_service import LLMService, create_llm_service, provider_name


def test_provider_name_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert provider_name() == "openrouter"


def test_provider_name_env_normalized(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "  OpenRouter ")
    assert provider_name() == "openrouter"


def test_factory_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "frobnicate")
    with pytest.raises(ValueError):
        create_llm_service()


def test_factory_openai_dropped_raises(monkeypatch):
    # Direct OpenAI (ChatGPT OAuth) was dropped from scope.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(NotImplementedError):
        create_llm_service()


def test_factory_builds_openai_compatible_providers(monkeypatch):
    from web.backend.openai_compat_service import OpenAICompatService

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    svc = create_llm_service()
    assert isinstance(svc, OpenAICompatService)
    assert svc.supports_tools and not svc.supports_cache and svc.supports_vision

    monkeypatch.setenv("LLM_PROVIDER", "venice")
    monkeypatch.setenv("VENICE_API_KEY", "test-key")
    v = create_llm_service()
    assert isinstance(v, OpenAICompatService)
    assert not v.supports_vision


def test_factory_default_is_openrouter(monkeypatch):
    """Unset LLM_PROVIDER now builds the OpenRouter (OpenAI-compatible) provider."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from web.backend.openai_compat_service import OpenAICompatService

    svc = create_llm_service()
    assert isinstance(svc, LLMService)
    assert isinstance(svc, OpenAICompatService)
    assert svc._label == "OpenRouter"


def test_factory_fast_tier_uses_fast_model(monkeypatch):
    """fast=True builds the cheap background tier for Bedrock — §3.1."""
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrockMantle", lambda **kw: object())
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    from web.backend.bedrock_service import DEFAULT_MODEL_ID, FAST_MODEL_ID

    primary = create_llm_service(region="us-east-1")
    fast = create_llm_service(region="us-east-1", fast=True)
    assert primary.model_id == DEFAULT_MODEL_ID
    assert fast.model_id == FAST_MODEL_ID


def test_factory_openrouter_fast_model_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "base/model")
    # Unset → fast reuses the base model (no tiering by default).
    monkeypatch.delenv("OPENROUTER_FAST_MODEL", raising=False)
    assert create_llm_service(fast=True).model_id == "base/model"
    # Set → fast uses the override.
    monkeypatch.setenv("OPENROUTER_FAST_MODEL", "cheap/model")
    assert create_llm_service(fast=True).model_id == "cheap/model"
    assert create_llm_service(fast=False).model_id == "base/model"


def test_factory_ollama_provider(monkeypatch):
    """Ollama branch returns OpenAICompatService with right url/model/label."""
    from web.backend.openai_compat_service import OpenAICompatService

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    svc = create_llm_service()
    assert isinstance(svc, OpenAICompatService)
    assert svc.model_id == "llama3.1"
    assert svc._base_url == "http://localhost:11434/v1"
    assert svc._label == "Ollama"
    assert not svc.supports_vision
    assert not svc._require_api_key
    assert svc._api_key == ""


def test_factory_ollama_default_base_url(monkeypatch):
    """Ollama uses http://localhost:11434/v1 when OLLAMA_BASE_URL is unset."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    svc = create_llm_service()
    assert svc._base_url == "http://localhost:11434/v1"
    assert svc.model_id == "llama3.1"


def test_factory_ollama_fast_tier_fallback(monkeypatch):
    """fast=True with no OLLAMA_FAST_MODEL falls back to the base model."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.delenv("OLLAMA_FAST_MODEL", raising=False)
    svc = create_llm_service(fast=True)
    assert svc.model_id == "llama3.1"


def test_factory_ollama_fast_tier_override(monkeypatch):
    """OLLAMA_FAST_MODEL is used for fast=True; base model for fast=False."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.setenv("OLLAMA_FAST_MODEL", "qwen2.5:3b")
    assert create_llm_service(fast=True).model_id == "qwen2.5:3b"
    assert create_llm_service(fast=False).model_id == "llama3.1"


def test_msg_shape():
    assert LLMService.msg("user", "hi") == {"role": "user", "content": [{"text": "hi"}]}


@pytest.mark.parametrize("api_key", ["", "test-local-token"])
def test_ollama_optional_auth_header(monkeypatch, api_key):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_API_KEY", api_key)
    for fast in (False, True):
        svc = create_llm_service(fast=fast)
        assert not svc._require_api_key
        assert svc._headers().get("Authorization") == (
            f"Bearer {api_key}" if api_key else None
        )
