"""SRT subtitle file parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})")


@dataclass(frozen=True)
class SrtSegment:
    index: int
    start: str
    end: str
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["speaker"] is None:
            del d["speaker"]
        return d


def parse_srt(content: str) -> list[SrtSegment]:
    segments: list[SrtSegment] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        m = _TS_RE.match(lines[1].strip())
        if not m:
            continue
        text = "\n".join(lines[2:]).strip()
        if text:
            segments.append(SrtSegment(index=index, start=m.group(1), end=m.group(2), text=text))
    return segments


def parse_srt_file(path: str) -> list[SrtSegment]:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_srt(f.read())
    except FileNotFoundError:
        return []


def segments_to_text(segments: list[SrtSegment]) -> str:
    parts = []
    for s in segments:
        if s.speaker:
            parts.append(f"[{s.speaker}] {s.text}")
        else:
            parts.append(s.text)
    return " ".join(parts)
