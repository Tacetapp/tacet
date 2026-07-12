"""Tests for MeetingEngine.current_initial_prompt — layered vocabulary biasing."""

from web.backend.engine import MeetingEngine
from web.backend.meeting_context import MeetingContext


def _engine():
    # current_initial_prompt uses only meeting_context, name_registry, and env.
    return MeetingEngine(llm=None, emit=None)


def _clear(monkeypatch):
    for k in (
        "WHISPER_VOCABULARY", "WHISPER_PROMPT_INCLUDE_ATTENDEES",
        "WHISPER_PROMPT_INCLUDE_SPEAKER_NAMES", "WHISPER_PROMPT_MAX_TOKENS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_no_config_means_no_prompt(monkeypatch):
    _clear(monkeypatch)
    e = _engine()
    assert e.current_initial_prompt() is None


def test_vocabulary_only_by_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WHISPER_VOCABULARY", "Acme, OKR")
    e = _engine()
    # Real meeting + attendees present, but the attendee toggle is OFF by default.
    e.meeting_context = MeetingContext(title="Roadmap", attendees=["Priya"])
    assert e.current_initial_prompt() == "Acme, OKR"


def test_attendees_included_when_opted_in(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WHISPER_VOCABULARY", "Acme")
    monkeypatch.setenv("WHISPER_PROMPT_INCLUDE_ATTENDEES", "true")
    e = _engine()
    e.meeting_context = MeetingContext(title="Roadmap", attendees=["Priya", "Sam"])
    assert e.current_initial_prompt() == "Acme, Priya, Sam"


def test_placeholder_meeting_contributes_no_attendees(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WHISPER_VOCABULARY", "Acme")
    monkeypatch.setenv("WHISPER_PROMPT_INCLUDE_ATTENDEES", "true")
    e = _engine()  # default MeetingContext = "Unknown Meeting", no attendees
    assert e.current_initial_prompt() == "Acme"


def test_confirmed_speaker_names_opt_in(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WHISPER_PROMPT_INCLUDE_SPEAKER_NAMES", "true")
    e = _engine()
    e.name_registry.assign("Speaker 1", "Dana")
    e.name_registry.assign("Speaker 2", "Dana")  # dedup
    assert e.current_initial_prompt() == "Dana"
