"""Load the user's profile (freeform Markdown) for prompt personalization.

The profile is a human-readable, growable Markdown document — sections like
Role, Expertise, Organization, Typical counterparts, Notes. It replaces the old
structured ``user_profile.json`` and is injected verbatim as a context block
into the soul (live system) and summary prompts, so the assistant adapts to who
the user actually is instead of a hardcoded persona.

Location precedence:
  1. ``USER_PROFILE_PATH`` env — the desktop app points this at
     ``userData/profile.md`` (read in dev, writable when packaged).
  2. ``<repo root>/profile.md``.

Returns the markdown text, or a neutral default when absent/blank so prompts
never carry a stale or invented persona.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_PATH = os.path.join(_REPO_ROOT, "profile.md")

# Used only when no profile file exists yet — keeps prompts generic (no persona).
DEFAULT_PROFILE = "(No profile set yet — treat the user as a general professional.)"


def profile_path() -> str:
    """Resolved profile path: USER_PROFILE_PATH env, else <repo root>/profile.md."""
    return os.environ.get("USER_PROFILE_PATH", "").strip() or _DEFAULT_PATH


def load_user_profile(path: str | None = None) -> str:
    """Read the profile markdown, or DEFAULT_PROFILE if missing/blank/unreadable."""
    p = path or profile_path()
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read().strip()
        return text or DEFAULT_PROFILE
    except FileNotFoundError:
        return DEFAULT_PROFILE
    except Exception as e:
        logger.warning(f"Failed to read profile at {p}: {e}")
        return DEFAULT_PROFILE
