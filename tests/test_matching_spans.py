"""Span-returning matcher variants must agree with the boolean matchers.

The design law behind the run viewer's evidence highlighting: for any input,
a span variant finds at least one span IFF its boolean counterpart returns
True. The viewer must never disagree with the scorer.

Covers:
  1. Property-style equivalence over a corpus of (text, fact/group/number)
     cases — the tricky cases the boolean matchers' own suites pin
     (word boundaries, B003CCC-style embeddings, unit windows, negation
     cues, clause clipping) plus combinatorial coverage.
  2. Span truthfulness: every returned span slices to the matched token.
  3. has_any_forbidden delegates to the span scanner (single algorithm,
     drift impossible by construction).
"""
from __future__ import annotations

import itertools

from windtunnel.api._matching import (
    find_fact_spans,
    find_forbidden_assertion_spans,
    match_fact_group,
    match_fact_group_spans,
    match_number_fact,
    match_number_fact_span,
)
from windtunnel.api.evaluators import NEGATION_CUES, has_any_forbidden
from windtunnel.api.scenario import NumberFact

# ─── corpus ──────────────────────────────────────────────────────────────────
# Texts in the style of the built-in scenarios (fictional ops/client data).

_TEXTS = [
    "",
    "ok",
    "The email on file is ops@bluewing.example.",
    "The email on file is OPS@BLUEWING.EXAMPLE, confirmed twice.",
    "No client matched that name.",
    "Here are all orders at the Intake stage for ACC-BLWG-001.",
    "Module B003CCC failed in BATCH-2026 reference order-3001.",
    "The answer is 3.",
    "There are 12 units in queue Q-INTAKE-1.",
    "Quantity: 12. The unit field was left blank far away from here though.",
    "phantom_bug is not the cause.",
    "The phantom_bug is the root cause.",
    "add is not the bug. multiply is the bug",
    "it is not add; multiply is wrong",
    "Additionally, the divide bug multiplying errors.",
    "The Bluewing Logistics did it.",
    "DataPoint_extra is correct.",
    "The bug is in DataPoint class.",
    "no such order exists, and 7 was never a valid quantity",
]

_FACTS = [
    "ok",
    "ops@bluewing.example",
    "Bluewing Logistics",
    "Intake",
    "3",
    "add",
    "multiply",
    "phantom_bug",
    "DataPoint",
    "no client",
    "missing entirely",
    "",
]

_NUMBER_FACTS = [
    NumberFact(value=3),
    NumberFact(value=12),
    NumberFact(value=12, unit="units"),
    NumberFact(value=12, unit="parcels"),
    NumberFact(value=7),
    NumberFact(value=2026),
    NumberFact(value=3001),
]


class TestFactSpanEquivalence:
    def test_span_found_iff_membership(self) -> None:
        """find_fact_spans is non-empty exactly when the boolean test is True."""
        for text, fact in itertools.product(_TEXTS, _FACTS):
            spans = find_fact_spans(text, fact)
            expected = bool(fact) and fact.lower() in text.lower()
            assert bool(spans) == expected, (text, fact)

    def test_group_spans_iff_match_fact_group(self) -> None:
        groups = [
            ["ok"],
            ["ops@bluewing.example", "Bluewing Logistics"],
            ["missing entirely"],
            ["missing entirely", "Intake"],
            [],
        ]
        for text, group in itertools.product(_TEXTS, groups):
            spans = match_fact_group_spans(text, group)
            assert bool(spans) == match_fact_group(text, group), (text, group)

    def test_spans_slice_to_the_matched_fact(self) -> None:
        for text, fact in itertools.product(_TEXTS, _FACTS):
            for span in find_fact_spans(text, fact):
                assert text[span.start:span.end].lower() == fact.lower(), (text, fact)

    def test_all_occurrences_are_returned(self) -> None:
        spans = find_fact_spans("ok, ok and OK again", "ok")
        assert len(spans) == 3


class TestNumberFactSpanEquivalence:
    def test_span_found_iff_boolean_match(self) -> None:
        """Same regex, same first-occurrence unit window as the boolean matcher."""
        for text, fact in itertools.product(_TEXTS, _NUMBER_FACTS):
            span = match_number_fact_span(text, fact)
            assert (span is not None) == match_number_fact(text, fact), (text, fact)

    def test_span_slices_to_the_number(self) -> None:
        span = match_number_fact_span("There are 12 units in queue Q-INTAKE-1.", NumberFact(12, "units"))
        assert span is not None
        assert "There are 12 units in queue Q-INTAKE-1."[span.start:span.end] == "12"

    def test_first_occurrence_window_semantics_are_preserved(self) -> None:
        """The boolean matcher only checks the FIRST occurrence's unit window;
        the span variant must not 'rescue' via a later occurrence."""
        text = "Order 12 was flagged. Later we counted 12 units exactly."
        fact = NumberFact(value=12, unit="units")
        assert match_number_fact(text, fact) is False
        assert match_number_fact_span(text, fact) is None

    def test_embedded_digits_do_not_match(self) -> None:
        assert match_number_fact_span("Module B003CCC.", NumberFact(3)) is None


class TestForbiddenSpanEquivalence:
    _FORBIDDEN_LISTS = [
        ["3"],
        ["add"],
        ["multiply"],
        ["add", "multiply"],
        ["phantom_bug"],
        ["Bluewing Logistics"],
        ["DataPoint"],
        ["no client"],
        [],
    ]

    def test_assertions_found_iff_has_any_forbidden(self) -> None:
        for text, forbidden in itertools.product(_TEXTS, self._FORBIDDEN_LISTS):
            spans = find_forbidden_assertion_spans(text, forbidden, NEGATION_CUES)
            assert bool(spans) == has_any_forbidden(text, forbidden), (text, forbidden)

    def test_spans_slice_to_the_forbidden_token(self) -> None:
        for text, forbidden in itertools.product(_TEXTS, self._FORBIDDEN_LISTS):
            for fact, span in find_forbidden_assertion_spans(text, forbidden, NEGATION_CUES):
                assert text.lower()[span.start:span.end] == fact.lower(), (text, fact)

    def test_negated_occurrence_is_skipped_but_asserted_one_is_spanned(self) -> None:
        """One text, two occurrences: only the asserted one gets a span."""
        text = "multiply is not the bug. the bug is in multiply"
        spans = find_forbidden_assertion_spans(text, ["multiply"], NEGATION_CUES)
        assert len(spans) == 1
        _fact, span = spans[0]
        assert span.start == text.rindex("multiply")

    def test_boolean_gate_is_the_span_scanner(self) -> None:
        """has_any_forbidden delegates to find_forbidden_assertion_spans —
        one algorithm serves the verdict and the evidence, so they cannot
        drift. (Behavioral pinning lives in test_forbidden_facts_gate.py /
        test_forbidden_before_clip.py, which now exercise the shared scan.)"""
        import inspect

        from windtunnel.api import evaluators

        source = inspect.getsource(evaluators.has_any_forbidden)
        assert "_find_forbidden_assertion_spans" in source


class TestCombinatorialSweep:
    """Deterministic pseudo-random sweep: fragments recombine into texts and
    every (text, fact) pair must keep span/boolean agreement."""

    _FRAGMENTS = [
        "the client is Bluewing Logistics",
        "no such client",
        "order-3001 shipped",
        "3 units remain",
        "that is not multiply",
        "B003CCC",
        "ops@bluewing.example",
        "",
    ]

    def test_fact_and_forbidden_agreement_over_fragment_products(self) -> None:
        facts = ["3", "multiply", "Bluewing Logistics", "ops@bluewing.example", "client"]
        for combo in itertools.product(self._FRAGMENTS, repeat=2):
            for joiner in (". ", "; ", " "):
                text = joiner.join(combo)
                for fact in facts:
                    assert bool(find_fact_spans(text, fact)) == (fact.lower() in text.lower())
                    spans = find_forbidden_assertion_spans(text, [fact], NEGATION_CUES)
                    assert bool(spans) == has_any_forbidden(text, [fact]), (text, fact)
                number = NumberFact(value=3, unit="units")
                assert (match_number_fact_span(text, number) is not None) == match_number_fact(
                    text, number
                ), text
