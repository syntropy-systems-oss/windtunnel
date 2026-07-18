"""Matrix dispatcher + aggregation for dim_sampler_sensitivity.

Sweeps a cross-product of model × temperature × top_p × scenario, running
each cell N times to measure variance. Per-cell aggregation uses
aggregate_runs(variance_allowed=True) which produces PASS_WITH_VARIANCE for
partial-pass cells and reports mean ± stddev rather than per-run-must-pass.

Default matrix:
    models:       ["default"]                        # 1 model
    temperatures: [0.0, 0.4, 0.7, 1.0]              # 4 cells
    top_ps:       [0.95, 1.0]                        # 2 cells
    scenarios:    3 (typo_recovery, comparison, multi_step)
    N per cell:   5 (default)
    Total runs:   1 × 4 × 2 × 3 × 5 = 120
"""
from __future__ import annotations

from dataclasses import dataclass

from windtunnel.api.aggregate import AggregateResult, ScenarioRunResult, aggregate_runs
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import GATE_LAYER_ORDER

# ─── Matrix configuration ─────────────────────────────────────────────────────

# Replace with the model id(s) your runtime serves; single-model keeps the
# sweep bounded.
MATRIX_MODELS: list[str] = ["default"]

# Four temperature points: greedy, low, medium, high.
MATRIX_TEMPERATURES: list[float] = [0.0, 0.4, 0.7, 1.0]

# Two top_p points: default (0.95) and no nucleus filtering (1.0).
MATRIX_TOP_PS: list[float] = [0.95, 1.0]


# ─── CellKey — hashable cross-product key ─────────────────────────────────────

@dataclass(frozen=True)
class CellKey:
    """Identifies one cell in the model × temperature × top_p × scenario matrix.

    frozen=True makes it hashable so it can be used as a dict key.
    """
    model: str
    temperature: float
    top_p: float
    scenario: str


# ─── MatrixResult ─────────────────────────────────────────────────────────────

@dataclass
class MatrixResult:
    """Aggregated results for the full model × temp × top_p × scenario matrix.

    cells: mapping from CellKey to AggregateResult for that cell's N runs.
    scenario_names: unique scenario names present in cells.
    """
    cells: dict[CellKey, AggregateResult]
    scenario_names: list[str]

    def percentile_stats(self, scenario: str) -> dict[str, float] | None:
        """Return P10/P50/P90 pass-rate across all cells for the given scenario.

        Uses linear interpolation on the sorted pass-rate list. Returns None if
        the scenario has no cells in this result.
        """
        rates = sorted(
            agg.pass_rate
            for key, agg in self.cells.items()
            if key.scenario == scenario
        )
        if not rates:
            return None
        return {
            "p10": _percentile(rates, 10),
            "p50": _percentile(rates, 50),
            "p90": _percentile(rates, 90),
        }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over a pre-sorted list."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ─── Matrix builder ───────────────────────────────────────────────────────────

def build_matrix(
    models: list[str],
    temperatures: list[float],
    top_ps: list[float],
    scenarios: list[Scenario],
) -> dict[CellKey, list[ScenarioRunResult]]:
    """Return an empty cell dict covering the full cross-product.

    Each cell starts as an empty list — the runner fills it with N
    ScenarioRunResult objects during execution.

    Order is deterministic: models × temperatures × top_ps × scenarios
    so callers iterating the dict get a stable, readable sweep order.
    """
    cells: dict[CellKey, list[ScenarioRunResult]] = {}
    for model in models:
        for temp in temperatures:
            for top_p in top_ps:
                for scenario in scenarios:
                    key = CellKey(
                        model=model,
                        temperature=temp,
                        top_p=top_p,
                        scenario=scenario.name,
                    )
                    cells[key] = []
    return cells


# ─── Matrix aggregation ───────────────────────────────────────────────────────

def run_matrix_aggregation(
    cells: dict[CellKey, list[ScenarioRunResult]],
) -> MatrixResult:
    """Aggregate all cells into a MatrixResult.

    Each cell's run list is passed to aggregate_runs(variance_allowed=True)
    because all sampler-sensitivity scenarios opt in to variance tracking.
    Empty cells produce an INVALID aggregate with pass_rate=0 and stddev=0.
    """
    from windtunnel.scenarios.dim_sampler_sensitivity.scenarios import (
        SAMPLER_SENSITIVITY_SCENARIOS,
    )

    gates_by_scenario = {
        scenario.name: scenario.resolved_gate_layers()
        for scenario in SAMPLER_SENSITIVITY_SCENARIOS
    }
    aggregated: dict[CellKey, AggregateResult] = {}
    for key, runs in cells.items():
        aggregated[key] = aggregate_runs(
            runs,
            variance_allowed=True,
            gate_layers=gates_by_scenario.get(key.scenario, GATE_LAYER_ORDER),
        )

    scenario_names = sorted({key.scenario for key in cells})
    return MatrixResult(cells=aggregated, scenario_names=scenario_names)
