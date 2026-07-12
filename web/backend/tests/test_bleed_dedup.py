"""Unit tests for the extracted audio-bleed dedup algorithm (web/backend/bleed_dedup.py).

Covers the matching paths the engine relies on: exact-duplicate match, exact
containment, fuzzy containment, timestamp-tolerance reject, short-fragment
reject, plus join_batch and is_mic_bleed behavior.
"""

from web.backend import bleed_dedup as bd
from web.backend.srt_parser import SrtSegment


def _seg(text, start="00:00:01,000", end="00:00:04,000", index=1, speaker=None):
    return SrtSegment(index=index, start=start, end=end, text=text, speaker=speaker)


# ── helpers ──────────────────────────────────────────────

def test_ts_to_seconds_parses_srt_timestamp():
    assert bd.ts_to_seconds("00:00:00,000") == 0.0
    assert bd.ts_to_seconds("00:00:01,500") == 1.5
    assert bd.ts_to_seconds("01:02:03,250") == 3600 + 120 + 3 + 0.25


def test_normalize_text_lowercases_and_strips_punctuation():
    assert bd.normalize_text("Hello, WORLD!!  Foo—bar") == "hello world foo bar"
    assert bd.normalize_text("   ") == ""


# ── segments_match ───────────────────────────────────────

def test_exact_duplicate_match():
    """Same text on both channels at the same time is a match (ratio path)."""
    a = _seg("Let's finalize the pricing proposal today.")
    b = _seg("Let's finalize the pricing proposal today.")
    assert bd.segments_match(a, b) is True


def test_containment_match():
    """A mic fragment that is a verbatim substring of a much longer system line
    matches via the exact-containment branch (ratio too low to match on its own)."""
    fragment = "we should finalize the pricing proposal before the end of this quarter"
    longer = ("so anyway everyone " + fragment + " and then we can move on to the next "
              "agenda item which is the hiring plan and the budget for next year overall")
    a = _seg(fragment)
    b = _seg(longer)
    # Sanity: full-text ratio alone is below threshold, so this exercises containment.
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, bd.normalize_text(fragment), bd.normalize_text(longer)).ratio()
    assert ratio <= bd.DEDUP_TEXT_THRESHOLD
    assert bd.segments_match(a, b) is True


def test_fuzzy_containment_match():
    """A fragment that is *almost* contained (differs only by a short tail) still
    matches via the longest-matching-block fuzzy branch."""
    common = "we should finalize the pricing proposal before the end of this quarter"
    fragment = common + " zzz"  # tail not present in the longer line
    longer = ("so anyway everyone " + common + " and then we can move on to the next "
              "agenda item which is the hiring plan and the budget for next year overall")
    a = _seg(fragment)
    b = _seg(longer)
    # Not an exact substring, and full-text ratio is below threshold — only the
    # fuzzy longest-block branch can return True here.
    assert bd.normalize_text(fragment) not in bd.normalize_text(longer)
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, bd.normalize_text(fragment), bd.normalize_text(longer)).ratio()
    assert ratio <= bd.DEDUP_TEXT_THRESHOLD
    assert bd.segments_match(a, b) is True


def test_timestamp_tolerance_reject():
    """Identical text but timestamps more than DEDUP_TS_TOLERANCE apart is not a
    match — the two channels are too far separated in SRT-space."""
    a = _seg("Let's finalize the pricing proposal today.", start="00:00:01,000")
    b = _seg("Let's finalize the pricing proposal today.", start="00:00:30,000")
    assert abs(bd.ts_to_seconds(a.start) - bd.ts_to_seconds(b.start)) > bd.DEDUP_TS_TOLERANCE
    assert bd.segments_match(a, b) is False


def test_short_fragment_reject():
    """A sub-DEDUP_MIN_FRAGMENT_CHARS fragment is ignored by the containment
    check even when it appears inside the longer text (too little signal)."""
    fragment = "okay sure"  # 9 chars normalized, < DEDUP_MIN_FRAGMENT_CHARS
    longer = ("okay sure lets talk about the quarterly roadmap and the staffing plan "
              "for the platform team over the next two quarters in some detail")
    assert len(bd.normalize_text(fragment)) < bd.DEDUP_MIN_FRAGMENT_CHARS
    assert bd.segments_match(_seg(fragment), _seg(longer)) is False


def test_empty_text_reject():
    assert bd.segments_match(_seg(""), _seg("something said here")) is False


# ── join_batch ───────────────────────────────────────────

def test_join_batch_concatenates_and_spans():
    segs = [
        _seg("first part", start="00:00:01,000", end="00:00:02,000", index=5, speaker="Speaker 1"),
        _seg("second part", start="00:00:02,000", end="00:00:03,000", index=6, speaker="Speaker 1"),
        _seg("third part", start="00:00:03,000", end="00:00:05,000", index=7, speaker="Speaker 1"),
    ]
    joined = bd.join_batch(segs)
    assert joined is not None
    assert joined.text == "first part second part third part"
    assert joined.index == 5           # first segment's index
    assert joined.start == "00:00:01,000"  # first segment's start
    assert joined.end == "00:00:05,000"    # last segment's end
    assert joined.speaker == "Speaker 1"


def test_join_batch_empty_returns_none():
    assert bd.join_batch([]) is None


def test_join_batch_all_blank_returns_none():
    assert bd.join_batch([_seg("   "), _seg("")]) is None


# ── is_mic_bleed ─────────────────────────────────────────

def test_is_mic_bleed_matches_individual_system_seg():
    mic = _seg("let's finalize the pricing proposal today")
    system = [_seg("completely unrelated chatter here"),
              _seg("let's finalize the pricing proposal today")]
    assert bd.is_mic_bleed(mic, system, None) is True


def test_is_mic_bleed_matches_only_the_join():
    """The mic line straddles several short system sub-segments: each is too
    short to match on its own (sub-fragment reject + low ratio), but their
    concatenation matches the mic line."""
    mic = _seg("ship the app review docs approve now")
    system = [
        _seg("ship the app", start="00:00:01,000", end="00:00:02,000", index=1),
        _seg("review docs", start="00:00:02,000", end="00:00:03,000", index=2),
        _seg("approve now", start="00:00:03,000", end="00:00:04,000", index=3),
    ]
    joined = bd.join_batch(system)
    assert bd.is_mic_bleed(mic, system, None) is False   # no single short seg matches
    assert bd.is_mic_bleed(mic, system, joined) is True  # the join does


def test_is_mic_bleed_no_match():
    mic = _seg("i think we should ship it on friday")
    system = [_seg("the weather is quite nice outside today")]
    assert bd.is_mic_bleed(mic, system, bd.join_batch(system)) is False
