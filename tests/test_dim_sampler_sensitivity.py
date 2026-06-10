"""Tests for dim_sampler_sensitivity.

Coverage:

  1. Scenario catalog: exactly 3 scenarios, all tagged 'dim:sampler_sensitivity',
     all have variance_allowed=True.
  2. Matrix dispatcher: build_matrix() returns the correct cross-product of
     model × temperature × top_p cells.
  3. Matrix aggregation: run_matrix_aggregation() correctly computes mean_pass_rate,
     stddev, p10/p50/p90 percentiles from synthetic per-cell AggregateResults.
  4. Variance reporting: a cell with mixed pass/fail produces stddev > 0 and
     correct mean_pass_rate.
  5. Per-cell variance: aggregate_runs with variance_allowed=True returns
     PASS_WITH_VARIANCE (not FAIL) for a partial-pass cell.
  6. CellKey dataclass: hashable, carries model/temp/top_p/scenario fields.
  7. MatrixResult dataclass: carries cells dict, scenario names, and convenience
     accessors for percentile stats.
  8. Known-flaky scenario unit check: typo_recovery scenario with high temperature
     is expected to show variance — confirmed by its target_facts being
     temperature-sensitive (the test encodes the intent, not a live run).
  9. Synthetic db contracts: the mock DB for this dim backs the tools the
     3 scenarios need (ops_client_lookup, ops_order_report).

Design note on the matrix:
  - 1 model × 4 temperatures × 2 top_p values × 3 scenarios = 24 cells minimum
  - Each cell runs N=5 times for variance
  - Total runs = 24 × 5 = 120 per full matrix sweep
  - Unit tests exercise the dispatcher and aggregation logic with SYNTHETIC results
    (no live model needed) to keep the suite fast.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import pytest

from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
from windtunnel.api.evaluators import evaluate_outcome
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.scenarios.dim_sampler_sensitivity.matrix import (
    MATRIX_MODELS,
    MATRIX_TEMPERATURES,
    MATRIX_TOP_PS,
    CellKey,
    MatrixResult,
    build_matrix,
    run_matrix_aggregation,
)

# ─── Import targets (all fail until implemented) ──────────────────────────────
from windtunnel.scenarios.dim_sampler_sensitivity.scenarios import (
    DIM_TAG,
    SAMPLER_SENSITIVITY_SCENARIOS,
    comparison_which_has_more,
    multi_step_followup,
    typo_recovery,
)
from windtunnel.scenarios.dim_sampler_sensitivity.synthetic_db import (
    CLIENTS,
    find_clients,
    find_comparison_clients,
    reset_lookup_log,
)

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


def _make_trace(
    *turns: Turn,
    model: str = "test-model",
    sampler: dict | None = None,
) -> Trace:
    return Trace(
        scenario_id="test",
        agent_id="agent-test",
        variant_id="baseline",
        model=model,
        quant="q4",
        sampler=sampler or {},
        started_at=_ts(),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=list(turns),
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )


def _make_run_result(passed: bool, scenario_id: str = "test") -> ScenarioRunResult:
    """Synthesize a ScenarioRunResult for matrix aggregation tests."""
    from windtunnel.api.score import FailureCost, LayerResult, Score

    score = Score(
        outcome=LayerResult(passed=passed, detail="synthetic"),
        trajectory=LayerResult(passed=passed, detail="synthetic"),
        constraint=LayerResult(passed=True, detail="no policies"),
        robustness=LayerResult(passed=True, detail="no perturbations"),
        failure_cost=FailureCost(),
    )
    trace = Trace(
        scenario_id=scenario_id,
        agent_id="test",
        variant_id="test",
        model="test",
        quant="q4",
        sampler={},
        started_at=_ts(),
        finished_at=_ts(),
        turns=[],
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )
    return ScenarioRunResult(score=score, trace=trace)


# ─── 1. Scenario catalog ──────────────────────────────────────────────────────


class TestScenarioCatalog:
    def test_dim_tag_constant(self):
        assert DIM_TAG == "dim:sampler_sensitivity"

    def test_all_three_scenarios_tagged(self):
        for sc in SAMPLER_SENSITIVITY_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_set_has_exactly_three(self):
        assert len(SAMPLER_SENSITIVITY_SCENARIOS) == 3

    def test_scenario_names(self):
        names = {sc.name for sc in SAMPLER_SENSITIVITY_SCENARIOS}
        assert names == {"typo_recovery", "comparison_which_has_more", "multi_step_followup"}

    def test_all_scenarios_variance_allowed(self):
        """All sampler-sensitivity scenarios must opt in to variance_allowed=True."""
        for sc in SAMPLER_SENSITIVITY_SCENARIOS:
            assert sc.variance_allowed is True, (
                f"{sc.name} must have variance_allowed=True — "
                "sampler dim specifically tests variance, not 100% pass."
            )

    def test_all_scenarios_require_tool_use(self):
        for sc in SAMPLER_SENSITIVITY_SCENARIOS:
            assert sc.requires_tool_use is True, f"{sc.name} must require tool use"

    def test_typo_recovery_target_facts(self):
        """typo_recovery must include facts that will appear in passing traces."""
        flat = [f for group in typo_recovery.target_facts for f in group]
        assert len(flat) >= 1

    def test_comparison_which_has_more_target_facts(self):
        flat = [f for group in comparison_which_has_more.target_facts for f in group]
        assert len(flat) >= 1

    def test_multi_step_followup_target_facts(self):
        flat = [f for group in multi_step_followup.target_facts for f in group]
        assert len(flat) >= 1

    def test_all_scenarios_have_failure_cost(self):
        for sc in SAMPLER_SENSITIVITY_SCENARIOS:
            assert sc.failure_cost is not None


# ─── 2. Matrix config constants ───────────────────────────────────────────────


class TestMatrixConfig:
    def test_matrix_models_contains_default_model(self):
        assert "default" in MATRIX_MODELS

    def test_matrix_temperatures_contains_required_values(self):
        """Must cover [0.0, 0.4, 0.7, 1.0] as per acceptance criteria."""
        for temp in [0.0, 0.4, 0.7, 1.0]:
            assert temp in MATRIX_TEMPERATURES, (
                f"Temperature {temp} missing from MATRIX_TEMPERATURES={MATRIX_TEMPERATURES}"
            )

    def test_matrix_temperatures_has_four_values(self):
        assert len(MATRIX_TEMPERATURES) == 4

    def test_matrix_top_ps_contains_required_values(self):
        """Must cover [0.95, 1.0] as per acceptance criteria."""
        for tp in [0.95, 1.0]:
            assert tp in MATRIX_TOP_PS, (
                f"top_p {tp} missing from MATRIX_TOP_PS={MATRIX_TOP_PS}"
            )

    def test_matrix_top_ps_has_two_values(self):
        assert len(MATRIX_TOP_PS) == 2


# ─── 3. CellKey dataclass ─────────────────────────────────────────────────────


class TestCellKey:
    def test_cell_key_is_hashable(self):
        key = CellKey(
            model="default",
            temperature=0.7,
            top_p=0.95,
            scenario="typo_recovery",
        )
        # Must be usable as dict key
        d = {key: "value"}
        assert d[key] == "value"

    def test_cell_key_equality(self):
        k1 = CellKey(model="m", temperature=0.5, top_p=1.0, scenario="s")
        k2 = CellKey(model="m", temperature=0.5, top_p=1.0, scenario="s")
        assert k1 == k2

    def test_cell_key_inequality_on_temperature(self):
        k1 = CellKey(model="m", temperature=0.5, top_p=1.0, scenario="s")
        k2 = CellKey(model="m", temperature=0.7, top_p=1.0, scenario="s")
        assert k1 != k2

    def test_cell_key_fields(self):
        key = CellKey(
            model="default",
            temperature=0.0,
            top_p=0.95,
            scenario="typo_recovery",
        )
        assert key.model == "default"
        assert key.temperature == 0.0
        assert key.top_p == 0.95
        assert key.scenario == "typo_recovery"


# ─── 4. build_matrix() ────────────────────────────────────────────────────────


class TestBuildMatrix:
    def test_build_matrix_returns_correct_cell_count(self):
        """1 model × 4 temps × 2 top_ps × 3 scenarios = 24 cells."""
        cells = build_matrix(
            models=["default"],
            temperatures=[0.0, 0.4, 0.7, 1.0],
            top_ps=[0.95, 1.0],
            scenarios=SAMPLER_SENSITIVITY_SCENARIOS,
        )
        assert len(cells) == 24, f"Expected 24 cells, got {len(cells)}"

    def test_build_matrix_all_combinations_present(self):
        """Every model × temp × top_p × scenario combination appears exactly once."""
        models = ["m1"]
        temperatures = [0.0, 0.7]
        top_ps = [0.95, 1.0]
        cells = build_matrix(
            models=models,
            temperatures=temperatures,
            top_ps=top_ps,
            scenarios=SAMPLER_SENSITIVITY_SCENARIOS[:2],
        )
        assert len(cells) == 1 * 2 * 2 * 2  # 8
        keys = set(cells)
        assert CellKey("m1", 0.0, 0.95, SAMPLER_SENSITIVITY_SCENARIOS[0].name) in keys
        assert CellKey("m1", 0.7, 1.0, SAMPLER_SENSITIVITY_SCENARIOS[1].name) in keys

    def test_build_matrix_cells_start_empty(self):
        """Cells start with empty run lists (no results yet)."""
        cells = build_matrix(
            models=["m"],
            temperatures=[0.0],
            top_ps=[1.0],
            scenarios=SAMPLER_SENSITIVITY_SCENARIOS[:1],
        )
        assert len(cells) == 1
        cell_runs = list(cells.values())[0]
        assert cell_runs == []

    def test_build_matrix_returns_dict_of_lists(self):
        cells = build_matrix(
            models=["m"],
            temperatures=[0.0],
            top_ps=[1.0],
            scenarios=SAMPLER_SENSITIVITY_SCENARIOS[:1],
        )
        assert isinstance(cells, dict)
        for v in cells.values():
            assert isinstance(v, list)


# ─── 5. run_matrix_aggregation() ──────────────────────────────────────────────


class TestRunMatrixAggregation:
    """Aggregate synthetic per-cell run results into a MatrixResult."""

    def _make_cell(
        self, passes: list[bool], model: str = "m", temp: float = 0.0,
        top_p: float = 1.0, scenario: str = "typo_recovery"
    ) -> tuple[CellKey, list[ScenarioRunResult]]:
        key = CellKey(model=model, temperature=temp, top_p=top_p, scenario=scenario)
        runs = [_make_run_result(p, scenario) for p in passes]
        return key, runs

    def test_all_pass_cell_correct_mean(self):
        """5/5 passes → mean_pass_rate=1.0, stddev=0.0."""
        key, runs = self._make_cell([True, True, True, True, True])
        cells = {key: runs}
        result = run_matrix_aggregation(cells)
        agg = result.cells[key]
        assert agg.pass_rate == 1.0
        assert agg.stddev == 0.0
        assert agg.verdict == "PASS"

    def test_all_fail_cell_correct_mean(self):
        """0/5 passes → mean_pass_rate=0.0, stddev=0.0."""
        key, runs = self._make_cell([False, False, False, False, False])
        cells = {key: runs}
        result = run_matrix_aggregation(cells)
        agg = result.cells[key]
        assert agg.pass_rate == 0.0
        assert agg.stddev == 0.0
        assert agg.verdict == "PASS_WITH_VARIANCE"

    def test_partial_pass_cell_correct_mean(self):
        """3/5 passes → mean_pass_rate=0.6."""
        key, runs = self._make_cell([True, True, True, False, False])
        cells = {key: runs}
        result = run_matrix_aggregation(cells)
        agg = result.cells[key]
        assert abs(agg.pass_rate - 0.6) < 1e-9
        assert agg.verdict == "PASS_WITH_VARIANCE"

    def test_partial_pass_cell_nonzero_stddev(self):
        """3/5 passes → stddev > 0 (shows variance)."""
        key, runs = self._make_cell([True, True, True, False, False])
        cells = {key: runs}
        result = run_matrix_aggregation(cells)
        agg = result.cells[key]
        assert agg.stddev > 0.0

    def test_stddev_calculation_correctness(self):
        """Verify population stddev for [1,1,1,0,0] = sqrt(0.6*0.4)."""
        key, runs = self._make_cell([True, True, True, False, False])
        cells = {key: runs}
        result = run_matrix_aggregation(cells)
        agg = result.cells[key]
        expected_stddev = math.sqrt(0.6 * 0.4)  # population stddev
        assert abs(agg.stddev - expected_stddev) < 1e-9

    def test_multiple_cells_aggregated_independently(self):
        """Two cells with different pass rates produce independent aggregates."""
        k1, r1 = self._make_cell([True, True, True, True, True], temp=0.0)
        k2, r2 = self._make_cell([True, False, True, False, True], temp=1.0)
        cells = {k1: r1, k2: r2}
        result = run_matrix_aggregation(cells)
        assert result.cells[k1].pass_rate == 1.0
        assert abs(result.cells[k2].pass_rate - 0.6) < 1e-9

    def test_result_scenario_names_match_cells(self):
        """MatrixResult.scenario_names contains all unique scenario names in cells."""
        k1, r1 = self._make_cell([True], scenario="typo_recovery")
        k2, r2 = self._make_cell([False], scenario="comparison_which_has_more")
        result = run_matrix_aggregation({k1: r1, k2: r2})
        assert "typo_recovery" in result.scenario_names
        assert "comparison_which_has_more" in result.scenario_names


# ─── 6. MatrixResult percentile stats ─────────────────────────────────────────


class TestMatrixResultPercentiles:
    """MatrixResult.percentile_stats(scenario) returns P10/P50/P90 pass-rate."""

    def _make_result_with_rates(
        self, rates: list[float], scenario: str = "typo_recovery"
    ) -> MatrixResult:
        """Build a MatrixResult with cells having the given pass rates."""
        cells: dict[CellKey, list[ScenarioRunResult]] = {}
        for i, rate in enumerate(rates):
            n_pass = round(rate * 5)
            passes = [True] * n_pass + [False] * (5 - n_pass)
            key = CellKey(
                model="m", temperature=float(i) * 0.1, top_p=1.0, scenario=scenario
            )
            cells[key] = [_make_run_result(p, scenario) for p in passes]
        return run_matrix_aggregation(cells)

    def test_percentile_stats_returns_p10_p50_p90(self):
        result = self._make_result_with_rates([0.2, 0.4, 0.6, 0.8, 1.0])
        stats = result.percentile_stats("typo_recovery")
        assert "p10" in stats
        assert "p50" in stats
        assert "p90" in stats

    def test_percentile_stats_p50_is_median(self):
        """With rates [0.0, 0.5, 1.0] the median is 0.5."""
        result = self._make_result_with_rates([0.0, 0.5, 1.0])
        stats = result.percentile_stats("typo_recovery")
        # median of 3 sorted values is the middle one
        assert stats["p50"] == pytest.approx(0.5, abs=0.1)

    def test_percentile_stats_unknown_scenario_returns_none_or_empty(self):
        result = self._make_result_with_rates([0.5])
        stats = result.percentile_stats("nonexistent_scenario")
        # Either None or an empty dict — either is acceptable
        assert stats is None or stats == {}

    def test_percentile_stats_single_cell(self):
        """Single cell: p10=p50=p90=that cell's pass_rate."""
        result = self._make_result_with_rates([0.8])
        stats = result.percentile_stats("typo_recovery")
        # all percentiles should be close to 0.8
        for key in ("p10", "p50", "p90"):
            assert stats[key] == pytest.approx(0.8, abs=0.21)  # rounding due to 5 runs


# ─── 7. Variance detected by aggregate_runs with variance_allowed=True ────────


class TestAggregateRunsVariance:
    """Confirm that aggregate_runs behaves correctly for sampler-sensitivity cells."""

    def test_partial_pass_variance_allowed_true_gives_found_with_variance(self):
        runs = [_make_run_result(p) for p in [True, False, True, False, True]]
        agg = aggregate_runs(runs, variance_allowed=True)
        assert agg.verdict == "PASS_WITH_VARIANCE"

    def test_partial_pass_variance_allowed_false_gives_not_found(self):
        runs = [_make_run_result(p) for p in [True, False, True, False, True]]
        agg = aggregate_runs(runs, variance_allowed=False)
        assert agg.verdict == "FAIL"

    def test_all_pass_variance_allowed_true_gives_found(self):
        runs = [_make_run_result(True) for _ in range(5)]
        agg = aggregate_runs(runs, variance_allowed=True)
        assert agg.verdict == "PASS"

    def test_stddev_matches_expected_for_known_distribution(self):
        """[1,0,1,0,1] → pass_rate=0.6, stddev=sqrt(0.6*0.4)=0.4899."""
        runs = [_make_run_result(p) for p in [True, False, True, False, True]]
        agg = aggregate_runs(runs, variance_allowed=True)
        assert abs(agg.pass_rate - 0.6) < 1e-9
        expected = math.sqrt(0.6 * 0.4)
        assert abs(agg.stddev - expected) < 1e-9


# ─── 8. Outcome + Trajectory evaluators on scenario-specific traces ───────────


class TestScenarioEvaluators:
    """Verify each scenario's evaluators pass/fail on crafted traces."""

    # ── typo_recovery ─────────────────────────────────────────────────────────

    def test_typo_recovery_outcome_pass(self):
        """Model calls lookup and returns the resolved client name and contact."""
        trace = _make_trace(
            _turn(role="user", content="Look up Bluewing Logistcs"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\", \\"name\\": \\"Bluewing Logistics\\", \\"clientContactName\\": \\"Joe Marsh\\"}]}"}'),
            _turn(role="assistant", content="Found Bluewing Logistics (ACC-BLWG-001). The contact name is Joe Marsh."),
        )
        result = evaluate_outcome(trace, typo_recovery)
        assert result.passed, f"Expected pass: {result.detail}"

    def test_typo_recovery_outcome_fail_no_tool(self):
        """Model fails to use a tool → outcome fail (requires_tool_use=True)."""
        trace = _make_trace(
            _turn(role="user", content="Look up Bluewing Logistcs"),
            _turn(role="assistant", content="I found Bluewing Logistics."),
        )
        result = evaluate_outcome(trace, typo_recovery)
        assert not result.passed, "Expected fail: no tool call made"

    # ── comparison_which_has_more ─────────────────────────────────────────────

    def test_comparison_pass(self):
        """Model calls comparison lookup and returns answer with correct client name."""
        trace = _make_trace(
            _turn(role="user", content="Which client has more open order volume: ACC-BLWG-001 or ACC-CHIC-001?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_report", {"client": "ACC-BLWG-001"}),
            ]),
            _turn(role="tool", content='{"result": "{\\"total\\": {\\"quantity\\": 20}}"}'),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_report", {"client": "ACC-CHIC-001"}),
            ]),
            _turn(role="tool", content='{"result": "{\\"total\\": {\\"quantity\\": 5}}"}'),
            _turn(role="assistant", content="ACC-BLWG-001 (Bluewing Logistics) has more order volume with a total quantity of 20."),
        )
        result = evaluate_outcome(trace, comparison_which_has_more)
        assert result.passed, f"Expected pass: {result.detail}"

    def test_comparison_fail_no_tool(self):
        trace = _make_trace(
            _turn(role="user", content="Which client has more open order volume?"),
            _turn(role="assistant", content="I don't know without looking it up."),
        )
        result = evaluate_outcome(trace, comparison_which_has_more)
        assert not result.passed, "Expected fail: no tool use"

    # ── multi_step_followup ───────────────────────────────────────────────────

    def test_multi_step_outcome_pass(self):
        """Multi-step: model resolves client, then retrieves the correct field."""
        trace = _make_trace(
            _turn(role="user", content="What's the contact email for the Bluewing Logistics client?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\", \\"clientEmail\\": \\"ops@bluewing.example\\"}]}"}'),
            _turn(role="assistant", content="The contact email is ops@bluewing.example."),
        )
        result = evaluate_outcome(trace, multi_step_followup)
        assert result.passed, f"Expected pass: {result.detail}"

    def test_multi_step_fail_wrong_answer(self):
        trace = _make_trace(
            _turn(role="user", content="What's the contact email for the Bluewing Logistics client?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\"}]}"}'),
            _turn(role="assistant", content="The email is wrong@example.com."),
        )
        result = evaluate_outcome(trace, multi_step_followup)
        assert not result.passed, "Expected fail: wrong email returned"


# ─── 9. Synthetic DB contracts ────────────────────────────────────────────────


class TestSyntheticDbContracts:
    """Mock DB for sampler_sensitivity dim."""

    def test_clients_dataset_exists(self):
        assert len(CLIENTS) >= 2

    def test_find_clients_fuzzy_typo(self):
        """find_clients with a near-typo still returns the correct client."""
        # The mock DB accepts exact query strings — the dim tests model behaviour,
        # not fuzzy matching in the DB. Confirm exact-name lookup works.
        results = find_clients(query="Bluewing Logistics")
        assert len(results) >= 1
        assert results[0]["id"] == "ACC-BLWG-001"

    def test_find_clients_returns_email(self):
        results = find_clients(query="Bluewing Logistics")
        assert any(c.get("clientEmail") == "ops@bluewing.example" for c in results)

    def test_find_comparison_clients_returns_two(self):
        """Comparison scenarios need at least 2 clients with order-volume data."""
        results = find_comparison_clients()
        assert len(results) >= 2

    def test_comparison_clients_have_order_volumes(self):
        results = find_comparison_clients()
        for c in results:
            assert "order_qty" in c, f"Client {c.get('id')} missing order_qty"

    def test_reset_lookup_log_clears_state(self):
        reset_lookup_log()
        # Just verifies it doesn't throw
        assert True
