"""A gated outcome layer must never pass vacuously on an empty answer turn.

The failure shape: a scenario that declares no target facts, scored against
a run whose final assistant turn is empty (e.g. the runtime failed and
produced nothing). Fact matching finds nothing to check and used to report
"all facts and numbers found" — a PASS earned by silence.

Covers:
  1. Regression: empty/whitespace answer + no declared facts + gated
     outcome = FAIL with an honest detail
  2. Opt-in: allow_empty_answer=True restores the pass, explicitly
  3. Non-gated outcome keeps the legacy vacuous pass (diagnostic-only
     layers never flip verdicts)
  4. Unchanged neighbors: non-empty answers, declared-fact misses
  5. Turn.error — the generic runtime-failure marker: round-trip through
     save/load, runner threading from the response shape, and INVALID (not
     FAIL, not vacuous PASS) when the scored turn carries it
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from windtunnel.api.evaluators import evaluate_integrity, evaluate_outcome
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, load_trace, save_trace


def _trace(turns: list[Turn]) -> Trace:
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    return Trace(
        scenario_id="empty_answer_case",
        agent_id="wt-cli",
        variant_id="candidate",
        model="model-x",
        quant="q4",
        sampler={},
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        turns=turns,
        tool_schema_hash=None,
    )


def _turn(role: str, content: str, *, error: str | None = None) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=[],
        tool_results=[],
        latency_ms=1.0,
        error=error,
    )


class TestVacuousPassRegression:
    def test_empty_answer_with_no_facts_fails_gated_outcome(self) -> None:
        scenario = Scenario(name="empty_answer_case", prompt="do the task")
        result = evaluate_outcome(_trace([_turn("user", "go"), _turn("assistant", "")]), scenario)
        assert result.passed is False
        assert "answer turn is empty" in result.detail
        assert "allow_empty_answer" in result.detail

    def test_whitespace_answer_fails_the_same_way(self) -> None:
        scenario = Scenario(name="empty_answer_case", prompt="do the task")
        result = evaluate_outcome(_trace([_turn("assistant", "  \n\t ")]), scenario)
        assert result.passed is False
        assert "answer turn is empty" in result.detail

    def test_opt_in_allows_silence(self) -> None:
        scenario = Scenario(
            name="empty_answer_case", prompt="do the task", allow_empty_answer=True
        )
        result = evaluate_outcome(_trace([_turn("assistant", "")]), scenario)
        assert result.passed is True
        assert "allow_empty_answer" in result.detail

    def test_non_gated_outcome_keeps_the_legacy_vacuous_pass(self) -> None:
        # The only way outcome leaves the gate: explicit gate_layers with
        # strict_gates=False. A diagnostic-only outcome never flips a
        # verdict, so the guard stays out of its way.
        scenario = Scenario(
            name="empty_answer_case",
            prompt="do the task",
            gate_layers=[],
            strict_gates=False,
        )
        assert "outcome" not in scenario.resolved_gate_layers()
        result = evaluate_outcome(_trace([_turn("assistant", "")]), scenario)
        assert result.passed is True

    def test_non_empty_answer_with_no_facts_still_passes(self) -> None:
        scenario = Scenario(name="empty_answer_case", prompt="do the task")
        result = evaluate_outcome(_trace([_turn("assistant", "done, as asked")]), scenario)
        assert result.passed is True

    def test_declared_fact_miss_keeps_its_diagnostic_detail(self) -> None:
        scenario = Scenario(
            name="empty_answer_case", prompt="do the task", target_facts=[["ok"]]
        )
        result = evaluate_outcome(_trace([_turn("assistant", "")]), scenario)
        assert result.passed is False
        assert "missing fact groups" in result.detail


class TestTurnErrorSignal:
    def test_round_trip_preserves_error(self, tmp_path: Path) -> None:
        trace = _trace([_turn("assistant", "", error="inference worker timed out")])
        path = tmp_path / "run.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.turns[0].error == "inference worker timed out"

    def test_absent_error_defaults_none_for_legacy_turns(self, tmp_path: Path) -> None:
        trace = _trace([_turn("assistant", "ok")])
        path = tmp_path / "run.json"
        save_trace(trace, path)
        # A legacy trace has no "error" key at all — simulate by stripping it.
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        for turn in data["turns"]:
            turn.pop("error", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        assert load_trace(path).turns[0].error is None

    def test_errored_scored_turn_is_invalid_not_a_vacuous_pass(self) -> None:
        scenario = Scenario(name="empty_answer_case", prompt="do the task")
        trace = _trace([
            _turn("user", "go"),
            _turn("assistant", "", error="gateway returned an empty completion"),
        ])
        integrity = evaluate_integrity(trace, scenario)
        assert integrity.passed is False
        assert "runtime error" in integrity.detail
        # INVALID beats FAIL: the aggregate verdict for such a run.
        from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
        from windtunnel.api.evaluators import (
            evaluate_constraint,
            evaluate_trajectory,
        )
        from windtunnel.api.score import Score

        score = Score(
            outcome=evaluate_outcome(trace, scenario),
            trajectory=evaluate_trajectory(trace, scenario),
            constraint=evaluate_constraint(trace, scenario),
            integrity=integrity,
        )
        aggregate = aggregate_runs(
            [ScenarioRunResult(score=score, trace=trace)],
            gate_layers=scenario.resolved_gate_layers(),
        )
        assert aggregate.verdict == "INVALID"

    def test_recovered_intermediate_error_stays_scoreable(self) -> None:
        scenario = Scenario(
            name="empty_answer_case", prompt="do the task", target_facts=[["ok"]]
        )
        trace = _trace([
            _turn("user", "go"),
            _turn("assistant", "", error="transient failure"),
            _turn("user", "try again"),
            _turn("assistant", "ok"),
        ])
        assert evaluate_integrity(trace, scenario).passed is True
        assert evaluate_outcome(trace, scenario).passed is True

    def test_runner_threads_the_error_from_the_response(self) -> None:
        """End to end through run_scenario: a scripted errored turn lands on
        Turn.error and the aggregate is INVALID."""
        from windtunnel.api.runner import run_scenario
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        scenario = Scenario(name="empty_answer_case", prompt="do the task")
        runtime = InMemoryRuntime(
            scripted_responses=[{"content": "", "error": "inference worker timed out"}]
        )
        result = run_scenario(scenario, runtime)
        trace = result.runs[0].trace
        assert trace.turns[-1].error == "inference worker timed out"
        assert result.aggregate.verdict == "INVALID"

    def test_error_free_pipeline_is_unchanged(self) -> None:
        from windtunnel.api.runner import run_scenario
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        scenario = Scenario(
            name="empty_answer_case", prompt="say ok", target_facts=[["ok"]]
        )
        result = run_scenario(scenario, InMemoryRuntime(scripted_responses=["ok"]))
        assert result.aggregate.verdict == "PASS"
        assert result.runs[0].trace.turns[-1].error is None
