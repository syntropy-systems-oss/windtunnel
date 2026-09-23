"""Structured metrics: named measurements carried next to a layer's verdict.

Covers the LayerResult.metrics contract (validation, backward-compatible
defaults), Score.metrics flattening, sidecar persistence and round-trips,
aggregation by value type, propagation through scorers/trajectory
checks/policies, and the end-to-end path from an outcome_fn to a run's
aggregate.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from windtunnel.api import (
    LayerResult,
    MetricSummary,
    Policy,
    Scenario,
    Score,
    ScoreFormatError,
    Trace,
    TrajectoryCheck,
    Turn,
    aggregate_metrics,
    aggregate_runs,
    all_of,
    any_of,
    evaluate_constraint,
    evaluate_trajectory,
    run_scenario,
    score_from_dict,
    score_to_dict,
)
from windtunnel.api.aggregate import ScenarioRunResult
from windtunnel.runtimes.in_memory import InMemoryRuntime


def _trace(answer: str = "ok", tool_names: list[str] | None = None) -> Trace:
    tool_calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for i, name in enumerate(tool_names or [])
    ]
    now = datetime(2026, 1, 2, tzinfo=UTC)
    return Trace(
        scenario_id="metrics",
        agent_id="agent",
        variant_id="candidate",
        model="model",
        quant="q",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=[
            Turn(role="user", content="q", tool_calls=[], tool_results=[], latency_ms=0.0),
            Turn(
                role="assistant",
                content=answer,
                tool_calls=tool_calls,
                tool_results=[],
                latency_ms=1.0,
            ),
        ],
        tool_schema_hash=None,
    )


def _score(outcome: LayerResult, trajectory: LayerResult | None = None) -> Score:
    return Score(
        outcome=outcome,
        trajectory=trajectory or LayerResult(True, "trajectory"),
        constraint=LayerResult(True, "constraint"),
        integrity=LayerResult(True, "integrity"),
    )


class TestLayerResultMetrics:
    def test_metrics_default_to_empty_so_existing_constructions_are_unchanged(self) -> None:
        layer = LayerResult(True, "detail")
        assert layer.metrics == {}
        assert layer == LayerResult(passed=True, detail="detail")

    def test_accepts_bool_int_float_and_str_values(self) -> None:
        layer = LayerResult(
            True,
            "ok",
            metrics={"final_correct": True, "revisions": 9, "ratio": 0.5, "path": "retry"},
        )
        assert layer.metrics == {
            "final_correct": True,
            "revisions": 9,
            "ratio": 0.5,
            "path": "retry",
        }

    def test_other_real_numbers_are_converted_to_plain_floats(self) -> None:
        layer = LayerResult(True, "ok", metrics={"half": Fraction(1, 2)})
        assert layer.metrics == {"half": 0.5}
        assert type(layer.metrics["half"]) is float

    def test_metrics_are_copied_not_aliased(self) -> None:
        source = {"revisions": 1}
        layer = LayerResult(True, "ok", metrics=source)
        source["revisions"] = 2
        assert layer.metrics == {"revisions": 1}

    @pytest.mark.parametrize("name", ["", "   ", 3])
    def test_rejects_empty_or_non_string_names(self, name: object) -> None:
        with pytest.raises(ValueError, match="metric names"):
            LayerResult(True, "ok", metrics={name: 1})  # type: ignore[dict-item]

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_rejects_non_finite_numbers_which_are_not_valid_json(self, value: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            LayerResult(True, "ok", metrics={"ratio": value})

    @pytest.mark.parametrize("value", [None, [1], {"a": 1}])
    def test_rejects_structured_values(self, value: object) -> None:
        with pytest.raises(TypeError, match="must be bool, int, float, or str"):
            LayerResult(True, "ok", metrics={"bad": value})  # type: ignore[dict-item]


class TestScoreMetrics:
    def test_score_metrics_are_flattened_and_qualified_by_layer(self) -> None:
        score = _score(
            LayerResult(True, "ok", metrics={"count": 1}),
            trajectory=LayerResult(True, "ok", metrics={"count": 2}),
        )
        assert score.metrics == {"outcome.count": 1, "trajectory.count": 2}

    def test_score_without_metrics_serializes_exactly_as_before(self) -> None:
        payload = score_to_dict(_score(LayerResult(True, "ok")))
        assert payload["outcome"] == {"passed": True, "detail": "ok"}
        assert "metrics" not in json.dumps(payload)

    def test_metrics_round_trip_through_the_sidecar_shape(self) -> None:
        score = _score(LayerResult(False, "no", metrics={"revisions": 9, "path": "retry"}))
        payload = score_to_dict(score)
        assert payload["outcome"]["metrics"] == {"revisions": 9, "path": "retry"}
        assert score_from_dict(json.loads(json.dumps(payload))).metrics == {
            "outcome.revisions": 9,
            "outcome.path": "retry",
        }

    def test_invalid_persisted_metrics_are_a_score_format_error(self) -> None:
        payload = score_to_dict(_score(LayerResult(True, "ok")))
        payload["outcome"]["metrics"] = {"bad": [1, 2]}
        with pytest.raises(ScoreFormatError):
            score_from_dict(payload)

    def test_score_sidecar_persists_metrics_in_both_consumer_shapes(self, tmp_path: Path) -> None:
        from windtunnel._cli.storage import _write_score_sidecar

        scenario = Scenario(name="metrics", prompt="q")
        score = _score(LayerResult(True, "ok", metrics={"final_correct": True}))
        sidecar_path = _write_score_sidecar(tmp_path / "run.json", score, scenario)
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert data["outcome"]["metrics"] == {"final_correct": True}
        assert data["score"]["outcome"]["metrics"] == {"final_correct": True}


class TestAggregateMetrics:
    def test_booleans_aggregate_to_a_rate(self) -> None:
        summary = aggregate_metrics([{"ok": True}, {"ok": False}, {"ok": True}, {"ok": True}])
        assert summary["ok"] == MetricSummary(kind="bool", count=4, true_count=3, rate=0.75)

    def test_numbers_aggregate_to_mean_min_and_max(self) -> None:
        summary = aggregate_metrics([{"n": 6}, {"n": 11}, {"n": 7.5}])["n"]
        assert summary.kind == "number"
        assert summary.count == 3
        assert summary.mean == pytest.approx(8.1666666)
        assert (summary.min, summary.max) == (6, 11)

    def test_strings_aggregate_to_counts_most_frequent_first(self) -> None:
        summary = aggregate_metrics([{"p": "b"}, {"p": "a"}, {"p": "a"}, {"p": "c"}])["p"]
        assert summary.kind == "string"
        assert list(summary.counts or {}) == ["a", "b", "c"]
        assert summary.counts == {"a": 2, "b": 1, "c": 1}

    def test_disagreeing_types_fall_back_to_counts_of_json_values(self) -> None:
        summary = aggregate_metrics([{"x": True}, {"x": 3}, {"x": "3"}])["x"]
        assert summary.kind == "mixed"
        assert summary.counts == {'"3"': 1, "3": 1, "true": 1}

    def test_count_only_includes_runs_that_reported_the_metric(self) -> None:
        summary = aggregate_metrics([{"n": 1}, {}, {"n": 3}])
        assert summary["n"].count == 2
        assert summary["n"].mean == 2.0

    def test_names_are_sorted_and_to_dict_drops_inapplicable_fields(self) -> None:
        summary = aggregate_metrics([{"z": 1, "a": True}])
        assert list(summary) == ["a", "z"]
        assert summary["a"].to_dict() == {"kind": "bool", "count": 1, "true_count": 1, "rate": 1.0}
        assert summary["z"].to_dict() == {
            "kind": "number",
            "count": 1,
            "mean": 1.0,
            "min": 1,
            "max": 1,
        }

    def test_aggregate_runs_carries_metric_summaries(self) -> None:
        runs = [
            ScenarioRunResult(
                score=_score(LayerResult(passed, "x", metrics={"revisions": revisions})),
                trace=_trace(),
            )
            for passed, revisions in [(True, 4), (False, 8)]
        ]
        aggregate = aggregate_runs(runs)
        assert aggregate.metrics["outcome.revisions"].mean == 6.0
        assert aggregate_runs([]).metrics == {}


class TestMetricPropagation:
    def test_all_of_merges_every_child_with_later_children_winning(self) -> None:
        scorer = all_of(
            lambda _t: LayerResult(True, "a", metrics={"shared": 1, "first": True}),
            lambda _t: LayerResult(False, "b", metrics={"shared": 2}),
        )
        result = scorer(_trace())
        assert result.passed is False
        assert result.metrics == {"shared": 2, "first": True}

    def test_any_of_merges_children_up_to_the_first_pass(self) -> None:
        scorer = any_of(
            lambda _t: LayerResult(False, "a", metrics={"tried": 1}),
            lambda _t: LayerResult(True, "b", metrics={"won": True}),
            lambda _t: LayerResult(True, "c", metrics={"never": True}),
        )
        result = scorer(_trace())
        assert result.passed is True
        assert result.metrics == {"tried": 1, "won": True}

    def test_trajectory_check_may_return_a_layer_result_with_metrics(self) -> None:
        class CountLookups(TrajectoryCheck):
            def check(self, calls: list[str]) -> LayerResult:
                n = sum(1 for call in calls if call == "lookup")
                return LayerResult(n <= 1, f"{n} lookups", metrics={"lookups": n})

        scenario = Scenario(name="s", prompt="q", trajectory_checks=[CountLookups()])
        result = evaluate_trajectory(_trace(tool_names=["lookup", "lookup"]), scenario)
        assert result.passed is False
        assert "2 lookups" in result.detail
        assert result.metrics == {"lookups": 2}

    def test_tuple_returning_trajectory_checks_still_work(self) -> None:
        class Always(TrajectoryCheck):
            def check(self, calls: list[str]) -> tuple[bool, str]:
                return True, "fine"

        scenario = Scenario(name="s", prompt="q", trajectory_checks=[Always()])
        result = evaluate_trajectory(_trace(), scenario)
        assert result.passed is True
        assert result.metrics == {}

    def test_policy_may_return_a_layer_result_with_metrics_and_detail(self) -> None:
        scenario = Scenario(
            name="s",
            prompt="q",
            policies=[
                Policy(
                    name="write_budget",
                    predicate=lambda _t: LayerResult(False, "3 writes > 2", metrics={"writes": 3}),
                ),
                Policy(name="plain", predicate=lambda _t: True),
            ],
        )
        result = evaluate_constraint(_trace(), scenario)
        assert result.passed is False
        assert "write_budget: 3 writes > 2" in result.detail
        assert result.metrics == {"writes": 3}


class TestEndToEnd:
    def test_outcome_fn_metrics_reach_the_run_score_and_the_aggregate(self) -> None:
        def outcome(trace: Trace) -> LayerResult:
            answer = trace.turns[-1].content
            return LayerResult(
                passed="42" in answer,
                detail="graded",
                metrics={"final_correct": "42" in answer, "answer_chars": len(answer)},
            )

        scenario = Scenario(name="e2e", prompt="q", outcome_fn=outcome)
        result = run_scenario(
            scenario,
            InMemoryRuntime(scripted_responses=["it is 42"]),
            runs_per_scenario=2,
        )
        assert result.runs[0].score.metrics == {
            "outcome.final_correct": True,
            "outcome.answer_chars": 8,
        }
        assert result.aggregate.metrics["outcome.final_correct"].rate == 1.0
        assert result.aggregate.metrics["outcome.answer_chars"].mean == 8.0

    def test_invalid_metric_in_outcome_fn_fails_the_layer_with_a_diagnostic(self) -> None:
        scenario = Scenario(
            name="bad_metric",
            prompt="q",
            outcome_fn=lambda _t: LayerResult(True, "ok", metrics={"ratio": math.nan}),
        )
        result = run_scenario(scenario, InMemoryRuntime(scripted_responses=["ok"]))
        outcome = result.runs[0].score.outcome
        assert outcome.passed is False
        assert "outcome_fn error" in outcome.detail
        assert "finite" in outcome.detail
