"""Assemble Whisper `initial_prompt` text from curated vocabulary.

Biasing Whisper with a short list of expected terms (names, acronyms, brand
spellings, project codenames) fixes the words it systematically mangles. This
is *static, curated* text only — it never contains Whisper's own output, so it
carries none of the repetition/hallucination-loop risk of
`condition_on_previous_text`.

Three layers feed in (assembled by the engine, strongest-last so the most
important terms survive truncation — Whisper keeps the *tail* of the prompt):
  1. standing user vocabulary (WHISPER_VOCABULARY) — the backbone, works even
     with zero meeting info;
  2. meeting attendees (opt-in) when a real MeetingContext exists;
  3. user-confirmed speaker names (opt-in) accumulated during the meeting.

Empty in → None out, which makes the feature a true no-op (Whisper runs exactly
as it does today when no prompt is passed).
"""

from __future__ import annotations

import re

# Rough token estimate: Whisper's BPE averages ~4 chars/token for English. We
# only need a soft guard well under Whisper's ~224-token prompt window, so a
# char-based heuristic is plenty.
_CHARS_PER_TOKEN = 4
_SPLIT_RE = re.compile(r"[,\n;]+")


def _as_terms(value) -> list[str]:
    """Normalise a comma/newline string or a list into a clean term list."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = _SPLIT_RE.split(value)
    else:
        parts = list(value)
    return [t.strip() for t in parts if t and str(t).strip()]


def build_initial_prompt(vocabulary, names=None, max_tokens: int = 150) -> str | None:
    """Build a Whisper `initial_prompt` from a standing vocabulary plus optional
    names. Returns a comma-joined term string, or None if there's nothing to add.

    - `vocabulary`: comma/newline string or list of standing terms.
    - `names`: list of attendee / confirmed-speaker names (already filtered by
      the caller's opt-in toggles). Placed *after* vocabulary so names sit at the
      prompt tail and are the last to be dropped on truncation.
    - `max_tokens`: soft cap; terms are dropped from the front (least important)
      until the estimate fits.
    """
    ordered = _as_terms(vocabulary) + _as_terms(names)

    # Case-insensitive dedup, preserving first-seen spelling and order.
    seen: set[str] = set()
    terms: list[str] = []
    for term in ordered:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)

    if not terms:
        return None

    # Trim from the front (keep the tail) until under the soft token budget.
    char_budget = max(1, max_tokens) * _CHARS_PER_TOKEN
    while len(terms) > 1 and len(", ".join(terms)) > char_budget:
        terms.pop(0)

    return ", ".join(terms)
