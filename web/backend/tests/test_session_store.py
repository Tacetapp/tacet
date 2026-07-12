"""Tests for session_store transcript-line parsing (§2.9 regression)."""

from web.backend.session_store import (
    _parse_transcript,
    _session_to_markdown,
    _markdown_to_session,
)


def _block(*lines: str) -> str:
    """Wrap transcript lines in the fenced block the parser expects."""
    return "```transcript\n" + "\n".join(lines) + "\n```"


def test_no_speaker_text_with_colon():
    # A time-like prefix must not be captured as a speaker.
    segs = _parse_transcript(_block("[00:00:01,000 --> 00:00:03,000] 10:30 works for me"))
    assert len(segs) == 1
    assert "speaker" not in segs[0]
    assert segs[0]["text"] == "10:30 works for me"


def test_text_starting_with_time_and_colon():
    segs = _parse_transcript(_block("[00:00:01,000 --> 00:00:03,000] 10:30: sync at noon"))
    assert "speaker" not in segs[0]
    assert segs[0]["text"] == "10:30: sync at noon"


def test_real_speaker_cluster():
    segs = _parse_transcript(_block("[00:00:01,000 --> 00:00:03,000] Speaker 3: Hello there"))
    assert segs[0]["speaker"] == "Speaker 3"
    assert segs[0]["text"] == "Hello there"


def test_speaker_you():
    segs = _parse_transcript(_block("[00:00:01,000 --> 00:00:03,000] You: I agree"))
    assert segs[0]["speaker"] == "You"
    assert segs[0]["text"] == "I agree"


def test_multiword_name_speaker():
    segs = _parse_transcript(_block("[00:00:01,000 --> 00:00:03,000] Alice Chen: sounds good"))
    assert segs[0]["speaker"] == "Alice Chen"
    assert segs[0]["text"] == "sounds good"


def test_writer_parser_round_trip_backward_compatible():
    """Writer output still parses back with speakers intact (format unchanged)."""
    data = {
        "id": "2026-01-01-0000",
        "meeting_context": {"title": "Sync"},
        "segments": [
            {"start": "00:00:01,000", "end": "00:00:02,000", "text": "Hello", "speaker": "Speaker 3"},
            {"start": "00:00:02,000", "end": "00:00:03,000", "text": "10:30 works for me"},
            {"start": "00:00:03,000", "end": "00:00:04,000", "text": "Yep", "speaker": "You"},
        ],
    }
    md = _session_to_markdown(data)
    parsed = _markdown_to_session(md)["segments"]
    assert parsed[0]["speaker"] == "Speaker 3"
    assert parsed[0]["text"] == "Hello"
    assert "speaker" not in parsed[1]
    assert parsed[1]["text"] == "10:30 works for me"
    assert parsed[2]["speaker"] == "You"
