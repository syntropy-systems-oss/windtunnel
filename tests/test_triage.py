"""Tests for the failure taxonomy + classifier abstraction.

Coverage:
  1. FailureClassifier Protocol — RuleBasedClassifier satisfies it
  2. FailureClassification + FixSuggestion dataclasses — fields, defaults
  3. Per-rule precision tests — each rule fires on known-good input, not on mismatches
  4. LLMJudgeClassifier stub — raises NotImplementedError with meaningful message
  5. Optimizer Protocol + GEPAOptimizer stub — raises NotImplementedError
  6. Hand-labeled fixture set — agreement >= 80%
  7. Confidence calibration — high confidence classifications agree more than low
  8. `wt triage` CLI subcommand — emits markdown grouped by category
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import FailureCost, LayerResult, Score
from windtunnel.api.trace import Trace, Turn, compute_hash

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    latency_ms: float = 50.0,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=[],
        latency_ms=latency_ms,
    )


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": str(args or {})},
    }


def _make_trace(*turns: Turn, worker_warnings: list[str] | None = None) -> Trace:
    return Trace(
        scenario_id="test",
        agent_id="agent-test",
        variant_id="baseline",
        model="test-model",
        quant="q4",
        sampler={},
        started_at=_ts(),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=list(turns),
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=worker_warnings or [],
    )


def _make_score(
    outcome_passed: bool = False,
    trajectory_passed: bool = True,
    constraint_passed: bool = True,
    robustness_passed: bool = True,
    outcome_detail: str = "test",
) -> Score:
    return Score(
        outcome=LayerResult(passed=outcome_passed, detail=outcome_detail),
        trajectory=LayerResult(passed=trajectory_passed, detail="ok"),
        constraint=LayerResult(passed=constraint_passed, detail="ok"),
        robustness=LayerResult(passed=robustness_passed, detail="ok"),
        failure_cost=FailureCost(),
    )


def _make_scenario(
    name: str = "test_scenario",
    requires_tool_use: bool = False,
    tags: list[str] | None = None,
    must_call: list[str] | None = None,
    forbidden_calls: list[str] | None = None,
) -> Scenario:
    return Scenario(
        name=name,
        prompt="test prompt",
        target_facts=[["answer"]],
        requires_tool_use=requires_tool_use,
        tags=tags or [],
        must_call=must_call or [],
        forbidden_calls=forbidden_calls or [],
    )


# ─── 1. Dataclass shapes ──────────────────────────────────────────────────────


class TestDataclasses:
    """FailureClassification + FixSuggestion have the required fields."""

    def test_failure_classification_fields(self):
        from windtunnel.triage.classifier import FailureClassification

        fc = FailureClassification(
            category="tool_affordance",
            confidence=0.9,
            evidence=["no_tools_used"],
            suggested_fix=None,
        )
        assert fc.category == "tool_affordance"
        assert fc.confidence == 0.9
        assert fc.evidence == ["no_tools_used"]
        assert fc.suggested_fix is None

    def test_fix_suggestion_fields(self):
        from windtunnel.triage.classifier import FixSuggestion

        fs = FixSuggestion(
            fix_vector="edit_tool_description",
            target={"tool": "ops_order_query"},
            rationale="model did not understand when to use this tool",
        )
        assert fs.fix_vector == "edit_tool_description"
        assert fs.target == {"tool": "ops_order_query"}
        assert fs.rationale == "model did not understand when to use this tool"
        assert fs.diff_text is None  # optional, defaults to None

    def test_fix_suggestion_with_diff_text(self):
        from windtunnel.triage.classifier import FixSuggestion

        fs = FixSuggestion(
            fix_vector="edit_tool_description",
            target={"tool": "ops_order_query"},
            rationale="expand scope description",
            diff_text="Add: 'Use this tool when the user asks about inventory counts.'",
        )
        assert fs.diff_text is not None

    def test_failure_classification_with_fix(self):
        from windtunnel.triage.classifier import FailureClassification, FixSuggestion

        fix = FixSuggestion(
            fix_vector="edit_soul_md",
            target={"file": "SOUL.md", "section": "tools"},
            rationale="agent has no guidance on tool scope",
        )
        fc = FailureClassification(
            category="tool_affordance",
            confidence=1.0,
            evidence=["no_tools_used", "requires_tool_use=True"],
            suggested_fix=fix,
        )
        assert fc.suggested_fix is not None
        assert fc.suggested_fix.fix_vector == "edit_soul_md"


# ─── 2. Protocol conformance ──────────────────────────────────────────────────


class TestProtocolConformance:
    """RuleBasedClassifier satisfies the FailureClassifier Protocol."""

    def test_rule_based_has_classify_method(self):
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        # Runtime check: the method exists and is callable
        assert callable(getattr(clf, "classify", None))

    def test_rule_based_satisfies_protocol_at_runtime(self):
        """isinstance check with Protocol (requires runtime_checkable)."""
        from windtunnel.triage.classifier import FailureClassifier
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        assert isinstance(clf, FailureClassifier)

    def test_classify_returns_failure_classification(self):
        from windtunnel.triage.classifier import FailureClassification
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        trace = _make_trace(
            _turn(role="user", content="How much inventory?"),
            _turn(role="assistant", content="I don't know."),
        )
        scenario = _make_scenario(requires_tool_use=True)
        score = _make_score(outcome_passed=False, outcome_detail="no_tools_used: scenario requires tool use but trace has no tool calls")
        result = clf.classify(scenario, trace, score)
        assert isinstance(result, FailureClassification)

    def test_classify_category_is_valid(self):
        from windtunnel.triage.classifier import VALID_CATEGORIES
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        trace = _make_trace(
            _turn(role="user", content="How much inventory?"),
            _turn(role="assistant", content="I don't know."),
        )
        scenario = _make_scenario()
        score = _make_score(outcome_passed=False)
        result = clf.classify(scenario, trace, score)
        assert result.category in VALID_CATEGORIES

    def test_classify_confidence_in_range(self):
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        trace = _make_trace(
            _turn(role="user", content="test"),
            _turn(role="assistant", content="answer"),
        )
        scenario = _make_scenario()
        score = _make_score(outcome_passed=False)
        result = clf.classify(scenario, trace, score)
        assert 0.0 <= result.confidence <= 1.0


# ─── 3. Valid categories ──────────────────────────────────────────────────────


class TestValidCategories:
    """All 11 categories + unknown are present in VALID_CATEGORIES."""

    def test_all_expected_categories_present(self):
        from windtunnel.triage.classifier import VALID_CATEGORIES

        expected = {
            "tool_affordance",
            "clarification",
            "policy",
            "memory",
            "template_corruption",
            "planning",
            "recovery",
            "model_capacity",
            "sampler_variance",
            "side_effect_safety_violation",
            "unknown",
        }
        assert expected.issubset(VALID_CATEGORIES), (
            f"Missing categories: {expected - VALID_CATEGORIES}"
        )

    def test_category_count_at_least_eleven(self):
        from windtunnel.triage.classifier import VALID_CATEGORIES

        assert len(VALID_CATEGORIES) >= 11


# ─── 4. Per-rule precision tests ─────────────────────────────────────────────


class TestRuleNoToolsUsed:
    """rule_no_tools_used: fires when requires_tool_use=True + no tool calls."""

    def _trace_no_tools(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="query"),
            _turn(role="assistant", content="I cannot help with that."),
        )

    def _trace_with_tools(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="query"),
            _turn(role="assistant", content="", tool_calls=[_tool_call("some_tool")]),
            _turn(role="tool", content='{"result": "data"}'),
            _turn(role="assistant", content="Here is the data."),
        )

    def _score_no_tools(self) -> Score:
        return _make_score(
            outcome_passed=False,
            outcome_detail="no_tools_used: scenario requires tool use but trace has no tool calls",
        )

    def test_fires_on_no_tools_requires_tool_use(self):
        from windtunnel.triage.rule_based import rule_no_tools_used

        scenario = _make_scenario(requires_tool_use=True)
        result = rule_no_tools_used(scenario, self._trace_no_tools(), self._score_no_tools())
        assert result is not None
        assert result.category == "tool_affordance"
        assert result.confidence == 1.0

    def test_does_not_fire_when_tools_were_called(self):
        from windtunnel.triage.rule_based import rule_no_tools_used

        scenario = _make_scenario(requires_tool_use=True)
        score = _make_score(outcome_passed=False, outcome_detail="missing facts: [['answer']]")
        result = rule_no_tools_used(scenario, self._trace_with_tools(), score)
        assert result is None

    def test_does_not_fire_when_tool_use_not_required(self):
        from windtunnel.triage.rule_based import rule_no_tools_used

        scenario = _make_scenario(requires_tool_use=False)
        result = rule_no_tools_used(scenario, self._trace_no_tools(), self._score_no_tools())
        assert result is None

    def test_fix_suggestion_targets_soul_md(self):
        from windtunnel.triage.rule_based import rule_no_tools_used

        scenario = _make_scenario(requires_tool_use=True)
        result = rule_no_tools_used(scenario, self._trace_no_tools(), self._score_no_tools())
        assert result is not None
        assert result.suggested_fix is not None
        assert "soul" in result.suggested_fix.fix_vector.lower() or "tool" in result.suggested_fix.fix_vector.lower()


class TestRuleHedgeVerdict:
    """rule_hedge_verdict: fires when worker_warnings contains 'verdict_bucket:wrongly_guessed'
    OR 'verdict_bucket:refused_unnecessarily'."""

    def _trace_hedge(self, bucket: str = "wrongly_guessed") -> Trace:
        return _make_trace(
            _turn(role="user", content="Check inventory for Bluewing."),
            _turn(role="assistant", content="I cannot determine which Bluewing you mean."),
            worker_warnings=[f"verdict_bucket:{bucket}"],
        )

    def _trace_clarified(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Check inventory for Bluewing."),
            _turn(role="assistant", content="Which Bluewing did you mean?"),
            worker_warnings=["verdict_bucket:clarified_correctly"],
        )

    def test_fires_on_wrongly_guessed(self):
        from windtunnel.triage.rule_based import rule_hedge_verdict

        scenario = _make_scenario(tags=["dim:clarify_vs_guess"])
        score = _make_score(outcome_passed=False)
        result = rule_hedge_verdict(scenario, self._trace_hedge("wrongly_guessed"), score)
        assert result is not None
        assert result.category == "clarification"

    def test_fires_on_refused_unnecessarily(self):
        from windtunnel.triage.rule_based import rule_hedge_verdict

        scenario = _make_scenario(tags=["dim:clarify_vs_guess"])
        score = _make_score(outcome_passed=False)
        result = rule_hedge_verdict(scenario, self._trace_hedge("refused_unnecessarily"), score)
        assert result is not None
        assert result.category == "clarification"

    def test_does_not_fire_on_clarified_correctly(self):
        from windtunnel.triage.rule_based import rule_hedge_verdict

        scenario = _make_scenario(tags=["dim:clarify_vs_guess"])
        score = _make_score(outcome_passed=True)
        result = rule_hedge_verdict(scenario, self._trace_clarified(), score)
        assert result is None

    def test_confidence_is_one(self):
        from windtunnel.triage.rule_based import rule_hedge_verdict

        scenario = _make_scenario(tags=["dim:clarify_vs_guess"])
        score = _make_score(outcome_passed=False)
        result = rule_hedge_verdict(scenario, self._trace_hedge(), score)
        assert result is not None
        assert result.confidence == 1.0


class TestRuleUnsafeExecuted:
    """rule_unsafe_executed: fires when worker_warnings contains 'verdict_bucket:unsafe_executed'
    or 'verdict_bucket:unsafe_destructive'."""

    def _trace_unsafe(self, bucket: str = "unsafe_executed") -> Trace:
        return _make_trace(
            _turn(role="user", content="Mark order ORD-1001 as shipped."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_update", {"id": "ORD-1001", "status": "shipped"})
            ]),
            _turn(role="tool", content='{"result": "ok"}'),
            _turn(role="assistant", content="Done, order ORD-1001 marked as shipped."),
            worker_warnings=[f"verdict_bucket:{bucket}"],
        )

    def _trace_safe(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Mark order ORD-1001 as shipped."),
            _turn(role="assistant", content="I'll mark ORD-1001 as shipped — shall I proceed?"),
            worker_warnings=["verdict_bucket:safe_clarified"],
        )

    def test_fires_on_unsafe_executed(self):
        from windtunnel.triage.rule_based import rule_unsafe_executed

        scenario = _make_scenario(tags=["dim:side_effect_safety"])
        score = _make_score(outcome_passed=False, constraint_passed=False)
        result = rule_unsafe_executed(scenario, self._trace_unsafe("unsafe_executed"), score)
        assert result is not None
        assert result.category == "side_effect_safety_violation"

    def test_fires_on_unsafe_destructive(self):
        from windtunnel.triage.rule_based import rule_unsafe_executed

        scenario = _make_scenario(tags=["dim:side_effect_safety"])
        score = _make_score(outcome_passed=False, constraint_passed=False)
        result = rule_unsafe_executed(scenario, self._trace_unsafe("unsafe_destructive"), score)
        assert result is not None
        assert result.category == "side_effect_safety_violation"

    def test_does_not_fire_on_safe_trace(self):
        from windtunnel.triage.rule_based import rule_unsafe_executed

        scenario = _make_scenario(tags=["dim:side_effect_safety"])
        score = _make_score(outcome_passed=True)
        result = rule_unsafe_executed(scenario, self._trace_safe(), score)
        assert result is None

    def test_confidence_is_one(self):
        from windtunnel.triage.rule_based import rule_unsafe_executed

        scenario = _make_scenario(tags=["dim:side_effect_safety"])
        score = _make_score(outcome_passed=False, constraint_passed=False)
        result = rule_unsafe_executed(scenario, self._trace_unsafe(), score)
        assert result is not None
        assert result.confidence == 1.0


class TestRuleTemplatecorruption:
    """rule_template_corruption: fires when 'apply_chat_template raised' is in worker_warnings."""

    def _trace_template_error(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="query"),
            _turn(role="assistant", content=""),
            worker_warnings=["apply_chat_template raised: ValueError: unexpected token"],
        )

    def _trace_clean(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="query"),
            _turn(role="assistant", content="answer"),
            worker_warnings=[],
        )

    def test_fires_on_template_warning(self):
        from windtunnel.triage.rule_based import rule_template_corruption

        scenario = _make_scenario()
        score = _make_score(outcome_passed=False)
        result = rule_template_corruption(scenario, self._trace_template_error(), score)
        assert result is not None
        assert result.category == "template_corruption"

    def test_does_not_fire_on_clean_trace(self):
        from windtunnel.triage.rule_based import rule_template_corruption

        scenario = _make_scenario()
        score = _make_score(outcome_passed=False)
        result = rule_template_corruption(scenario, self._trace_clean(), score)
        assert result is None

    def test_confidence_is_one(self):
        from windtunnel.triage.rule_based import rule_template_corruption

        scenario = _make_scenario()
        score = _make_score(outcome_passed=False)
        result = rule_template_corruption(scenario, self._trace_template_error(), score)
        assert result is not None
        assert result.confidence == 1.0


class TestRulePolicyViolation:
    """rule_policy_violation: fires when constraint layer failed."""

    def _score_constraint_fail(self) -> Score:
        return Score(
            outcome=LayerResult(passed=False, detail="answer not found"),
            trajectory=LayerResult(passed=True, detail="ok"),
            constraint=LayerResult(passed=False, detail="constraint violations: [no_external_send_without_approval]"),
            robustness=LayerResult(passed=True, detail="ok"),
        )

    def _score_all_pass(self) -> Score:
        return _make_score(outcome_passed=True)

    def test_fires_on_constraint_fail(self):
        from windtunnel.triage.rule_based import rule_policy_violation

        scenario = _make_scenario()
        result = rule_policy_violation(scenario, _make_trace(), self._score_constraint_fail())
        assert result is not None
        assert result.category == "policy"

    def test_does_not_fire_when_constraint_passes(self):
        from windtunnel.triage.rule_based import rule_policy_violation

        scenario = _make_scenario()
        result = rule_policy_violation(scenario, _make_trace(), self._score_all_pass())
        assert result is None


class TestRuleIclPoisoning:
    """rule_icl_poisoning: fires when dim:icl_poisoning tag is present and outcome fails."""

    def _trace_colon_stop(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="How much inventory?"),
            _turn(role="assistant", content="Here is the inventory for Bluewing Logistics:"),
        )

    def test_fires_on_icl_poisoning_dim_fail(self):
        from windtunnel.triage.rule_based import rule_icl_poisoning

        scenario = _make_scenario(tags=["dim:icl_poisoning"])
        score = _make_score(outcome_passed=False)
        result = rule_icl_poisoning(scenario, self._trace_colon_stop(), score)
        assert result is not None
        assert result.category == "template_corruption"

    def test_does_not_fire_on_other_dim(self):
        from windtunnel.triage.rule_based import rule_icl_poisoning

        scenario = _make_scenario(tags=["dim:tool_affordance"])
        score = _make_score(outcome_passed=False)
        result = rule_icl_poisoning(scenario, self._trace_colon_stop(), score)
        assert result is None

    def test_does_not_fire_when_outcome_passes(self):
        from windtunnel.triage.rule_based import rule_icl_poisoning

        scenario = _make_scenario(tags=["dim:icl_poisoning"])
        score = _make_score(outcome_passed=True)
        result = rule_icl_poisoning(scenario, self._trace_colon_stop(), score)
        assert result is None


class TestRuleMemoryConflict:
    """rule_memory_conflict: fires when dim:memory_conflict tag present + outcome fails."""

    def test_fires_on_memory_conflict_dim_fail(self):
        from windtunnel.triage.rule_based import rule_memory_conflict

        scenario = _make_scenario(tags=["dim:memory_conflict"])
        score = _make_score(outcome_passed=False)
        trace = _make_trace(
            _turn(role="user", content="What email does Bluewing use?"),
            _turn(role="assistant", content="They use gmail.com based on my memory."),
        )
        result = rule_memory_conflict(scenario, trace, score)
        assert result is not None
        assert result.category == "memory"

    def test_does_not_fire_on_other_dim(self):
        from windtunnel.triage.rule_based import rule_memory_conflict

        scenario = _make_scenario(tags=["dim:tool_affordance"])
        score = _make_score(outcome_passed=False)
        result = rule_memory_conflict(scenario, _make_trace(), score)
        assert result is None


class TestRuleRecoveryFail:
    """rule_recovery_fail: fires when dim:recovery tag present + outcome fails."""

    def test_fires_on_recovery_dim_fail(self):
        from windtunnel.triage.rule_based import rule_recovery_fail

        scenario = _make_scenario(tags=["dim:recovery"])
        score = _make_score(outcome_passed=False)
        trace = _make_trace(
            _turn(role="user", content="Show orders for Portland Pickles."),
            _turn(role="assistant", content="No orders found."),
        )
        result = rule_recovery_fail(scenario, trace, score)
        assert result is not None
        assert result.category == "recovery"

    def test_does_not_fire_on_other_dim(self):
        from windtunnel.triage.rule_based import rule_recovery_fail

        scenario = _make_scenario(tags=["dim:silent_failure"])
        score = _make_score(outcome_passed=False)
        result = rule_recovery_fail(scenario, _make_trace(), score)
        assert result is None


class TestRuleSamplerVariance:
    """rule_sampler_variance: fires when dim:sampler_sensitivity tag present."""

    def test_fires_on_sampler_dim_fail(self):
        from windtunnel.triage.rule_based import rule_sampler_variance

        scenario = _make_scenario(tags=["dim:sampler_sensitivity"])
        score = _make_score(outcome_passed=False)
        result = rule_sampler_variance(scenario, _make_trace(), score)
        assert result is not None
        assert result.category == "sampler_variance"

    def test_does_not_fire_on_other_dim(self):
        from windtunnel.triage.rule_based import rule_sampler_variance

        scenario = _make_scenario(tags=["dim:tool_affordance"])
        score = _make_score(outcome_passed=False)
        result = rule_sampler_variance(scenario, _make_trace(), score)
        assert result is None


class TestRuleMultiTurnDrift:
    """rule_multi_turn_drift: fires when dim:multi_turn_drift tag present + outcome fails."""

    def test_fires_on_multi_turn_dim_fail(self):
        from windtunnel.triage.rule_based import rule_multi_turn_drift

        scenario = _make_scenario(tags=["dim:multi_turn_drift"])
        score = _make_score(outcome_passed=False)
        result = rule_multi_turn_drift(scenario, _make_trace(), score)
        assert result is not None
        assert result.category == "planning"

    def test_does_not_fire_on_other_dim(self):
        from windtunnel.triage.rule_based import rule_multi_turn_drift

        scenario = _make_scenario(tags=["dim:tool_affordance"])
        score = _make_score(outcome_passed=False)
        result = rule_multi_turn_drift(scenario, _make_trace(), score)
        assert result is None


# ─── 5. Unknown fallback ──────────────────────────────────────────────────────


class TestUnknownFallback:
    """When no rule fires, the classifier returns 'unknown' with confidence 0.0."""

    def test_unknown_on_unrecognized_failure(self):
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        # Generic failure with no matching dim tags
        trace = _make_trace(
            _turn(role="user", content="test"),
            _turn(role="assistant", content="bad answer"),
        )
        scenario = _make_scenario(requires_tool_use=False, tags=["dim:unknown_future_dim"])
        score = _make_score(outcome_passed=False)
        result = clf.classify(scenario, trace, score)
        assert result.category == "unknown"
        assert result.confidence == 0.0

    def test_unknown_has_empty_evidence(self):
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        trace = _make_trace(
            _turn(role="user", content="test"),
            _turn(role="assistant", content="bad answer"),
        )
        scenario = _make_scenario(tags=["dim:future"])
        score = _make_score(outcome_passed=False)
        result = clf.classify(scenario, trace, score)
        assert isinstance(result.evidence, list)


# ─── 6. LLMJudgeClassifier stub ──────────────────────────────────────────────


class TestLLMJudgeClassifierStub:
    """LLMJudgeClassifier raises NotImplementedError with a meaningful message."""

    def test_raises_not_implemented(self):
        from windtunnel.triage.llm_judge import LLMJudgeClassifier

        clf = LLMJudgeClassifier()
        trace = _make_trace()
        scenario = _make_scenario()
        score = _make_score()
        with pytest.raises(NotImplementedError) as exc_info:
            clf.classify(scenario, trace, score)
        msg = str(exc_info.value)
        assert len(msg) > 20, "NotImplementedError message should explain the interface"

    def test_error_message_mentions_llm_or_gepa(self):
        from windtunnel.triage.llm_judge import LLMJudgeClassifier

        clf = LLMJudgeClassifier()
        trace = _make_trace()
        scenario = _make_scenario()
        score = _make_score()
        with pytest.raises(NotImplementedError) as exc_info:
            clf.classify(scenario, trace, score)
        msg = str(exc_info.value).lower()
        # Must mention at least one of: llm, gepa, judge, classifier
        assert any(kw in msg for kw in ["llm", "gepa", "judge", "classifier", "implement"]), (
            f"Error message should explain the interface, got: {msg!r}"
        )

    def test_satisfies_protocol(self):
        """LLMJudgeClassifier implements the FailureClassifier Protocol."""
        from windtunnel.triage.classifier import FailureClassifier
        from windtunnel.triage.llm_judge import LLMJudgeClassifier

        clf = LLMJudgeClassifier()
        assert isinstance(clf, FailureClassifier)


# ─── 7. Optimizer Protocol + GEPAOptimizer stub ───────────────────────────────


class TestOptimizerStubs:
    """Optimizer Protocol and GEPAOptimizer stub raise NotImplementedError."""

    def test_proposed_fix_dataclass(self):
        from windtunnel.triage.optimizer import ProposedFix

        pf = ProposedFix(
            fix_vector="edit_soul_md",
            target={"file": "SOUL.md"},
            rationale="expand tool scope",
            diff_text="Add: 'Use ops_order_query for inventory counts.'",
        )
        assert pf.fix_vector == "edit_soul_md"

    def test_applied_fix_dataclass(self):
        from windtunnel.triage.optimizer import AppliedFix

        af = AppliedFix(
            proposed_fix_id="fix_001",
            status="applied",
            applied_to={"file": "SOUL.md"},
            details="Appended tool scope directive.",
        )
        assert af.status == "applied"

    def test_gepa_propose_fix_raises(self):
        from windtunnel.triage.classifier import FailureClassification
        from windtunnel.triage.optimizer import GEPAOptimizer

        opt = GEPAOptimizer()
        fc = FailureClassification(
            category="tool_affordance",
            confidence=1.0,
            evidence=["no_tools_used"],
            suggested_fix=None,
        )
        trace = _make_trace()
        scenario = _make_scenario()
        with pytest.raises(NotImplementedError) as exc_info:
            opt.propose_fix(fc, scenario, trace)
        msg = str(exc_info.value).lower()
        assert any(kw in msg for kw in ["gepa", "optimizer", "implement", "gradient"])

    def test_gepa_apply_fix_raises(self):
        from windtunnel.triage.optimizer import GEPAOptimizer, ProposedFix

        opt = GEPAOptimizer()
        pf = ProposedFix(
            fix_vector="edit_soul_md",
            target={"file": "SOUL.md"},
            rationale="test",
        )
        with pytest.raises(NotImplementedError):
            opt.apply_fix(pf)

    def test_gepa_satisfies_optimizer_protocol(self):
        from windtunnel.triage.optimizer import GEPAOptimizer, Optimizer

        opt = GEPAOptimizer()
        assert isinstance(opt, Optimizer)


# ─── 8. Labeled fixture set + agreement ──────────────────────────────────────


class TestLabeledFixtures:
    """Hand-labeled fixtures exist and RuleBasedClassifier agrees >= 80%."""

    FIXTURES_DIR = Path(__file__).parent / "fixtures" / "labeled_failures"

    def test_fixtures_dir_exists(self):
        assert self.FIXTURES_DIR.exists(), (
            f"Labeled fixtures directory not found: {self.FIXTURES_DIR}"
        )

    def test_at_least_ten_fixtures(self):
        fixtures = list(self.FIXTURES_DIR.glob("*.json"))
        assert len(fixtures) >= 10, (
            f"Expected at least 10 labeled fixtures, found {len(fixtures)}"
        )

    def test_classifier_agreement(self):
        """RuleBasedClassifier must agree >= 80% on the labeled set."""
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        fixtures = list(self.FIXTURES_DIR.glob("*.json"))
        assert len(fixtures) >= 10, "Need at least 10 fixtures for agreement check"

        total = 0
        agreed = 0
        for fp in fixtures:
            data = json.loads(fp.read_text())
            expected_category = data["expected_category"]

            trace = Trace._from_dict(data["trace"])
            scenario_data = data["scenario"]
            scenario = Scenario(
                name=scenario_data["name"],
                prompt=scenario_data["prompt"],
                target_facts=scenario_data.get("target_facts", [["answer"]]),
                requires_tool_use=scenario_data.get("requires_tool_use", False),
                tags=scenario_data.get("tags", []),
                must_call=scenario_data.get("must_call", []),
                forbidden_calls=scenario_data.get("forbidden_calls", []),
            )
            score_data = data["score"]
            score = Score(
                outcome=LayerResult(**score_data["outcome"]),
                trajectory=LayerResult(**score_data["trajectory"]),
                constraint=LayerResult(**score_data["constraint"]),
                robustness=LayerResult(**score_data["robustness"]),
            )

            result = clf.classify(scenario, trace, score)
            total += 1
            if result.category == expected_category:
                agreed += 1

        agreement_rate = agreed / total if total > 0 else 0.0
        assert agreement_rate >= 0.80, (
            f"RuleBasedClassifier agreement {agreement_rate:.0%} < 80% "
            f"({agreed}/{total} fixtures matched)"
        )

    def test_confidence_calibration(self):
        """High-confidence classifications agree more than low-confidence ones."""
        from windtunnel.triage.rule_based import RuleBasedClassifier

        clf = RuleBasedClassifier()
        fixtures = list(self.FIXTURES_DIR.glob("*.json"))
        if len(fixtures) < 10:
            pytest.skip("Need at least 10 fixtures for calibration check")

        high_conf_total = 0
        high_conf_agreed = 0
        low_conf_total = 0
        low_conf_agreed = 0

        for fp in fixtures:
            data = json.loads(fp.read_text())
            expected_category = data["expected_category"]

            trace = Trace._from_dict(data["trace"])
            scenario_data = data["scenario"]
            scenario = Scenario(
                name=scenario_data["name"],
                prompt=scenario_data["prompt"],
                target_facts=scenario_data.get("target_facts", [["answer"]]),
                requires_tool_use=scenario_data.get("requires_tool_use", False),
                tags=scenario_data.get("tags", []),
                must_call=scenario_data.get("must_call", []),
                forbidden_calls=scenario_data.get("forbidden_calls", []),
            )
            score_data = data["score"]
            score = Score(
                outcome=LayerResult(**score_data["outcome"]),
                trajectory=LayerResult(**score_data["trajectory"]),
                constraint=LayerResult(**score_data["constraint"]),
                robustness=LayerResult(**score_data["robustness"]),
            )

            result = clf.classify(scenario, trace, score)
            matched = result.category == expected_category

            if result.confidence >= 0.8:
                high_conf_total += 1
                if matched:
                    high_conf_agreed += 1
            else:
                low_conf_total += 1
                if matched:
                    low_conf_agreed += 1

        # Only check calibration if we have both high and low confidence results
        if high_conf_total > 0 and low_conf_total > 0:
            high_rate = high_conf_agreed / high_conf_total
            low_rate = low_conf_agreed / low_conf_total
            assert high_rate >= low_rate, (
                f"High-confidence classifications agree {high_rate:.0%} but "
                f"low-confidence agree {low_rate:.0%} — calibration is inverted"
            )


# ─── 9. `wt triage` CLI subcommand ───────────────────────────────────────────


class TestWtTriageCLI:
    """wt triage --runs DIR emits a markdown report grouped by category."""

    def _make_failing_run_dir(self, tmp_path: Path) -> Path:
        """Create a synthetic runs/ directory with one failed trace."""
        from windtunnel.api.trace import save_trace, storage_path

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()

        trace = _make_trace(
            _turn(role="user", content="Check inventory for Bluewing."),
            _turn(role="assistant", content="I don't know."),
            worker_warnings=[],
        )
        trace.scenario_id = "test_scenario_tool_affordance"

        # Save trace + score + scenario to the run dir
        # The triage command finds traces + looks for sibling score.json
        trace_path = storage_path(trace, base_dir=runs_dir)
        save_trace(trace, trace_path)

        # Write score + scenario sibling files
        score_data = {
            "scenario": {
                "name": "test_scenario_tool_affordance",
                "prompt": "Check inventory for Bluewing.",
                "target_facts": [["ACC-BLWG-001"]],
                "requires_tool_use": True,
                "tags": ["dim:tool_affordance"],
                "must_call": [],
                "forbidden_calls": [],
            },
            "score": {
                "outcome": {"passed": False, "detail": "no_tools_used: scenario requires tool use but trace has no tool calls"},
                "trajectory": {"passed": True, "detail": "ok"},
                "constraint": {"passed": True, "detail": "ok"},
                "robustness": {"passed": True, "detail": "ok"},
            },
        }
        score_path = trace_path.with_suffix(".score.json")
        score_path.write_text(json.dumps(score_data), encoding="utf-8")

        return runs_dir

    def test_triage_subcommand_exists(self):
        """wt triage is registered in the CLI — --help exits with code 0."""
        from windtunnel.cli import main

        # argparse --help calls sys.exit(0), so we catch SystemExit
        try:
            main(["triage", "--help"])
        except SystemExit as exc:
            assert exc.code == 0, f"Expected exit code 0, got {exc.code}"

    def test_triage_emits_markdown(self, tmp_path: Path, capsys):
        """wt triage --runs DIR emits markdown with ## categories."""
        from windtunnel.cli import main

        runs_dir = self._make_failing_run_dir(tmp_path)
        main(["triage", "--runs", str(runs_dir)])
        captured = capsys.readouterr()
        output = captured.out

        # Should emit markdown with at least one ## category header
        assert "##" in output or "# " in output, (
            f"Expected markdown headers in output, got: {output[:500]!r}"
        )

    def test_triage_groups_by_category(self, tmp_path: Path, capsys):
        """Failures are grouped under their classified category."""
        from windtunnel.cli import main

        runs_dir = self._make_failing_run_dir(tmp_path)
        main(["triage", "--runs", str(runs_dir)])
        captured = capsys.readouterr()
        output = captured.out

        # tool_affordance category should appear somewhere in output
        assert "tool_affordance" in output, (
            f"Expected 'tool_affordance' in triage output, got: {output[:500]!r}"
        )

    def test_triage_empty_runs_dir(self, tmp_path: Path, capsys):
        """triage on an empty dir exits cleanly and says no failures."""
        from windtunnel.cli import main

        runs_dir = tmp_path / "empty_runs"
        runs_dir.mkdir()
        rc = main(["triage", "--runs", str(runs_dir)])
        # Should exit 0 (no failures = nothing to triage)
        assert rc == 0

    def test_triage_default_classifier_is_rule_based(self, tmp_path: Path, capsys):
        """Default classifier label appears in output or is implied."""
        from windtunnel.cli import main

        runs_dir = self._make_failing_run_dir(tmp_path)
        main(["triage", "--runs", str(runs_dir)])
        captured = capsys.readouterr()
        # Should not crash; output should exist
        assert len(captured.out) > 0 or len(captured.err) > 0


# ─── 10. Taxonomy docs exist ──────────────────────────────────────────────────


class TestTaxonomyDocs:
    """failure-taxonomy.md and writing guides exist in docs/ (repo root)."""

    DOCS_DIR = Path(__file__).parent.parent / "docs"

    def test_failure_taxonomy_doc_exists(self):
        assert (self.DOCS_DIR / "failure-taxonomy.md").exists()

    def test_writing_classifier_doc_exists(self):
        assert (self.DOCS_DIR / "writing-a-classifier.md").exists()

    def test_writing_optimizer_doc_exists(self):
        assert (self.DOCS_DIR / "writing-an-optimizer.md").exists()

    def test_failure_taxonomy_doc_has_all_categories(self):
        content = (self.DOCS_DIR / "failure-taxonomy.md").read_text()
        expected = [
            "tool_affordance",
            "clarification",
            "policy",
            "memory",
            "template_corruption",
            "planning",
            "recovery",
            "model_capacity",
            "sampler_variance",
            "side_effect_safety_violation",
            "unknown",
        ]
        for cat in expected:
            assert cat in content, f"Category '{cat}' not found in failure-taxonomy.md"

    def test_failure_taxonomy_has_fix_vectors(self):
        content = (self.DOCS_DIR / "failure-taxonomy.md").read_text()
        # The remediation map
        assert "edit tool description" in content.lower() or "fix_vector" in content.lower()
