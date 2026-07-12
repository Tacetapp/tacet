"""Audio-bleed dedup — the pure matching algorithm, extracted from the engine (§6).

The meeting captures system audio and the microphone on two independent channels.
When the other party's voice leaks into the mic (speaker bleed), the mic channel
transcribes a near-duplicate of a system-audio line. These pure functions decide
whether a mic segment is such a cross-channel duplicate; the engine owns the
asyncio mic-pending buffering/flush orchestration and calls into here.

Nothing in this module touches asyncio or engine state — it's deliberately
side-effect-free so it can be unit-tested in isolation.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from web.backend.srt_parser import SrtSegment

# ── Audio bleed deduplication settings ─────────────────
DEDUP_WINDOW_S = 3.0         # seconds to buffer mic segments before confirming
DEDUP_TS_TOLERANCE = 15.0    # max timestamp difference (seconds) for match — very loose;
                             # mic and system channels emit SRT timestamps independently
                             # (each channel's VAD buffers separately), and the 20s
                             # VAD_MAX_SPEECH_S cap can make the same event land 10+ seconds
                             # apart in SRT-space. Text similarity is the real signal.
DEDUP_TEXT_THRESHOLD = 0.6   # min SequenceMatcher ratio for text match
DEDUP_CONTAINMENT_THRESHOLD = 0.8  # fraction of shorter text that must appear inside
                                   # longer text to count as a bleed fragment match
DEDUP_MIN_FRAGMENT_CHARS = 15      # ignore very short fragments in containment check

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


def ts_to_seconds(ts: str) -> float:
    """Parse SRT timestamp 'HH:MM:SS,mmm' to seconds."""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def normalize_text(s: str) -> str:
    """Lowercase + collapse punctuation/whitespace for dedup comparisons."""
    return " ".join(_NORMALIZE_RE.sub(" ", s.lower()).split())


def segments_match(a: SrtSegment, b: SrtSegment) -> bool:
    """Check if two segments are cross-channel duplicates (audio bleed).

    Two-stage match: first a loose full-text SequenceMatcher ratio (catches
    segments of similar length), then a containment fallback (catches the
    case where one channel's sub-segment boundaries split differently from
    the other's, so a mic fragment is a substring of a longer system line).
    """
    ts_diff = abs(ts_to_seconds(a.start) - ts_to_seconds(b.start))
    if ts_diff > DEDUP_TS_TOLERANCE:
        return False
    norm_a = normalize_text(a.text)
    norm_b = normalize_text(b.text)
    if not norm_a or not norm_b:
        return False
    if SequenceMatcher(None, norm_a, norm_b).ratio() > DEDUP_TEXT_THRESHOLD:
        return True
    # Containment fallback: the shorter text appears (nearly) verbatim inside the longer.
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if len(shorter) < DEDUP_MIN_FRAGMENT_CHARS:
        return False
    if shorter in longer:
        return True
    # Fuzzy containment: longest matching block covers enough of the shorter text.
    matcher = SequenceMatcher(None, shorter, longer)
    match = matcher.find_longest_match(0, len(shorter), 0, len(longer))
    return match.size >= DEDUP_CONTAINMENT_THRESHOLD * len(shorter)


def join_batch(segs: list[SrtSegment]) -> SrtSegment | None:
    """Collapse a batch of sub-segments into one synthetic segment for dedup.

    Whisper splits a single VAD buffer into multiple sub-segments, and the
    mic and system channels split at different boundaries. A mic fragment
    that straddles two system sub-segments won't match any one of them —
    but it will match the concatenated text of the whole batch.
    """
    if not segs:
        return None
    joined_text = " ".join(s.text for s in segs).strip()
    if not joined_text:
        return None
    return SrtSegment(
        index=segs[0].index, start=segs[0].start, end=segs[-1].end,
        text=joined_text, speaker=segs[0].speaker,
    )


def is_mic_bleed(mic_seg: SrtSegment,
                 system_segs: list[SrtSegment],
                 joined_system: SrtSegment | None) -> bool:
    """Check a mic segment against individual system segs AND their join."""
    if any(segments_match(mic_seg, s) for s in system_segs):
        return True
    if joined_system is not None and segments_match(mic_seg, joined_system):
        return True
    return False
