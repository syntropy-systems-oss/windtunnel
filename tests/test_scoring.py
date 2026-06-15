"""Tests for the four-layer scoring framework.

TDD red phase — these tests define the contract. They must fail before
any implementation exists.

Coverage map:
  1. Score / LayerResult / FailureCost data types
  2. Scenario authoring (target_facts, target_numbers, trajectory, constraint,
     robustness, failure_cost, requires_tool_use, variance_allowed)
  3. evaluate_outcome  — AND-of-OR facts, typed numeric matching, last-turn
     semantics, requires_tool_use gate
  4. evaluate_trajectory — must_call, forbidden, order_matters
  5. evaluate_constraint — predicate composition
  6. evaluate_robustness — perturbation-applied check
  7. Perturbation library — corrupt_prior_assistant_turn, inject_stale_memory,
     tool_timeout, tool_returns_malformed
  8. aggregate — per-run-must-pass, pass_rate ± stddev
  9. State-reset hook — interface + sqlite fixture
 10. Prototype scenario round-trip — typo_recovery verdict matches the
     recorded reference run
"""
from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from windtunnel.api import tool_name_matches
from windtunnel.api.aggregate import (
    AggregateResult,
    ScenarioRunResult,
    aggregate_runs,
)
from windtunnel.api.evaluators import (
    evaluate_constraint,
    evaluate_outcome,
    evaluate_robustness,
    evaluate_trajectory,
)
from windtunnel.api.perturbations import (
    CorruptPriorAssistantTurn,
    InjectStaleMemory,
    ToolReturnsMalformed,
    ToolTimeout,
)
from windtunnel.api.scenario import (
    NumberFact,
    Perturbation,
    Policy,
    Scenario,
)

# ─── Import targets (will fail until implemented) ─────────────────────────────
from windtunnel.api.score import (
    FailureCost,
    LayerResult,
    Score,
    Verdict,
)
from windtunnel.api.state_reset import StateResetConfig, reset_state_db
from windtunnel.api.trace import Trace, Turn, compute_hash

# ─── Trace / Turn helpers ─────────────────────────────────────────────────────

UTC = UTC


def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    latency_ms: float = 50.0,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        latency_ms=latency_ms,
    )


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": str(args or {})},
    }


def _make_trace(turns: list[Turn], tags: list[str] | None = None) -> Trace:
    return Trace(
        scenario_id="s01",
        agent_id="agent-test",
        variant_id="baseline",
        model="test-model",
        quant="q4",
        sampler={},
        started_at=_ts(),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=turns,
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )


def _simple_trace(final_answer: str, tool_names: list[str] | None = None) -> Trace:
    """Build a minimal trace: user turn + optional tool turns + assistant answer."""
    turns: list[Turn] = [_turn(role="user", content="test question")]
    for name in (tool_names or []):
        turns.append(_turn(role="assistant", content="", tool_calls=[_tool_call(name)]))
        turns.append(_turn(role="tool", content='{"result": "ok"}'))
    turns.append(_turn(role="assistant", content=final_answer))
    return _make_trace(turns)


class TestOutcomeFn:
    """outcome_fn: a custom outcome evaluator fully owns the outcome layer and can
    score from any trace evidence (e.g. an artifact a StateProbe froze into
    trace.observations) instead of the model's last-turn text."""

    def test_outcome_fn_passes_ignoring_target_facts(self):
        # target_facts would FAIL (the number is absent from the prose), but
        # outcome_fn grades the observation and PASSES.
        trace = _simple_trace("done — see the attached file", tool_names=["run"])
        trace.observations = {"artifact": {"value": 42}}
        scn = Scenario(
            name="s", prompt="p",
            target_facts=[["42"]],  # not in the prose → would fail the default path
            requires_tool_use=True,
            outcome_fn=lambda t: LayerResult(
                passed=(t.observations.get("artifact", {}).get("value") == 42),
                detail="artifact value matched",
            ),
        )
        res = evaluate_outcome(trace, scn)
        assert res.passed and "artifact value matched" in res.detail

    def test_outcome_fn_fails_on_bad_artifact(self):
        trace = _simple_trace("all good!", tool_names=["run"])
        trace.observations = {"artifact": {"value": 7}}
        scn = Scenario(
            name="s", prompt="p", target_facts=[], requires_tool_use=True,
            outcome_fn=lambda t: LayerResult(
                passed=(t.observations.get("artifact", {}).get("value") == 42),
                detail="artifact mismatch",
            ),
        )
        assert not evaluate_outcome(trace, scn).passed

    def test_outcome_fn_exception_is_a_failure(self):
        trace = _simple_trace("x", tool_names=["run"])
        scn = Scenario(
            name="s", prompt="p", target_facts=[], requires_tool_use=True,
            outcome_fn=lambda t: 1 / 0,
        )
        res = evaluate_outcome(trace, scn)
        assert not res.passed and "outcome_fn error" in res.detail

    def test_structural_gates_apply_before_outcome_fn(self):
        # requires_tool_use must fail FIRST — outcome_fn never runs without tools.
        trace = _simple_trace("answer", tool_names=[])  # no tool calls
        called = {"n": 0}

        def _fn(_t):
            called["n"] += 1
            return LayerResult(passed=True, detail="should not run")

        scn = Scenario(
            name="s", prompt="p", target_facts=[], requires_tool_use=True, outcome_fn=_fn,
        )
        res = evaluate_outcome(trace, scn)
        assert not res.passed and "no_tools_used" in res.detail
        assert called["n"] == 0


# ─── 1. Score / LayerResult / FailureCost types ───────────────────────────────

class TestScoreTypes:
    def test_verdict_enum_values(self):
        assert Verdict.PASS is not None
        assert Verdict.FAIL is not None
        assert Verdict.SKIP is not None

    def test_layer_result_pass(self):
        r = LayerResult(passed=True, detail="all facts found")
        assert r.passed is True
        assert r.detail == "all facts found"

    def test_layer_result_fail(self):
        r = LayerResult(passed=False, detail="missing: ['foo']")
        assert r.passed is False

    def test_score_has_four_layers(self):
        s = Score(
            outcome=LayerResult(passed=True, detail="ok"),
            trajectory=LayerResult(passed=False, detail="missing tool"),
            constraint=LayerResult(passed=True, detail="ok"),
            robustness=LayerResult(passed=True, detail="ok"),
            failure_cost=FailureCost(),
        )
        assert s.outcome.passed is True
        assert s.trajectory.passed is False
        assert s.constraint.passed is True
        assert s.robustness.passed is True

    def test_score_outcome_pass_trajectory_fail_distinct(self):
        """The key criterion: pass outcome + fail trajectory is representable."""
        s = Score(
            outcome=LayerResult(passed=True, detail="facts found"),
            trajectory=LayerResult(passed=False, detail="wrong tool order"),
            constraint=LayerResult(passed=True, detail="ok"),
            robustness=LayerResult(passed=True, detail="ok"),
            failure_cost=FailureCost(),
        )
        assert s.outcome.passed is True
        assert s.trajectory.passed is False
        # They are independent — not aggregated to a single bool
        assert s.outcome.passed != s.trajectory.passed

    def test_failure_cost_defaults(self):
        fc = FailureCost()
        assert fc.severity == "low"
        assert fc.customer_visible is False
        assert fc.reversible is True
        assert fc.side_effect_performed is False

    def test_failure_cost_custom(self):
        fc = FailureCost(
            severity="critical",
            customer_visible=True,
            reversible=False,
            side_effect_performed=True,
        )
        assert fc.severity == "critical"
        assert fc.customer_visible is True
        assert fc.reversible is False
        assert fc.side_effect_performed is True

    def test_failure_cost_severity_values(self):
        for s in ("low", "medium", "high", "critical"):
            fc = FailureCost(severity=s)  # type: ignore[arg-type]
            assert fc.severity == s

    def test_score_attaches_failure_cost(self):
        fc = FailureCost(severity="high", customer_visible=True, reversible=False)
        s = Score(
            outcome=LayerResult(passed=True, detail="ok"),
            trajectory=LayerResult(passed=True, detail="ok"),
            constraint=LayerResult(passed=True, detail="ok"),
            robustness=LayerResult(passed=True, detail="ok"),
            failure_cost=fc,
        )
        assert s.failure_cost.severity == "high"
        assert s.failure_cost.customer_visible is True


# ─── 2. Scenario type ─────────────────────────────────────────────────────────

class TestScenarioType:
    def test_minimal_scenario(self):
        sc = Scenario(
            name="test_scenario",
            prompt="What is the email?",
            target_facts=[["joe@example.com"]],
        )
        assert sc.name == "test_scenario"
        assert sc.prompt == "What is the email?"
        assert sc.target_facts == [["joe@example.com"]]

    def test_scenario_defaults(self):
        sc = Scenario(name="s", prompt="p", target_facts=[])
        assert sc.target_numbers == []
        assert sc.must_call == []
        assert sc.forbidden_calls == []
        assert sc.order_matters is False
        assert sc.policies == []
        assert sc.perturbations == []
        assert sc.requires_tool_use is False
        assert sc.variance_allowed is False
        assert sc.failure_cost.severity == "low"
        assert sc.failure_cost.customer_visible is False
        assert sc.failure_cost.reversible is True

    def test_scenario_with_trajectory(self):
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            must_call=["tool_a", "tool_b"],
            forbidden_calls=["tool_bad"],
            order_matters=True,
        )
        assert sc.must_call == ["tool_a", "tool_b"]
        assert sc.forbidden_calls == ["tool_bad"]
        assert sc.order_matters is True

    def test_scenario_with_failure_cost(self):
        fc = FailureCost(severity="critical", customer_visible=True, reversible=False)
        sc = Scenario(name="s", prompt="p", target_facts=[], failure_cost=fc)
        assert sc.failure_cost.severity == "critical"

    def test_scenario_requires_tool_use_flag(self):
        sc = Scenario(name="s", prompt="p", target_facts=[], requires_tool_use=True)
        assert sc.requires_tool_use is True

    def test_scenario_variance_allowed_flag(self):
        sc = Scenario(name="s", prompt="p", target_facts=[], variance_allowed=True)
        assert sc.variance_allowed is True

    def test_number_fact_value_and_unit(self):
        nf = NumberFact(value=3, unit="orders")
        assert nf.value == 3
        assert nf.unit == "orders"

    def test_number_fact_unit_optional(self):
        nf = NumberFact(value=42)
        assert nf.value == 42
        assert nf.unit is None

    def test_policy_is_callable(self):
        """A Policy wraps a predicate over a Trace."""
        called: list[bool] = []

        def my_predicate(trace: Trace) -> bool:
            called.append(True)
            return True

        p = Policy(name="my_policy", predicate=my_predicate)
        assert p.name == "my_policy"
        trace = _simple_trace("answer")
        result = p.predicate(trace)
        assert result is True
        assert called == [True]

    def test_perturbation_is_dataclass(self):
        """Perturbation is a base-type / protocol; CorruptPriorAssistantTurn satisfies it."""
        p = CorruptPriorAssistantTurn(idx=0, mode="empty")
        assert isinstance(p, Perturbation)


# ─── 3. evaluate_outcome ──────────────────────────────────────────────────────

class TestEvaluateOutcome:
    def test_simple_fact_found(self):
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
        )
        trace = _simple_trace("The email is joe@example.com.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_simple_fact_not_found(self):
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
        )
        trace = _simple_trace("I don't know the email.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_and_of_or_all_groups_required(self):
        """Both outer groups must match for pass."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[
                ["ACC-BLWG-001", "Bluewing Logistics"],
                ["B001AAA", "B002BBB"],
            ],
        )
        # Only first group matches
        trace = _simple_trace("The client is ACC-BLWG-001.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_and_of_or_or_within_group(self):
        """Within a group, any one member satisfies it."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[
                ["ACC-BLWG-001", "Bluewing Logistics"],
                ["B001AAA", "B002BBB"],
            ],
        )
        trace = _simple_trace("Client: Bluewing Logistics. SKU: B002BBB. Qty: 8.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_empty_target_facts_passes(self):
        """No facts required → trivially passes outcome."""
        sc = Scenario(name="s", prompt="p", target_facts=[])
        trace = _simple_trace("anything")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    # ── Last-assistant-turn semantics (carry-forward) ─────────────────────────

    def test_last_turn_empty_content_is_not_found(self):
        """If the actual last turn is empty (stop-after-tool-call), score FAIL."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
        )
        # Conversation ends with an assistant turn that has empty content
        # (i.e. a tool-call turn that never produced natural language)
        turns = [
            _turn(role="user", content="What is the email?"),
            _turn(role="assistant", content="The email is joe@example.com."),  # prior
            _turn(role="assistant", content="", tool_calls=[_tool_call("some_tool")]),  # LAST
        ]
        trace = _make_trace(turns)
        result = evaluate_outcome(trace, sc)
        # The LAST turn is empty — must NOT retroactively use the prior turn
        assert result.passed is False

    def test_last_non_empty_prior_turn_not_used(self):
        """Scoring must not pull from intermediate turns — only actual last."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["SECRET_DATA"]],
        )
        turns = [
            _turn(role="user", content="question"),
            _turn(role="assistant", content="SECRET_DATA is here."),  # intermediate
            _turn(role="assistant", content="Never mind.", tool_calls=[]),  # last
        ]
        trace = _make_trace(turns)
        result = evaluate_outcome(trace, sc)
        # "SECRET_DATA" is only in intermediate turn, last turn says "Never mind."
        # → must be FAIL if we score only the last assistant turn
        assert result.passed is False

    def test_last_assistant_turn_correct(self):
        """When last assistant turn contains the fact, it passes."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["correct_answer"]],
        )
        turns = [
            _turn(role="user", content="question"),
            _turn(role="assistant", content="wrong_info"),
            _turn(role="assistant", content="correct_answer is the result"),
        ]
        trace = _make_trace(turns)
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_no_assistant_turns_not_found(self):
        """Trace with no assistant turns at all → FAIL."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["anything"]],
        )
        turns = [_turn(role="user", content="question")]
        trace = _make_trace(turns)
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    # ── requires_tool_use gate (carry-forward) ────────────────────────────────

    def test_requires_tool_use_fails_when_no_tools(self):
        """requires_tool_use=True + no tool calls → FAIL even if facts present."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
            requires_tool_use=True,
        )
        # Facts are present but no tools were called
        trace = _simple_trace("The email is joe@example.com.")  # no tool_names
        result = evaluate_outcome(trace, sc)
        assert result.passed is False
        assert "no_tools_used" in result.detail.lower() or "tool" in result.detail.lower()

    def test_requires_tool_use_passes_when_tools_called(self):
        """requires_tool_use=True + tools called + facts present → PASS."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
            requires_tool_use=True,
        )
        trace = _simple_trace("The email is joe@example.com.", tool_names=["client_lookup"])
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_requires_tool_use_false_allows_no_tools(self):
        """Default requires_tool_use=False should pass even without tool calls."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["joe@example.com"]],
            requires_tool_use=False,
        )
        trace = _simple_trace("The email is joe@example.com.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    # ── Numeric matching (carry-forward) ──────────────────────────────────────

    def test_numeric_fact_exact_match(self):
        """NumberFact(3) matches '3 orders' in the answer."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            target_numbers=[NumberFact(value=3, unit="orders")],
        )
        trace = _simple_trace("There are 3 orders in storage.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_numeric_fact_no_false_positive_substring(self):
        """NumberFact(3) must NOT match 'B003CCC' (substring '3')."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            target_numbers=[NumberFact(value=3)],
        )
        # '3' appears only embedded in B003CCC, not as a standalone number
        trace = _simple_trace("The SKU is B003CCC.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_numeric_fact_no_false_positive_batch(self):
        """NumberFact(3) must NOT match 'BATCH-2026' or 'order-3001'."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            target_numbers=[NumberFact(value=3)],
        )
        trace = _simple_trace("Reference: BATCH-2026, order-3001.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_numeric_fact_word_boundary_standalone(self):
        """NumberFact(3) matches '3' when preceded/followed by non-digit."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            target_numbers=[NumberFact(value=3)],
        )
        trace = _simple_trace("Quantity: 3.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True

    def test_numeric_fact_combined_with_target_facts(self):
        """Both target_facts AND target_numbers must be satisfied."""
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["ACC-BLWG-001"]],
            target_numbers=[NumberFact(value=12)],
        )
        # Has the client ID but not the number
        trace = _simple_trace("Client ACC-BLWG-001 has items.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False

    def test_numeric_and_facts_both_present(self):
        sc = Scenario(
            name="s",
            prompt="p",
            target_facts=[["ACC-BLWG-001"]],
            target_numbers=[NumberFact(value=12)],
        )
        trace = _simple_trace("Client ACC-BLWG-001 has 12 orders.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is True


# ─── 4. evaluate_trajectory ───────────────────────────────────────────────────

class TestEvaluateTrajectory:
    def test_must_call_all_present(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["tool_a", "tool_b"],
        )
        trace = _simple_trace("answer", tool_names=["tool_a", "tool_b"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_must_call_missing_one(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["tool_a", "tool_b"],
        )
        trace = _simple_trace("answer", tool_names=["tool_a"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is False
        assert "tool_b" in result.detail

    def test_forbidden_call_absent_passes(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            forbidden_calls=["bad_tool"],
        )
        trace = _simple_trace("answer", tool_names=["good_tool"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_forbidden_call_present_fails(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            forbidden_calls=["bad_tool"],
        )
        trace = _simple_trace("answer", tool_names=["bad_tool"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is False
        assert "bad_tool" in result.detail

    def test_order_matters_correct_order_passes(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["client_lookup", "order_query"],
            order_matters=True,
        )
        trace = _simple_trace("answer", tool_names=["client_lookup", "order_query"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_order_matters_wrong_order_fails(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["client_lookup", "order_query"],
            order_matters=True,
        )
        trace = _simple_trace("answer", tool_names=["order_query", "client_lookup"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is False

    def test_order_not_matters_wrong_order_passes(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["client_lookup", "order_query"],
            order_matters=False,
        )
        trace = _simple_trace("answer", tool_names=["order_query", "client_lookup"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_no_trajectory_constraints_passes(self):
        sc = Scenario(name="s", prompt="p", target_facts=[])
        trace = _simple_trace("answer")
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_trajectory_does_not_affect_outcome(self):
        """Trajectory layer result is independent of outcome layer."""
        sc = Scenario(
            name="s", prompt="p",
            target_facts=[["the_answer"]],
            must_call=["required_tool"],
        )
        # outcome passes (fact present), trajectory fails (tool not called)
        trace = _simple_trace("the_answer is here")
        outcome_result = evaluate_outcome(trace, sc)
        traj_result = evaluate_trajectory(trace, sc)
        assert outcome_result.passed is True
        assert traj_result.passed is False


# ─── 4b. tool_name_matches — canonical-vs-decorated matcher ─────────────────

class TestToolNameMatcher:
    """Contract: scenarios declare CANONICAL bare tool names; platforms may
    decorate with prefix chains. Match = exact OR suffix at a word boundary
    ("_" or ".")."""

    def test_exact_match(self):
        assert tool_name_matches("client_lookup", "client_lookup") is True

    def test_underscore_prefix_chain(self):
        # Acme decoration: server prefix + platform name sanitizer
        assert tool_name_matches(
            "client_lookup", "mcp_acme_ops_client_lookup"
        ) is True

    def test_dotted_prefix(self):
        assert tool_name_matches("client_lookup", "ops.client_lookup") is True

    def test_suffix_of_another_bare_name_matches_by_design(self):
        # DOCUMENTED LIMITATION: a canonical that is itself a suffix of
        # another tool's bare name matches both ("lookup" hits client_lookup
        # via the "_lookup" boundary). Authors must use FULL bare names.
        assert tool_name_matches("lookup", "client_lookup") is True
        assert tool_name_matches("lookup", "mcp_acme_ops_order_lookup") is True

    def test_non_boundary_suffix_does_not_match(self):
        # "ookup" is a suffix but NOT at a word boundary
        assert tool_name_matches("ookup", "client_lookup") is False

    def test_canonical_longer_than_full_does_not_match(self):
        assert tool_name_matches("mcp_acme_ops_client_lookup", "client_lookup") is False

    def test_unrelated_name_does_not_match(self):
        assert tool_name_matches("client_lookup", "order_query") is False

    def test_trajectory_canonical_must_call_matches_decorated_trace(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["client_lookup", ["patch", "put_file"]],
            order_matters=True,
        )
        trace = _simple_trace(
            "answer",
            tool_names=["mcp_acme_ops_client_lookup", "mcp_acme_github_put_file"],
        )
        result = evaluate_trajectory(trace, sc)
        assert result.passed is True

    def test_trajectory_canonical_forbidden_matches_decorated_trace(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            forbidden_calls=["email_send"],
        )
        trace = _simple_trace("answer", tool_names=["mcp_acme_ops_email_send"])
        result = evaluate_trajectory(trace, sc)
        assert result.passed is False
        assert "mcp_acme_ops_email_send" in result.detail

    def test_trajectory_order_check_uses_matcher(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            must_call=["create_branch", "open_pr"],
            order_matters=True,
        )
        # Decorated names, wrong order → order violation still detected
        trace = _simple_trace(
            "answer",
            tool_names=["mcp_acme_github_open_pr", "mcp_acme_github_create_branch"],
        )
        result = evaluate_trajectory(trace, sc)
        assert result.passed is False
        assert "order" in result.detail


# ─── 5. evaluate_constraint ───────────────────────────────────────────────────

class TestEvaluateConstraint:
    def test_no_policies_passes(self):
        sc = Scenario(name="s", prompt="p", target_facts=[])
        trace = _simple_trace("answer")
        result = evaluate_constraint(trace, sc)
        assert result.passed is True

    def test_single_policy_passes(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            policies=[Policy(name="must_use_tool", predicate=lambda t: len(t.turns) > 1)],
        )
        trace = _simple_trace("answer", tool_names=["some_tool"])
        result = evaluate_constraint(trace, sc)
        assert result.passed is True

    def test_single_policy_fails(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            policies=[
                Policy(
                    name="must_not_use_bad_tool",
                    predicate=lambda t: not any(
                        tc.get("function", {}).get("name") == "bad_tool"
                        for turn in t.turns
                        for tc in turn.tool_calls
                    ),
                )
            ],
        )
        trace = _simple_trace("answer", tool_names=["bad_tool"])
        result = evaluate_constraint(trace, sc)
        assert result.passed is False
        assert "must_not_use_bad_tool" in result.detail

    def test_multiple_policies_all_pass(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            policies=[
                Policy(name="p1", predicate=lambda t: True),
                Policy(name="p2", predicate=lambda t: True),
            ],
        )
        trace = _simple_trace("answer")
        result = evaluate_constraint(trace, sc)
        assert result.passed is True

    def test_multiple_policies_one_fails(self):
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            policies=[
                Policy(name="p1", predicate=lambda t: True),
                Policy(name="p2_fails", predicate=lambda t: False),
            ],
        )
        trace = _simple_trace("answer")
        result = evaluate_constraint(trace, sc)
        assert result.passed is False
        assert "p2_fails" in result.detail

    def test_effect_policy_hook_present(self):
        """Policy supports an optional effect_class field (forward compat)."""
        p = Policy(
            name="no_external_send",
            predicate=lambda t: True,
            effect_class="external_send",
        )
        assert p.effect_class == "external_send"


# ─── 6. evaluate_robustness ───────────────────────────────────────────────────

class TestEvaluateRobustness:
    def test_no_perturbations_passes(self):
        sc = Scenario(name="s", prompt="p", target_facts=[])
        trace = _simple_trace("answer")
        result = evaluate_robustness(trace, sc)
        assert result.passed is True

    def test_perturbation_marked_applied_passes(self):
        """If perturbations are declared and trace has the applied marker → PASS."""
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            perturbations=[CorruptPriorAssistantTurn(idx=0, mode="empty")],
        )
        # A trace that was run under perturbation has worker_warnings containing the marker
        trace = _make_trace([
            _turn(role="user", content="q"),
            _turn(role="assistant", content="answer"),
        ])
        # Add the marker that the runner would add when it applied perturbation
        object.__setattr__(
            trace, "worker_warnings",
            ["perturbation_applied: corrupt_prior_assistant_turn idx=0 mode=empty"],
        )
        result = evaluate_robustness(trace, sc)
        assert result.passed is True

    def test_perturbation_declared_but_not_applied_fails(self):
        """Perturbation declared but trace has no applied marker → FAIL."""
        sc = Scenario(
            name="s", prompt="p", target_facts=[],
            perturbations=[CorruptPriorAssistantTurn(idx=0, mode="empty")],
        )
        trace = _simple_trace("answer")  # no perturbation marker
        result = evaluate_robustness(trace, sc)
        assert result.passed is False


# ─── 7. Perturbation library ──────────────────────────────────────────────────

class TestPerturbationLibrary:
    def test_corrupt_prior_assistant_turn_empty_mode(self):
        """mode='empty' clears content of the target turn."""
        turns = [
            _turn(role="user", content="question"),
            _turn(role="assistant", content="intermediate answer"),
            _turn(role="assistant", content="final answer"),
        ]
        trace = _make_trace(turns)
        p = CorruptPriorAssistantTurn(idx=1, mode="empty")
        mutated = p.apply(trace)
        # Turn at idx 1 should have empty content
        assert mutated.turns[1].content == ""
        # Other turns unchanged
        assert mutated.turns[0].content == "question"
        assert mutated.turns[2].content == "final answer"

    def test_corrupt_prior_assistant_turn_literal_tool_text(self):
        """mode='literal_tool_text' sets content to literal tool template text."""
        turns = [
            _turn(role="user", content="question"),
            _turn(role="assistant", content="normal answer"),
        ]
        trace = _make_trace(turns)
        p = CorruptPriorAssistantTurn(idx=1, mode="literal_tool_text")
        mutated = p.apply(trace)
        # content should be the template artifact string
        assert "<tool_call>" in mutated.turns[1].content or "tool_call" in mutated.turns[1].content

    def test_corrupt_prior_assistant_turn_does_not_mutate_original(self):
        """apply() returns a NEW trace, original is unchanged."""
        turns = [
            _turn(role="user", content="q"),
            _turn(role="assistant", content="original"),
        ]
        trace = _make_trace(turns)
        p = CorruptPriorAssistantTurn(idx=1, mode="empty")
        mutated = p.apply(trace)
        assert trace.turns[1].content == "original"
        assert mutated.turns[1].content == ""
        assert mutated is not trace

    def test_corrupt_adds_worker_warning_marker(self):
        """apply() adds a perturbation_applied marker to worker_warnings."""
        turns = [
            _turn(role="user", content="q"),
            _turn(role="assistant", content="answer"),
        ]
        trace = _make_trace(turns)
        p = CorruptPriorAssistantTurn(idx=1, mode="empty")
        mutated = p.apply(trace)
        assert any("perturbation_applied" in w for w in mutated.worker_warnings)

    def test_inject_stale_memory_is_perturbation(self):
        p = InjectStaleMemory(key="user_pref", value="metric units")
        assert isinstance(p, Perturbation)
        assert p.key == "user_pref"
        assert p.value == "metric units"

    def test_inject_stale_memory_apply_adds_warning(self):
        """InjectStaleMemory.apply() records the injection in worker_warnings."""
        trace = _simple_trace("answer")
        p = InjectStaleMemory(key="user_pref", value="imperial units")
        mutated = p.apply(trace)
        assert any("perturbation_applied" in w for w in mutated.worker_warnings)
        assert any("inject_stale_memory" in w for w in mutated.worker_warnings)

    def test_tool_timeout_is_perturbation(self):
        p = ToolTimeout(probability=0.5, delay_ms=200)
        assert isinstance(p, Perturbation)
        assert p.probability == 0.5
        assert p.delay_ms == 200

    def test_tool_timeout_apply_records_config(self):
        """ToolTimeout.apply() records the knob config in worker_warnings."""
        trace = _simple_trace("answer")
        p = ToolTimeout(probability=0.5, delay_ms=200)
        mutated = p.apply(trace)
        assert any("tool_timeout" in w for w in mutated.worker_warnings)

    def test_tool_returns_malformed_is_perturbation(self):
        p = ToolReturnsMalformed(probability=0.3)
        assert isinstance(p, Perturbation)
        assert p.probability == 0.3

    def test_tool_returns_malformed_apply_records_config(self):
        trace = _simple_trace("answer")
        p = ToolReturnsMalformed(probability=0.3)
        mutated = p.apply(trace)
        assert any("tool_returns_malformed" in w for w in mutated.worker_warnings)


# ─── 8. Aggregate ─────────────────────────────────────────────────────────────

class TestAggregate:
    def _score(self, outcome_pass: bool, fc: FailureCost | None = None) -> Score:
        return Score(
            outcome=LayerResult(passed=outcome_pass, detail=""),
            trajectory=LayerResult(passed=True, detail=""),
            constraint=LayerResult(passed=True, detail=""),
            robustness=LayerResult(passed=True, detail=""),
            failure_cost=fc or FailureCost(),
        )

    def test_all_passes_is_found(self):
        runs = [
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
        ]
        result = aggregate_runs(runs, variance_allowed=False)
        assert result.verdict == "PASS"
        assert result.passed == 2
        assert result.total == 2
        assert result.pass_rate == 1.0

    def test_all_fails_is_not_found(self):
        runs = [
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
        ]
        result = aggregate_runs(runs, variance_allowed=False)
        assert result.verdict == "FAIL"
        assert result.passed == 0
        assert result.pass_rate == 0.0

    def test_partial_pass_not_variance_is_not_found(self):
        """4/5 passes → FAIL when variance_allowed=False (per-run-must-pass)."""
        runs = [
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
        ]
        result = aggregate_runs(runs, variance_allowed=False)
        assert result.verdict == "FAIL"
        assert result.passed == 4
        assert result.total == 5

    def test_partial_pass_variance_allowed_reports(self):
        """4/5 passes → still reported with variance; verdict reflects partial pass."""
        runs = [
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
        ]
        result = aggregate_runs(runs, variance_allowed=True)
        # partial pass with variance allowed → PARTIAL or similar non-regression verdict
        assert result.passed == 4
        assert result.total == 5
        assert abs(result.pass_rate - 0.8) < 1e-6
        # stddev must be present and non-negative
        assert result.stddev >= 0.0
        # verdict should indicate it's variance-allowed, not a hard FAIL
        assert result.verdict in ("PASS", "PARTIAL", "PASS_WITH_VARIANCE")

    def test_pass_rate_and_stddev_single_run(self):
        runs = [ScenarioRunResult(score=self._score(True), trace=_simple_trace("a"))]
        result = aggregate_runs(runs, variance_allowed=False)
        assert result.pass_rate == 1.0
        assert result.stddev == 0.0

    def test_stddev_computed_correctly(self):
        """5 runs: 3 pass, 2 fail → mean=0.6, stddev of Bernoulli population."""
        runs = [
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(True), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
            ScenarioRunResult(score=self._score(False), trace=_simple_trace("a")),
        ]
        result = aggregate_runs(runs, variance_allowed=True)
        assert abs(result.pass_rate - 0.6) < 1e-6
        # population stddev of Bernoulli with p=0.6, n=5 samples
        # values = [1, 1, 1, 0, 0]; mean=0.6; variance = mean of (xi - mean)^2
        # = (3*(0.4^2) + 2*(0.6^2)) / 5 = (3*0.16 + 2*0.36)/5 = (0.48+0.72)/5 = 0.24
        # stddev = sqrt(0.24) ≈ 0.4899
        expected_stddev = math.sqrt(0.24)
        assert abs(result.stddev - expected_stddev) < 1e-4

    def test_aggregate_result_fields(self):
        runs = [ScenarioRunResult(score=self._score(True), trace=_simple_trace("a"))]
        result = aggregate_runs(runs, variance_allowed=False)
        assert isinstance(result, AggregateResult)
        assert result.verdict in ("PASS", "FAIL", "PARTIAL", "PASS_WITH_VARIANCE")
        assert isinstance(result.passed, int)
        assert isinstance(result.total, int)
        assert isinstance(result.pass_rate, float)
        assert isinstance(result.stddev, float)

    def test_per_layer_pass_rates(self):
        """AggregateResult exposes per-layer pass rates."""
        run1 = ScenarioRunResult(
            score=Score(
                outcome=LayerResult(passed=True, detail=""),
                trajectory=LayerResult(passed=False, detail=""),
                constraint=LayerResult(passed=True, detail=""),
                robustness=LayerResult(passed=True, detail=""),
                failure_cost=FailureCost(),
            ),
            trace=_simple_trace("a"),
        )
        run2 = ScenarioRunResult(
            score=Score(
                outcome=LayerResult(passed=True, detail=""),
                trajectory=LayerResult(passed=True, detail=""),
                constraint=LayerResult(passed=True, detail=""),
                robustness=LayerResult(passed=True, detail=""),
                failure_cost=FailureCost(),
            ),
            trace=_simple_trace("a"),
        )
        result = aggregate_runs([run1, run2], variance_allowed=False)
        assert result.outcome_pass_rate == 1.0
        assert result.trajectory_pass_rate == 0.5
        assert result.constraint_pass_rate == 1.0
        assert result.robustness_pass_rate == 1.0


# ─── 9. State-reset hook ──────────────────────────────────────────────────────

class TestStateResetHook:
    def _create_state_db(self, path: Path) -> None:
        """Create a minimal state.db with the tables that need wiping."""
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                data TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                content TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, content=messages, content_rowid=id)
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
            USING fts5(content, content=messages, content_rowid=id,
                       tokenize="trigram")
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        # Seed with data that should be wiped
        conn.execute("INSERT INTO sessions VALUES ('s001', '{}')")
        conn.execute("INSERT INTO messages (session_id, content) VALUES ('s001', 'prior context')")
        conn.execute("INSERT INTO state_meta VALUES ('user_pref', 'dark mode')")
        conn.execute("INSERT INTO state_meta VALUES ('db_initialized', '1')")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')")
        conn.commit()
        conn.close()

    def test_reset_clears_messages(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_reset_clears_sessions(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_reset_clears_state_meta_except_db_initialized(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT key FROM state_meta").fetchall()
        conn.close()
        keys = [r[0] for r in rows]
        # db_initialized should remain
        assert "db_initialized" in keys
        # user_pref should be wiped
        assert "user_pref" not in keys

    def test_reset_preserves_schema_version(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        conn.close()
        assert rows == 1  # schema_version must NOT be wiped

    def test_reset_fts_indexes_empty_after_reset(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        # After rebuild on empty messages, FTS should return no rows
        rows = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_keep_state_skips_wipe(self, tmp_path: Path):
        """keep_state=True must not clear messages."""
        db_path = tmp_path / "state.db"
        self._create_state_db(db_path)
        cfg = StateResetConfig(state_db_path=db_path, keep_state=True)
        reset_state_db(cfg)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert rows == 1  # untouched

    def test_reset_config_defaults(self):
        cfg = StateResetConfig(state_db_path=Path("/tmp/fake.db"))
        assert cfg.keep_state is False


# ─── 10. Prototype scenario round-trip ────────────────────────────────────────

class TestPrototypeRoundTrip:
    """Round-trip the migrated prototype scenarios through the new framework.

    The typo_recovery scenario should produce PASS when the recorded
    reference answer is used as the trace answer.
    """

    def _build_typo_recovery_trace(self) -> Trace:
        """Reconstruct a minimal Trace that matches the reference run for typo_recovery."""
        # Reference run: 3 tool calls, final answer contains required facts
        final_answer = (
            "Here are the SKUs and quantities currently in the back-office ops suite for "
            "**Bluewing Logistics** (ACC-BLWG-001):\n\n"
            "| SKU | Item Name | Quantity | Stage |\n"
            "|------|-----------|----------|-------|\n"
            "| B001AAA | Bluewing Jersey - Home | 12 | Intake |\n"
            "| B002BBB | Bluewing Cap - Adjustable | 8 | Intake |\n\n"
            "**Total:** 20 orders (all in Intake stage)."
        )
        turns: list[Turn] = [
            _turn(
                role="user",
                content="Please list the SKUs and quantities currently in the back-office ops suite "
                        "for the client 'ACC-1322 – Bluewing Logistics'.",
            ),
            _turn(
                role="assistant", content="",
                tool_calls=[_tool_call("mcp_acme_ops_client_lookup")],
            ),
            _turn(role="tool", content='{"clients": [{"id": "ACC-BLWG-001"}]}'),
            _turn(
                role="assistant", content="",
                tool_calls=[_tool_call("mcp_acme_ops_order_report")],
            ),
            _turn(role="tool", content='{"items": []}'),
            _turn(
                role="assistant", content="",
                tool_calls=[_tool_call("mcp_acme_ops_order_query")],
            ),
            _turn(role="tool", content='{"orders": [{"sku": "B001AAA", "qty": 12}]}'),
            _turn(role="assistant", content=final_answer),
        ]
        return _make_trace(turns)

    def test_typo_recovery_round_trip_outcome_found(self):
        """typo_recovery with the reference answer must score outcome PASS."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        sc = next(s for s in PROTOTYPE_SCENARIOS if s.name == "typo_recovery")
        trace = self._build_typo_recovery_trace()
        result = evaluate_outcome(trace, sc)
        assert result.passed is True, (
            f"Expected typo_recovery to PASS outcome, but: {result.detail}"
        )

    def test_prototype_scenarios_loaded(self):
        """Prototype scenarios module must export 11 scenarios."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        assert len(PROTOTYPE_SCENARIOS) == 11

    def test_prototype_scenarios_have_correct_names(self):
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        names = {s.name for s in PROTOTYPE_SCENARIOS}
        expected = {
            "typo_recovery", "cross_stage_search", "multi_client_aggregate",
            "disambiguation", "wrong_tool_seduction", "negative_query",
            "cross_reference", "specific_field_lookup", "comparison_which_has_more",
            "order_by_id_trace", "multi_step_followup",
        }
        assert names == expected

    def test_prototype_scenarios_have_default_failure_cost(self):
        """Migrated scenarios default to low/internal/reversible failure_cost."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        for sc in PROTOTYPE_SCENARIOS:
            assert sc.failure_cost.severity == "low"
            assert sc.failure_cost.customer_visible is False
            assert sc.failure_cost.reversible is True

    def test_prototype_scenarios_have_empty_trajectory_constraint_robustness(self):
        """Migrated scenarios start with empty trajectory/constraint/robustness."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        for sc in PROTOTYPE_SCENARIOS:
            assert sc.must_call == []
            assert sc.forbidden_calls == []
            assert sc.policies == []
            assert sc.perturbations == []

    def test_specific_field_lookup_requires_tool_use(self):
        """specific_field_lookup must be tagged requires_tool_use=True."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        sc = next(s for s in PROTOTYPE_SCENARIOS if s.name == "specific_field_lookup")
        assert sc.requires_tool_use is True

    def test_specific_field_lookup_not_found_without_tools(self):
        """specific_field_lookup: even if email text is present, no tools = FAIL."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        sc = next(s for s in PROTOTYPE_SCENARIOS if s.name == "specific_field_lookup")
        # Model guessed the email from training, no tools used
        trace = _simple_trace("The email is ops@bluewing.example.")
        result = evaluate_outcome(trace, sc)
        assert result.passed is False
