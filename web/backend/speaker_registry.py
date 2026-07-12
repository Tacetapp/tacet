"""Persistent, opt-in speaker voiceprint registry (§7).

Remembers named speaker voiceprints across meetings so that a confirmed
"Speaker 3 = Alice" is recognized from Alice's first utterance in the next
meeting — name once, recognized forever.

STRICTLY OPT-IN. Everything here is gated on ``SPEAKER_REGISTRY_ENABLED``:
with it off (the default) nothing is read or written and behavior is
byte-identical to a build without this module. Storage is a single JSON file at
``SPEAKER_REGISTRY_PATH`` (default: a ``speaker-registry.json`` sibling of the
meetings dir), never any audio.

Privacy (see EXPERT-REVIEW §9.3 #5): persisted voiceprints of third parties are
biometric-adjacent and carry statutory risk (e.g. Illinois BIPA). Entries are
per-person (only names the user explicitly confirms), inspectable via
``status()`` (which never returns the raw centroid), and deletable via
``delete()`` / ``clear()``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)

# Serializes the read-modify-write cycle on the JSON file so concurrent enrolls
# / deletes can't clobber each other.
_lock = threading.Lock()

# Exponential blend weight applied to a freshly confirmed centroid when a stored
# one already exists: stored = (1 - rate) * stored + rate * fresh. Small so the
# accumulated voiceprint stays stable while still adapting across meetings.
_BLEND_RATE = 0.3


def enabled() -> bool:
    """True iff the persistent registry is opted in via env. Cheap; called on
    every confirm/seed so it must not touch the filesystem."""
    raw = os.environ.get("SPEAKER_REGISTRY_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _default_path() -> str:
    """`<meetings dir>/../speaker-registry.json`, using the same SESSIONS_DIR that
    session_store computes (so it tracks MEETINGS_DIR / test reloads)."""
    from web.backend import session_store
    return os.path.normpath(
        os.path.join(session_store.SESSIONS_DIR, "..", "speaker-registry.json")
    )


def registry_path() -> str:
    env = os.environ.get("SPEAKER_REGISTRY_PATH", "").strip()
    if env:
        return os.path.normpath(os.path.expanduser(env))
    return _default_path()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw() -> dict:
    """Read the on-disk registry, tolerating a missing or corrupt file.

    Returns ``{"people": {name: entry}}``. A malformed file is treated as empty
    (and left untouched) so one bad write never bricks diarization.
    """
    path = registry_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"people": {}}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning(f"Speaker registry unreadable ({path}): {e}; treating as empty")
        return {"people": {}}
    if not isinstance(data, dict):
        return {"people": {}}
    people = data.get("people")
    if not isinstance(people, dict):
        people = {}
    return {"people": people}


def _save_raw(data: dict) -> None:
    """Atomic write (temp file + os.replace) so a crash can't leave a half file."""
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _unit(vec) -> np.ndarray | None:
    """Coerce to a unit-norm float32 1-D vector, or None if empty/degenerate."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return None
    norm = float(np.linalg.norm(arr))
    if norm < 1e-8:
        return None
    return arr / norm


def load_centroids() -> dict[str, np.ndarray]:
    """{name: unit-norm centroid} for seeding an embedder at meeting start.

    Returns an empty dict when disabled (so seeding is a no-op).
    """
    if not enabled():
        return {}
    with _lock:
        raw = _load_raw()
    out: dict[str, np.ndarray] = {}
    for name, entry in raw["people"].items():
        if not isinstance(entry, dict):
            continue
        vec = _unit(entry.get("centroid"))
        if vec is not None and isinstance(name, str) and name.strip():
            out[name] = vec
    return out


def enroll(name: str, centroid) -> None:
    """Persist / update a confirmed speaker's voiceprint. No-op when disabled.

    Blends exponentially with any existing stored centroid, renormalizes, and
    bumps meeting_count.
    """
    if not enabled():
        return
    name = (name or "").strip()
    if not name:
        return
    fresh = _unit(centroid)
    if fresh is None:
        return
    with _lock:
        raw = _load_raw()
        people = raw["people"]
        existing = people.get(name)
        meeting_count = 1
        if isinstance(existing, dict):
            prev = _unit(existing.get("centroid"))
            if prev is not None and prev.shape == fresh.shape:
                blended = (1.0 - _BLEND_RATE) * prev + _BLEND_RATE * fresh
                blended = _unit(blended)
                if blended is not None:
                    fresh = blended
            meeting_count = int(existing.get("meeting_count", 0) or 0) + 1
        people[name] = {
            "name": name,
            "centroid": [float(x) for x in fresh.tolist()],
            "updated_at": _now_iso(),
            "meeting_count": meeting_count,
        }
        _save_raw(raw)
    logger.info(f"Speaker registry: enrolled {name!r} (meeting_count={meeting_count})")


def status() -> dict:
    """{enabled, people: [{name, updated_at, meeting_count}]} — never centroids.

    When disabled, ``enabled`` is False and ``people`` empty; the frontend uses
    this to hide the "known voices" UI entirely.
    """
    if not enabled():
        return {"enabled": False, "people": []}
    with _lock:
        raw = _load_raw()
    people = [
        {
            "name": entry.get("name", name) if isinstance(entry, dict) else name,
            "updated_at": entry.get("updated_at", "") if isinstance(entry, dict) else "",
            "meeting_count": int(entry.get("meeting_count", 0) or 0)
            if isinstance(entry, dict) else 0,
        }
        for name, entry in sorted(raw["people"].items())
    ]
    return {"enabled": True, "people": people}


def delete(name: str) -> bool:
    """Forget one person. Returns True if an entry was removed. No-op when disabled."""
    if not enabled():
        return False
    name = (name or "").strip()
    if not name:
        return False
    with _lock:
        raw = _load_raw()
        if name in raw["people"]:
            del raw["people"][name]
            _save_raw(raw)
            logger.info(f"Speaker registry: deleted {name!r}")
            return True
    return False


def clear() -> None:
    """Forget everyone. No-op when disabled."""
    if not enabled():
        return
    with _lock:
        _save_raw({"people": {}})
    logger.info("Speaker registry: cleared")
