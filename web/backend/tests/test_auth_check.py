"""Tests for Bedrock auth-status reporting (api-key primary, SSO fallback)."""

import os

from web.backend import auth_check
from web.backend.auth_check import AuthItem, bedrock_auth_error_message, check_auth


# ── check_auth mode selection ───────────────────────────────────────────────

def test_check_auth_api_key_mode(monkeypatch):
    """Env var + a region set → ok, mode 'api_key', no local expiry poll."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    monkeypatch.setenv("AWS_REGION", "us-east-1")  # region required in both modes
    # _check_sso must NOT run in api-key mode (there's no local token to poll).
    monkeypatch.setattr(
        auth_check, "_check_sso",
        lambda: (_ for _ in ()).throw(AssertionError("_check_sso should not run")),
    )
    status = check_auth()
    assert status.sso.ok is True
    assert status.sso.mode == "api_key"
    d = status.to_dict()
    assert d["sso"]["ok"] is True
    assert d["sso"]["mode"] == "api_key"
    # Payload stays backward-compatible: the keys the frontend reads still exist.
    assert set(d["sso"]) >= {"ok", "message", "command"}


def test_check_auth_api_key_mode_missing_region(monkeypatch):
    """API key set but no region → not ok, mode 'api_key', region-fix message."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    # _check_sso must NOT run in api-key mode even when the region is missing.
    monkeypatch.setattr(
        auth_check, "_check_sso",
        lambda: (_ for _ in ()).throw(AssertionError("_check_sso should not run")),
    )
    status = check_auth()
    assert status.sso.ok is False
    assert status.sso.mode == "api_key"
    assert "region" in status.sso.message.lower()
    assert status.sso.command == ""


def test_check_auth_api_key_mode_bedrock_region_ok(monkeypatch):
    """BEDROCK_REGION alone also satisfies the region requirement in api-key mode."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    status = check_auth()
    assert status.sso.ok is True
    assert status.sso.mode == "api_key"


def test_check_auth_blank_env_is_not_api_key_mode(monkeypatch):
    """A whitespace-only bearer token is treated as unset (falls back to SSO)."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "   ")
    sentinel = AuthItem("sso", True, message="from-sso", mode="sso")
    monkeypatch.setattr(auth_check, "_check_sso", lambda: sentinel)
    status = check_auth()
    assert status.sso is sentinel
    assert status.sso.mode == "sso"


def test_check_auth_sso_fallback_delegates(monkeypatch):
    """Env var unset → existing SSO-cache logic drives the status (mode 'sso')."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    sentinel = AuthItem("sso", False, message="AWS SSO token expired", command="aws sso login")
    monkeypatch.setattr(auth_check, "_check_sso", lambda: sentinel)
    status = check_auth()
    assert status.sso is sentinel
    assert status.sso.mode == "sso"  # default preserved for the existing path
    assert status.to_dict()["sso"]["command"] == "aws sso login"


def test_check_sso_missing_cache_steers_to_api_key(monkeypatch, tmp_path):
    """No SSO footprint at all → not-ok, but the remedy is the API key (no
    `aws sso login` hint — the user isn't an SSO user)."""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # default is now openrouter
    missing = str(tmp_path / "no-sso-cache")
    real_expanduser = os.path.expanduser
    monkeypatch.setattr(
        auth_check.os.path, "expanduser",
        lambda p: missing if p == "~/.aws/sso/cache" else real_expanduser(p),
    )
    status = check_auth()
    assert status.sso.ok is False
    assert status.sso.command == ""
    assert "Bedrock API key" in status.sso.message
    assert status.sso.mode == "sso"


# ── provider awareness (§ non-Bedrock providers involve no AWS) ─────────────

def test_check_auth_ok_for_non_bedrock_providers(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    for provider in ("ollama", "openrouter", "venice"):
        monkeypatch.setenv("LLM_PROVIDER", provider)
        status = check_auth()
        assert status.sso.ok is True, provider
        assert status.sso.mode == "none", provider
        assert status.sso.message == ""


def test_region_check_does_not_affect_non_bedrock(monkeypatch):
    """The api-key region requirement is Bedrock-only — non-bedrock providers stay
    ok (mode 'none') even with no region and a stray bearer token set."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    for provider in ("ollama", "openrouter", "venice"):
        monkeypatch.setenv("LLM_PROVIDER", provider)
        status = check_auth()
        assert status.sso.ok is True, provider
        assert status.sso.mode == "none", provider


def test_check_auth_unset_provider_is_none(monkeypatch):
    """Unset LLM_PROVIDER defaults to openrouter → no AWS check, mode 'none'."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    status = check_auth()
    assert status.sso.ok is True
    assert status.sso.mode == "none"
    assert status.sso.message == ""


# ── mode-aware error messaging ──────────────────────────────────────────────

def test_error_message_api_key_mode(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "sk-bedrock-abc123")
    msg = bedrock_auth_error_message()
    assert "Bedrock API key" in msg
    assert "Settings" in msg
    assert "aws sso login" not in msg


def test_error_message_sso_mode(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    msg = bedrock_auth_error_message()
    assert "aws sso login" in msg
