"""Tests for the forbidden_facts gate in the windtunnel evaluator.

Negation-aware has_any_forbidden + NEGATION_CUES.

Coverage map:
  1. Scenario.forbidden_facts field exists, defaults to empty list
  2. evaluate_outcome passes when target_facts met + no forbidden asserted
  3. evaluate_outcome fails when a forbidden fact is asserted in the answer
  4. Negation-aware: forbidden fact in a negated/disclaiming clause does NOT trip gate
  5. Word-boundary numeric matching on forbidden facts ("3" ≠ "B003CCC")
  6. Empty forbidden_facts leaves existing eval behavior unchanged
  7. has_any_forbidden is importable as a standalone pure function
  8. NEGATION_CUES is importable from evaluators (for other dims to reuse)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from windtunnel.api.evaluators import NEGATION_CUES, evaluate_outcome, has_any_forbidden
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, compute_hash

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts(s: str = "2026-06-04T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=[],
        latency_ms=50.0,
    )


def _make_trace(turns: list[Turn]) -> Trace:
    return Trace(
        scenario_id="s06",
        agent_id="agent-test",
        variant_id="baseline",
        model="test-model",
        quant="q4",
        sampler={},
        started_at=_ts(),
        finished_at=_ts("2026-06-04T12:00:30+00:00"),
        turns=turns,
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )


def _simple_trace(final_answer: str) -> Trace:
    """Minimal trace: user turn + assistant answer."""
    return _make_trace([
        _turn(role="user", content="find the bug"),
        _turn(role="assistant", content=final_answer),
    ])


# ─── 1. Scenario.forbidden_facts field ────────────────────────────────────────

class TestForbiddenFactsField:
    def test_scenario_forbidden_facts_default_empty(self):
        """forbidden_facts defaults to an empty list."""
        sc = Scenario(name="s", prompt="p", target_facts=[])
        assert sc.forbidden_facts == []

    def test_scenario_forbidden_facts_accepts_list(self):
        """forbidden_facts can be set to a list of strings."""
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            forbidden_facts=["hallucinated_bug", "phantom_function"],
        )
        assert sc.forbidden_facts == ["hallucinated_bug", "phantom_function"]

    def test_scenario_forbidden_facts_single_item(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            forbidden_facts=["off_by_one_error"],
        )
        assert len(sc.forbidden_facts) == 1
        assert sc.forbidden_facts[0] == "off_by_one_error"


# ─── 2. Pass when target_facts met + no forbidden asserted ────────────────────

class TestForbiddenGatePassCases:
    def test_target_facts_met_no_forbidden_passes(self):
        """When all target facts present and no forbidden facts asserted → pass."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug causes the crash.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_target_facts_met_forbidden_absent_passes(self):
        """Forbidden fact not present at all → pass."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["actual_issue"]],
            forbidden_facts=["nonexistent_function"],
        )
        trace = _simple_trace("The actual_issue is in the parser.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_empty_forbidden_facts_unchanged_behavior(self):
        """Empty forbidden_facts: existing behavior preserved — pass when facts met."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["correct_answer"]],
            forbidden_facts=[],
        )
        trace = _simple_trace("The correct_answer is here.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_empty_forbidden_facts_unchanged_fail(self):
        """Empty forbidden_facts: fail path unchanged when facts missing."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["correct_answer"]],
            forbidden_facts=[],
        )
        trace = _simple_trace("I don't know.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False


# ─── 3. Fail when a forbidden fact is asserted ────────────────────────────────

class TestForbiddenGateFailCases:
    def test_forbidden_fact_asserted_fails(self):
        """A forbidden fact asserted in the answer → fail even if target facts present."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug and phantom_bug both cause crashes.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_forbidden_fact_asserted_without_target_fails(self):
        """Forbidden fact asserted + target facts missing → fail."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The phantom_bug causes crashes.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_forbidden_fact_asserted_detail_mentions_forbidden(self):
        """LayerResult detail explains the forbidden-fact failure."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug and phantom_bug both cause crashes.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False
        # Detail should mention forbidden or false_claim
        assert "forbidden" in result.detail.lower() or "phantom_bug" in result.detail.lower()

    def test_multiple_forbidden_any_asserted_fails(self):
        """Any one asserted forbidden fact trips the gate."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_fix"]],
            forbidden_facts=["wrong_func", "another_phantom"],
        )
        # Only another_phantom is asserted
        trace = _simple_trace("The real_fix works; another_phantom is unrelated.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_forbidden_case_insensitive(self):
        """Forbidden matching is case-insensitive."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["PhantomFunc"],
        )
        trace = _simple_trace("The real_bug is in phantomfunc.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False


# ─── 4. Negation-aware: negated forbidden terms pass ──────────────────────────

class TestForbiddenNegationAware:
    def test_forbidden_in_negated_context_passes(self):
        """'phantom_bug is NOT the issue' — negated, does not trip the gate."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug is the culprit. phantom_bug is not the issue.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_forbidden_with_no_prefix_passes(self):
        """'no phantom_bug' — 'no ' cue negates, does not trip."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug triggered it; no phantom_bug detected.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_forbidden_with_contraction_negation_passes(self):
        """'phantom_bug wasn't the cause' — n't cue negates."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug is present. phantom_bug wasn't the cause.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_forbidden_with_invalid_prefix_passes(self):
        """'invalid phantom_bug' — 'invalid' cue negates."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug is real. The invalid phantom_bug hypothesis is rejected.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_forbidden_asserted_separate_sentence_fails(self):
        """Forbidden fact asserted in a fresh sentence (no nearby negation) → fails.

        This guards against 'negation in a LATER sentence' spuriously excusing
        a prior bare assertion. The window is clipped at sentence boundaries.
        """
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        # "phantom_bug caused it." — bare assertion, no negation in its clause.
        # "It is not related to other bugs." — the negation is in a different sentence.
        trace = _simple_trace(
            "The real_bug exists. phantom_bug caused it. It is not related to other bugs."
        )
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_forbidden_with_without_prefix_passes(self):
        """'without phantom_bug' — 'without' cue negates."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The real_bug is present; without phantom_bug, the fix is safe.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True


# ─── 5. Word-boundary numeric matching on forbidden facts ─────────────────────

class TestForbiddenNumericBoundary:
    def test_bare_number_forbidden_not_matched_in_token(self):
        """Forbidden '3' does NOT match 'B003CCC' (boundary-aware)."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["3"],
        )
        # '3' only appears inside B003CCC — not a standalone word
        trace = _simple_trace("The real_bug is in module B003CCC.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True  # '3' not asserted as standalone

    def test_bare_number_forbidden_matched_standalone(self):
        """Forbidden '3' DOES match when '3' appears as a standalone word."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["3"],
        )
        trace = _simple_trace("The real_bug is on line 3 of the file.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_bare_number_forbidden_not_matched_in_batch_token(self):
        """Forbidden '3' does NOT match 'BATCH-2026' or 'order-3001'."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["3"],
        )
        trace = _simple_trace("The real_bug is in BATCH-2026 reference order-3001.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True


# ─── 6. has_any_forbidden as standalone pure function ────────────────────────

class TestHasAnyForbiddenStandalone:
    def test_importable(self):
        """has_any_forbidden must be importable from windtunnel.api.evaluators."""
        from windtunnel.api.evaluators import has_any_forbidden
        assert callable(has_any_forbidden)

    def test_returns_true_when_asserted(self):
        assert has_any_forbidden("The phantom_bug is the root cause.", ["phantom_bug"]) is True

    def test_returns_false_when_absent(self):
        assert has_any_forbidden("The real_bug is the issue.", ["phantom_bug"]) is False

    def test_returns_false_when_negated(self):
        assert has_any_forbidden("phantom_bug is not the cause.", ["phantom_bug"]) is False

    def test_returns_false_empty_forbidden(self):
        assert has_any_forbidden("anything goes here.", []) is False

    def test_returns_true_multiple_one_asserted(self):
        assert has_any_forbidden("The abc_func fails.", ["abc_func", "xyz_func"]) is True

    def test_returns_false_multiple_all_negated(self):
        result = has_any_forbidden(
            "no abc_func issue; xyz_func is not present.", ["abc_func", "xyz_func"]
        )
        assert result is False

    def test_boundary_number_asserted(self):
        """Standalone '3' is a forbidden fact and appears standalone → True."""
        assert has_any_forbidden("The answer is 3.", ["3"]) is True

    def test_boundary_number_in_token_not_asserted(self):
        """'3' inside 'B003CCC' is NOT a standalone assertion → False."""
        assert has_any_forbidden("Module B003CCC.", ["3"]) is False

    def test_case_insensitive_match(self):
        """Matching is case-insensitive."""
        assert has_any_forbidden("The PhantomBug is real.", ["phantombug"]) is True


# ─── 7. NEGATION_CUES importable ──────────────────────────────────────────────

class TestNegationCuesExported:
    def test_negation_cues_importable(self):
        """NEGATION_CUES must be importable from windtunnel.api.evaluators."""
        from windtunnel.api.evaluators import NEGATION_CUES
        assert isinstance(NEGATION_CUES, tuple)

    def test_negation_cues_contains_core_entries(self):
        """NEGATION_CUES must include the core cue set."""
        assert "not " in NEGATION_CUES
        assert "no " in NEGATION_CUES
        assert "n't" in NEGATION_CUES
        assert "without" in NEGATION_CUES
        assert "invalid" in NEGATION_CUES
        assert "unknown" in NEGATION_CUES

    def test_negation_cues_is_non_empty(self):
        assert len(NEGATION_CUES) > 0


# ─── 8. Interaction with existing evaluator features ─────────────────────────

class TestForbiddenInteractionWithExistingFeatures:
    def test_forbidden_gate_independent_of_trajectory(self):
        """forbidden_facts is an outcome gate — trajectory layer is separate."""
        from windtunnel.api.evaluators import evaluate_trajectory
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
            must_call=["read_file"],
        )
        # Answer has real_bug (target met) + phantom_bug (forbidden asserted)
        trace = _simple_trace("The real_bug and phantom_bug both matter.")
        outcome = evaluate_outcome(trace, sc)
        trajectory = evaluate_trajectory(trace, sc)
        # Outcome fails due to forbidden; trajectory fails due to no tool call
        assert outcome.passed is False
        assert trajectory.passed is False  # no tool calls

    def test_forbidden_gate_does_not_affect_empty_target_facts(self):
        """With empty target_facts, forbidden gate still applies."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[],
            forbidden_facts=["phantom_bug"],
        )
        trace = _simple_trace("The phantom_bug is the issue.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_forbidden_gate_empty_target_no_forbidden_passes(self):
        """Empty target_facts + empty forbidden_facts → trivially passes."""
        sc = Scenario(name="s", prompt="p", target_facts=[], forbidden_facts=[])
        trace = _simple_trace("anything")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_last_turn_semantics_preserved_with_forbidden(self):
        """Forbidden gate still uses last-turn semantics: only last assistant turn scored."""
        from windtunnel.api.trace import Turn
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["real_bug"]],
            forbidden_facts=["phantom_bug"],
        )
        # Intermediate turn has target facts but also forbidden; last turn is clean
        turns = [
            Turn(role="user", content="find bug", tool_calls=[], tool_results=[], latency_ms=0),
            Turn(  # intermediate — has forbidden
                role="assistant",
                content="The real_bug and phantom_bug are both present.",
                tool_calls=[], tool_results=[], latency_ms=50,
            ),
            Turn(  # LAST — no forbidden, target fact present
                role="assistant",
                content="The real_bug is the culprit.",
                tool_calls=[], tool_results=[], latency_ms=50,
            ),
        ]
        trace = _make_trace(turns)
        result = evaluate_outcome(trace, sc)
        # Last turn: real_bug present, no phantom_bug → should pass
        assert result.passed is True


# ─── Word-boundary matching for single-identifier forbidden facts ─────────────

class TestForbiddenIdentifierWordBoundary:
    """Short identifier tokens (add, subtract, multiply) must NOT match
    inside longer words like 'additional', 'subtracted', 'multiplying'.

    Without word-boundary matching, 'add' matches 'additional', causing the
    forbidden_facts gate to reject a correct answer that mentions 'additionally'.
    NON_BUGGY_SYMBOLS = [add, subtract, multiply, compute_stats, DataPoint]
    all qualify as single identifiers and must get word-boundary treatment.
    """

    def test_add_does_not_match_additional(self):
        """Forbidden 'add' must NOT fire when only 'additional' appears."""
        assert has_any_forbidden(
            "Additionally, the divide function uses integer division.",
            ["add"],
        ) is False

    def test_add_does_not_match_added(self):
        """Forbidden 'add' must NOT fire inside 'added'."""
        assert has_any_forbidden(
            "We added a new feature to the codebase.",
            ["add"],
        ) is False

    def test_add_matches_standalone(self):
        """Forbidden 'add' DOES fire when 'add' appears as a standalone identifier."""
        assert has_any_forbidden(
            "The bug is in the add function.",
            ["add"],
        ) is True

    def test_subtract_does_not_match_subtracted(self):
        """Forbidden 'subtract' must NOT fire inside 'subtracted'."""
        assert has_any_forbidden(
            "The value was subtracted from the total.",
            ["subtract"],
        ) is False

    def test_subtract_matches_standalone(self):
        """Forbidden 'subtract' DOES fire when standalone."""
        assert has_any_forbidden(
            "The bug is in the subtract function.",
            ["subtract"],
        ) is True

    def test_multiply_does_not_match_multiplying(self):
        """Forbidden 'multiply' must NOT fire inside 'multiplying'."""
        assert has_any_forbidden(
            "The divide bug keeps multiplying its effect.",
            ["multiply"],
        ) is False

    def test_multiply_matches_standalone(self):
        """Forbidden 'multiply' DOES fire when standalone."""
        assert has_any_forbidden(
            "The problem is in multiply.",
            ["multiply"],
        ) is True

    def test_compute_stats_does_not_match_partial(self):
        """Forbidden 'compute_stats' is an identifier — must not match mid-word."""
        # compute_stats is long and won't normally appear mid-word, but
        # word-boundary should still apply correctly (it does, via \b).
        assert has_any_forbidden(
            "The divide function uses integer division.",
            ["compute_stats"],
        ) is False

    def test_non_buggy_symbols_no_false_fire_on_correct_answer(self):
        """A correct answer mentioning 'additionally' and 'multiplying' must
        not be rejected by a forbidden-facts gate built from identifier-style
        symbols."""
        # Identifier-style forbidden facts as a codebase-comprehension scenario
        # would declare them (the words a wrong answer would name).
        NON_BUGGY_SYMBOLS = ["add", "subtract", "multiply", "compute_stats", "DataPoint"]

        # A correct answer disclaims add/subtract/multiply — but because
        # those words appear bare (as targets of correction, not as bug claims),
        # negation context determines pass/fail. The key assertion here: the
        # word 'additionally' and 'multiplying' do NOT fire the gate.
        # (Note: "add, subtract, and multiply functions are all correct" is NOT
        # negated in the standard NEGATION_CUES sense — this sub-test just
        # verifies the word-boundary fix for 'additionally' and 'multiplying'.)
        assert has_any_forbidden("Additionally, the divide bug multiplying errors.", NON_BUGGY_SYMBOLS) is False

    def test_multi_word_phrase_still_uses_substring(self):
        """Multi-word phrases ('Bluewing Logistics') still use substring matching,
        not word-boundary regex (confirmed: spaces in fact → not a single identifier)."""
        # 'Bluewing Logistics' as a forbidden fact should match via substring
        assert has_any_forbidden("The Bluewing Logistics did it.", ["Bluewing Logistics"]) is True

    def test_identifier_with_underscore_uses_word_boundary(self):
        """Identifiers containing underscores (DataPoint_extra) are single
        identifiers and get word-boundary treatment."""
        # 'DataPoint' should not match 'DataPoint_extra'
        assert has_any_forbidden("DataPoint_extra is correct.", ["DataPoint"]) is False

    def test_identifier_matches_when_isolated(self):
        """Word-boundary: 'DataPoint' as an isolated token IS matched."""
        assert has_any_forbidden("The bug is in DataPoint class.", ["DataPoint"]) is True
