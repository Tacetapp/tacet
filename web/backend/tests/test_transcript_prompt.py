"""Tests for Whisper initial_prompt assembly (transcript_prompt.build_initial_prompt)."""

from web.backend.transcript_prompt import build_initial_prompt


def test_empty_inputs_return_none():
    assert build_initial_prompt("") is None
    assert build_initial_prompt(None) is None
    assert build_initial_prompt("   ,  ,\n") is None
    assert build_initial_prompt([], names=[]) is None


def test_string_vocabulary_is_split_and_joined():
    assert build_initial_prompt("Kubernetes, OKR, Acme") == "Kubernetes, OKR, Acme"
    # newlines and semicolons also split
    assert build_initial_prompt("a\nb; c") == "a, b, c"


def test_list_vocabulary_supported():
    assert build_initial_prompt(["Foo", "Bar"]) == "Foo, Bar"


def test_case_insensitive_dedup_preserves_first_spelling_and_order():
    assert build_initial_prompt("Acme, acme, ACME, Beta") == "Acme, Beta"


def test_names_appended_after_vocabulary():
    out = build_initial_prompt("Kubernetes", names=["Priya", "Sam"])
    assert out == "Kubernetes, Priya, Sam"


def test_names_dedup_against_vocabulary():
    out = build_initial_prompt("Priya, OKR", names=["priya", "Sam"])
    assert out == "Priya, OKR, Sam"


def test_truncation_keeps_the_tail():
    # Names are at the tail and must survive when over budget. With a tiny cap,
    # the earliest (vocabulary) terms are dropped first.
    terms = [f"term{i}" for i in range(50)]
    out = build_initial_prompt(terms, names=["KeepMeLast"], max_tokens=3)
    assert out is not None
    assert out.endswith("KeepMeLast")
    # Front terms dropped under the tight budget.
    assert "term0" not in out


def test_single_long_term_not_dropped_to_empty():
    # Even over budget, a single term is preserved (never trim below one).
    out = build_initial_prompt(["a" * 500], max_tokens=1)
    assert out == "a" * 500
