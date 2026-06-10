"""The BEFORE window of has_any_forbidden must be clipped at
sentence/clause boundaries — a negation in a PRIOR sentence must not excuse a
forbidden fact asserted in the current one."""
from __future__ import annotations

from windtunnel.api.evaluators import has_any_forbidden


def test_prior_sentence_negation_does_not_excuse():
    # "multiply" is ASSERTED as the bug; the `not` belongs to the prior sentence.
    assert has_any_forbidden("add is not the bug. multiply is the bug", ["multiply"]) is True


def test_prior_clause_negation_does_not_excuse():
    assert has_any_forbidden("it is not add; multiply is wrong", ["multiply"]) is True


def test_same_clause_negation_still_excuses():
    # A negation in the SAME clause as the forbidden token still counts as negated.
    assert has_any_forbidden("multiply is not the bug", ["multiply"]) is False
    assert has_any_forbidden("the bug is not multiply", ["multiply"]) is False


def test_plain_assertion_is_forbidden():
    assert has_any_forbidden("the bug is in multiply", ["multiply"]) is True
