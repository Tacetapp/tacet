"""Meeting metadata (title, attendees, agenda) attached to a session.

A small value object used by the engine for prompt context and persisted with
each saved session. Populated from the in-app chat tool
`update_session_meeting_context`; defaults to an "Unknown Meeting" placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MeetingContext:
    title: str = "Unknown Meeting"
    attendees: list[str] = field(default_factory=list)
    agenda: str = ""
    start_time: str = ""
    end_time: str = ""

    def to_prompt_text(self) -> str:
        parts = [f"Meeting: {self.title}"]
        if self.start_time:
            parts.append(f"Time: {self.start_time} - {self.end_time}")
        if self.attendees:
            parts.append(f"Attendees: {', '.join(self.attendees)}")
        if self.agenda:
            parts.append(f"Agenda: {self.agenda}")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {"title": self.title, "attendees": self.attendees, "agenda": self.agenda,
                "start_time": self.start_time, "end_time": self.end_time}

    @classmethod
    def from_dict(cls, d: dict) -> "MeetingContext":
        if not d:
            return cls()
        return cls(**{k: d[k] for k in ("title", "attendees", "agenda", "start_time", "end_time") if k in d})
