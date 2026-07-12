"""Bedrock auth status.

Auth model: a **Bedrock API key** (bearer token in ``AWS_BEARER_TOKEN_BEDROCK``)
is the primary path — botocore honours it natively for bedrock-runtime and the
Anthropic SDK's Mantle client resolves the same env var, so a single key powers
both providers with no AWS CLI/SSO/profile setup. The AWS SigV4 credential chain
(SSO / named profile) is the advanced fallback used only when that env var is
unset.

This module reports which mode is active. In API-key mode there is no local token
expiry to poll, so it simply reports ``ok`` — a bad/expired key only surfaces when
a Bedrock call fails, mapped to a friendly message by ``bedrock_auth_error_message``.
In SSO mode it keeps the original ``~/.aws/sso/cache`` expiry check that drives the
frontend auth banner.
"""

from __future__ import annotations

import configparser
import glob
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


_last_bedrock_success: float = 0.0  # monotonic timestamp of last successful Bedrock call

# Env var that holds a Bedrock API key (bearer token). Set → API-key auth mode;
# unset → SigV4 (SSO/profile) fallback. Same var botocore and the Anthropic SDK's
# Mantle client both resolve, so one key powers both Bedrock providers.
BEARER_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"


def _api_key_mode() -> bool:
    """True when a Bedrock API key (bearer token) is configured in the env."""
    return bool(os.environ.get(BEARER_TOKEN_ENV, "").strip())


def _has_region() -> bool:
    """True when a Bedrock region is configured (AWS_REGION or BEDROCK_REGION).

    A region is required in BOTH auth modes — it selects the bedrock-runtime
    endpoint, independent of how the request is signed. Without it, boto3 raises
    NoRegionError ("You must specify a region.") on the first call.
    """
    return bool(
        os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("BEDROCK_REGION", "").strip()
    )


def bedrock_auth_error_message() -> str:
    """Friendly message for a Bedrock auth failure, tailored to the active mode.

    API-key mode: nothing to run locally — point the user at Settings to check or
    rotate the key. SSO mode: the token has likely expired — point at
    ``aws sso login``. Used by both Bedrock providers so the two auth models get
    the right remediation hint.
    """
    if _api_key_mode():
        return (
            "Bedrock API key was rejected. Check or rotate your Bedrock API key in "
            "Settings (AWS Bedrock → Bedrock API key)."
        )
    return "AWS SSO token expired. Run `aws sso login` in your terminal, then retry."


def mark_bedrock_success():
    """Called by bedrock_service when a call completes successfully."""
    global _last_bedrock_success
    _last_bedrock_success = time.monotonic()


@dataclass
class AuthItem:
    name: str
    ok: bool
    message: str = ""
    command: str = ""
    # Which auth model this status reflects: "api_key" (bearer token) or "sso"
    # (SigV4 credential chain). Additive field; the frontend banner ignores it.
    mode: str = "sso"


@dataclass
class AuthStatus:
    sso: AuthItem = field(default_factory=lambda: AuthItem("sso", True))

    def to_dict(self) -> dict:
        return {
            "sso": {
                "ok": self.sso.ok,
                "message": self.sso.message,
                "command": self.sso.command,
                "mode": self.sso.mode,
            },
        }


def _token_expiry(path: str) -> datetime | None:
    """Parse an SSO cache file's token expiry, or None if it isn't a token file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Only token files have an accessToken (skip client registration files).
        if "accessToken" not in data:
            return None
        expires_str = data.get("expiresAt", "")
        if not expires_str:
            return None
        # Parse ISO format — handle both with and without Z suffix.
        expires_str = expires_str.replace("Z", "+00:00")
        expires = datetime.fromisoformat(expires_str)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires
    except Exception:
        return None


def _latest_token_expiry(cache_dir: str) -> datetime | None:
    """Latest-expiring token across all SSO cache files (the default heuristic)."""
    latest = None
    for path in glob.glob(os.path.join(cache_dir, "*.json")):
        expires = _token_expiry(path)
        if expires is None:
            continue
        if latest is None or expires > latest:
            latest = expires
    return latest


def _profile_token_expiry(profile: str, cache_dir: str) -> datetime | None:
    """Best-effort expiry of the SSO token belonging to `profile`.

    Resolves the profile's `sso_session` / `sso_start_url` from ~/.aws/config to
    the cache filename (sha1 of that value, per the AWS CLI) and reads only that
    token. Returns None if unresolvable so the caller falls back to the global
    latest-expiry scan (kept minimal and non-breaking) (§2.9).
    """
    key = _profile_sso_cache_key(profile)
    if not key:
        return None
    path = os.path.join(cache_dir, f"{key}.json")
    if not os.path.isfile(path):
        return None
    return _token_expiry(path)


def _profile_sso_cache_key(profile: str) -> str | None:
    """sha1 cache key for a profile's SSO token, or None if not resolvable."""
    config_path = os.path.expanduser("~/.aws/config")
    if not os.path.isfile(config_path):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path)
    except Exception:
        return None
    section = "default" if profile == "default" else f"profile {profile}"
    if not parser.has_section(section):
        if parser.has_section(profile):
            section = profile
        else:
            return None
    sso_session = parser.get(section, "sso_session", fallback="").strip()
    if sso_session:
        return hashlib.sha1(sso_session.encode("utf-8")).hexdigest()
    start_url = parser.get(section, "sso_start_url", fallback="").strip()
    if start_url:
        return hashlib.sha1(start_url.encode("utf-8")).hexdigest()
    return None


# Message shown when Bedrock is active but NO credential of either kind exists.
# The API key is the primary remedy; SSO is only suggested for existing SSO users.
_NO_CREDS_MSG = (
    "Bedrock credentials not set — add a Bedrock API key "
    "(Settings → AWS Bedrock, or AWS_BEARER_TOKEN_BEDROCK in .env)"
)


def _check_sso() -> AuthItem:
    """Check AWS SSO token expiration."""
    cache_dir = os.path.expanduser("~/.aws/sso/cache")
    if not os.path.isdir(cache_dir):
        # No SSO footprint at all → the user isn't an SSO user; steer to the key.
        return AuthItem("sso", False, _NO_CREDS_MSG, "")

    # Prefer the token for the configured profile when it resolves; otherwise
    # fall back to the latest-expiring token across all cache files.
    profile = os.environ.get("AWS_PROFILE", "").strip()
    latest_expires = _profile_token_expiry(profile, cache_dir) if profile else None
    if latest_expires is None:
        latest_expires = _latest_token_expiry(cache_dir)

    if latest_expires is None:
        return AuthItem("sso", False, _NO_CREDS_MSG, "")

    now = datetime.now(timezone.utc)
    remaining = (latest_expires - now).total_seconds()

    if remaining <= 0:
        # If Bedrock calls are succeeding, the credentials are still valid
        # despite what the cache file says — suppress the warning
        if _last_bedrock_success and (time.monotonic() - _last_bedrock_success) < 600:
            return AuthItem("sso", True)
        return AuthItem(
            "sso", False,
            "AWS SSO token expired (or set a Bedrock API key in Settings to stop using SSO)",
            "aws sso login",
        )
    elif remaining < 900:  # 15 minutes
        if _last_bedrock_success and (time.monotonic() - _last_bedrock_success) < 600:
            return AuthItem("sso", True)
        mins = max(1, int(remaining / 60))
        return AuthItem("sso", False, f"AWS SSO token expiring in {mins}m", "aws sso login")

    return AuthItem("sso", True)


def check_auth() -> AuthStatus:
    """Check Bedrock auth status — provider-aware.

    Non-Bedrock providers (openrouter/venice/ollama) involve no AWS at all, so
    the check reports ok (mode "none") and the banner never fires. LLM_PROVIDER
    defaults to openrouter, so an unset provider also reports mode "none". For
    Bedrock: API-key mode (``AWS_BEARER_TOKEN_BEDROCK`` set) reports ok with mode
    "api_key" — there is no local token to poll for expiry; a bad key only
    surfaces on a failing Bedrock call. Only Bedrock WITHOUT an API key falls
    back to the SSO-cache expiry check (mode "sso") that drives the banner.
    """
    status = AuthStatus()
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower() or "openrouter"
    if not provider.startswith("bedrock"):
        status.sso = AuthItem("sso", True, mode="none")
    elif _api_key_mode():
        # A Bedrock API key still needs a region to select the endpoint. Surface
        # the missing region up front (instead of a NoRegionError on first call).
        if not _has_region():
            status.sso = AuthItem(
                "sso", False,
                "AWS region not set — choose a region (Settings → AI Provider)",
                "",
                mode="api_key",
            )
        else:
            status.sso = AuthItem("sso", True, mode="api_key")
    else:
        status.sso = _check_sso()  # AuthItem.mode defaults to "sso"
    return status
