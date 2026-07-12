"""Tests for SRT parser."""

import tempfile, os
from web.backend.srt_parser import parse_srt, parse_srt_file, segments_to_text

SAMPLE = """1
00:00:01,000 --> 00:00:03,000
Hello everyone.

2
00:00:03,500 --> 00:00:06,000
Let's get started with the meeting.

3
00:00:06,500 --> 00:00:09,000
First topic is the Q1 roadmap.
"""


def test_parse_srt_basic():
    segs = parse_srt(SAMPLE)
    assert len(segs) == 3
    assert segs[0].index == 1
    assert segs[0].text == "Hello everyone."
    assert segs[2].start == "00:00:06,500"


def test_parse_srt_empty():
    assert parse_srt("") == []
    assert parse_srt("   \n\n  ") == []


def test_parse_srt_partial_block():
    partial = """1
00:00:01,000 --> 00:00:03,000
Hello everyone.

2
00:00:03,500 -->"""
    segs = parse_srt(partial)
    assert len(segs) == 1


def test_parse_srt_file_not_found():
    assert parse_srt_file("/nonexistent/path.srt") == []


def test_parse_srt_file_reads():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False) as f:
        f.write(SAMPLE)
        f.flush()
        segs = parse_srt_file(f.name)
    os.unlink(f.name)
    assert len(segs) == 3


def test_segments_to_text():
    segs = parse_srt(SAMPLE)
    text = segments_to_text(segs)
    assert "Hello everyone." in text
    assert "Q1 roadmap" in text


def test_to_dict():
    seg = parse_srt(SAMPLE)[0]
    d = seg.to_dict()
    assert d == {"index": 1, "start": "00:00:01,000", "end": "00:00:03,000", "text": "Hello everyone."}
