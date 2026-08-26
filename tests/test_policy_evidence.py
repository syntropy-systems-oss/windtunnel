"""Policy evidence anchors — PolicyVerdict + EvidenceAnchor contracts.

Covers:
  1. EvidenceAnchor shape validation per kind (witnessed_call / span /
     locator), including rejected cross-kind fields
  2. PolicyVerdict: list→tuple coercion, anchor type checking
  3. evaluate_constraint back-compat: plain bool policies are untouched,
     PolicyVerdict.passed is authoritative (the truthy-object trap), failing
     verdict details are appended to the layer detail, mixed policies work
  4. Evidence recomputation: anchors surface per policy, a raising
     predicate degrades to the opaque presentation
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from windtunnel.api.evaluators import evaluate_constraint
from windtunnel.api.scenario import EvidenceAnchor, Policy, PolicyVerdict, Scenario
from windtunnel.api.trace import Trace, Turn


def _trace(content: str = "ok") -> Trace:
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    return Trace(
        scenario_id="policy_case",
        agent_id="wt-cli",
        variant_id="candidate",
        model="model-x",
        quant="q4",
        sampler={},
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        turns=[
            Turn(role="user", content="go", tool_calls=[], tool_results=[], latency_ms=1.0),
            Turn(role="assistant", content=content, tool_calls=[], tool_results=[], latency_ms=1.0),
        ],
        tool_schema_hash=None,
    )


class TestEvidenceAnchorShape:
    def test_witnessed_call_anchor(self) -> None:
        anchor = EvidenceAnchor(kind="witnessed_call", call_index=3, note="the retry")
        assert anchor.call_index == 3

    def test_witnessed_call_requires_call_index(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="witnessed_call")
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="witnessed_call", call_index=-1)

    def test_witnessed_call_rejects_span_fields(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="witnessed_call", call_index=0, turn_index=1)

    def test_span_anchor(self) -> None:
        anchor = EvidenceAnchor(kind="span", turn_index=1, start=0, end=2)
        assert (anchor.start, anchor.end) == (0, 2)

    def test_span_requires_valid_bounds(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="span", turn_index=1, start=2, end=2)
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="span", turn_index=1, start=-1, end=2)
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="span", start=0, end=2)  # missing turn_index

    def test_span_rejects_call_index(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="span", turn_index=0, start=0, end=1, call_index=0)

    def test_locator_requires_note(self) -> None:
        assert EvidenceAnchor(kind="locator", note="workspace/notes.txt").note
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="locator", note="  ")

    def test_locator_rejects_positional_fields(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="locator", note="x", call_index=0)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            EvidenceAnchor(kind="paragraph")  # type: ignore[arg-type]


class TestPolicyVerdictShape:
    def test_anchor_list_coerced_to_tuple(self) -> None:
        verdict = PolicyVerdict(
            passed=True, anchors=[EvidenceAnchor(kind="locator", note="x")]
        )
        assert isinstance(verdict.anchors, tuple)

    def test_non_anchor_entries_rejected(self) -> None:
        with pytest.raises(ValueError):
            PolicyVerdict(passed=True, anchors=[{"kind": "locator", "note": "x"}])  # type: ignore[list-item]

    def test_defaults_are_bare(self) -> None:
        verdict = PolicyVerdict(passed=False)
        assert verdict.detail == ""
        assert verdict.anchors == ()


class TestEvaluateConstraintBackCompat:
    def test_plain_bool_policies_unchanged(self) -> None:
        scenario = Scenario(
            name="policy_case",
            prompt="go",
            policies=[
                Policy(name="holds", predicate=lambda t: True),
                Policy(name="breaks", predicate=lambda t: False),
            ],
        )
        result = evaluate_constraint(_trace(), scenario)
        assert result.passed is False
        assert "'breaks'" in result.detail
        assert "'holds'" not in result.detail

    def test_verdict_passed_is_authoritative_not_truthiness(self) -> None:
        """A PolicyVerdict object is truthy even when passed=False — the
        evaluator must read .passed, never bool(result)."""
        scenario = Scenario(
            name="policy_case",
            prompt="go",
            policies=[Policy(name="strict", predicate=lambda t: PolicyVerdict(passed=False))],
        )
        result = evaluate_constraint(_trace(), scenario)
        assert result.passed is False
        assert "'strict'" in result.detail

    def test_failing_verdict_detail_is_appended(self) -> None:
        scenario = Scenario(
            name="policy_case",
            prompt="go",
            policies=[Policy(
                name="cited",
                predicate=lambda t: PolicyVerdict(passed=False, detail="no citation found"),
            )],
        )
        result = evaluate_constraint(_trace(), scenario)
        assert "'cited'" in result.detail
        assert "cited: no citation found" in result.detail

    def test_passing_verdict_and_mixed_policies(self) -> None:
        scenario = Scenario(
            name="policy_case",
            prompt="go",
            policies=[
                Policy(name="plain_ok", predicate=lambda t: True),
                Policy(
                    name="anchored_ok",
                    predicate=lambda t: PolicyVerdict(
                        passed=True,
                        anchors=[EvidenceAnchor(kind="span", turn_index=1, start=0, end=2)],
                    ),
                ),
            ],
        )
        result = evaluate_constraint(_trace(), scenario)
        assert result.passed is True
        assert result.detail == "all constraints satisfied"

    def test_raising_predicate_still_fails_closed(self) -> None:
        def boom(trace: Trace) -> bool:
            raise RuntimeError("fixture unavailable")

        scenario = Scenario(
            name="policy_case", prompt="go", policies=[Policy(name="boom", predicate=boom)]
        )
        result = evaluate_constraint(_trace(), scenario)
        assert result.passed is False
        assert "boom(error: fixture unavailable)" in result.detail


class TestConstraintEvidenceRecomputation:
    def test_anchors_surface_per_policy(self) -> None:
        from windtunnel._serve.evidence import compute_evidence

        scenario = Scenario(
            name="policy_case",
            prompt="go",
            policies=[Policy(
                name="anchored",
                predicate=lambda t: PolicyVerdict(
                    passed=True,
                    detail="looked at the answer and one witnessed call",
                    anchors=[
                        EvidenceAnchor(kind="witnessed_call", call_index=0),
                        EvidenceAnchor(kind="span", turn_index=1, start=0, end=2),
                        EvidenceAnchor(kind="locator", note="workspace/notes.txt"),
                    ],
                ),
            )],
        )
        entry = compute_evidence(scenario, _trace())["constraint"]["policies"][0]
        assert entry["anchorable"] is True
        assert entry["call_anchors"] == [{"call_index": 0, "note": ""}]
        assert entry["span_anchors"] == [
            {"turn_index": 1, "start": 0, "end": 2, "note": ""}
        ]
        assert entry["locators"] == ["workspace/notes.txt"]

    def test_check_source_is_introspected_for_def_based_callables(self) -> None:
        """'What exactly is windtunnel expecting?' — the evidence endpoint
        carries the opaque check's own source when introspectable."""
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.score import LayerResult

        def answer_mentions_the_total(trace: Trace) -> LayerResult:
            mentioned = "12" in trace.turns[-1].content
            return LayerResult(passed=mentioned, detail="checked for the total")

        def no_second_send(trace: Trace) -> bool:
            return len(trace.mcp_calls) <= 1

        scenario = Scenario(
            name="policy_case",
            prompt="go",
            outcome_fn=answer_mentions_the_total,
            policies=[Policy(name="no_second_send", predicate=no_second_send)],
        )
        evidence = compute_evidence(scenario, _trace())
        assert "def answer_mentions_the_total" in evidence["outcome"]["outcome_fn_source"]
        policy_entry = evidence["constraint"]["policies"][0]
        assert "def no_second_send" in policy_entry["source"]

    def test_unsourceable_callable_degrades_to_honest_absence(self) -> None:
        from windtunnel._serve.evidence import compute_evidence

        scenario = Scenario(
            name="policy_case",
            prompt="go",
            # A C builtin is callable but has no Python source.
            policies=[Policy(name="opaque_builtin", predicate=bool)],
        )
        entry = compute_evidence(scenario, _trace())["constraint"]["policies"][0]
        assert entry["source"] is None

    def test_custom_trajectory_check_source_surfaces(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import TrajectoryCheck

        class AtMostTwoLookups(TrajectoryCheck):
            def check(self, calls: list[str]) -> tuple[bool, str]:
                lookups = sum(1 for name in calls if name.endswith("client_lookup"))
                return lookups <= 2, f"{lookups} lookup(s)"

        scenario = Scenario(
            name="policy_case", prompt="go", trajectory_checks=[AtMostTwoLookups()]
        )
        checks = compute_evidence(scenario, _trace())["trajectory"]["custom_checks"]
        assert checks[0]["name"] == "AtMostTwoLookups"
        assert "class AtMostTwoLookups" in checks[0]["source"]

    def test_over_long_source_is_truncated_with_a_marker(self) -> None:
        from windtunnel._serve import evidence as evidence_module

        def long_check(trace: Trace) -> bool:  # pragma: no cover - never called
            return True

        original = evidence_module._SOURCE_CHAR_LIMIT
        evidence_module._SOURCE_CHAR_LIMIT = 10
        try:
            source = evidence_module._callable_source(long_check)
        finally:
            evidence_module._SOURCE_CHAR_LIMIT = original
        assert source is not None
        assert "more characters]" in source

    def test_raising_predicate_degrades_to_opaque(self) -> None:
        from windtunnel._serve.evidence import compute_evidence

        def boom(trace: Trace) -> bool:
            raise RuntimeError("fixture unavailable")

        scenario = Scenario(
            name="policy_case", prompt="go", policies=[Policy(name="boom", predicate=boom)]
        )
        entry = compute_evidence(scenario, _trace())["constraint"]["policies"][0]
        assert entry["anchorable"] is False
        assert entry["recomputed_passed"] is None
        assert "could not be re-evaluated" in entry["detail"]
