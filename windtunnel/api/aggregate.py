"""Per-run aggregation — declared gates, integrity, variance, and risk.

Per-run-must-pass semantics:
  When running a scenario N times, ALL N runs must satisfy every selected
  gate layer. A single miss → FAIL unless variance_allowed=True. A run whose
  experiment-integrity check fails makes the aggregate INVALID instead.

variance_allowed=True semantics:
  The deploy gate still reports the pass_rate ± stddev but
  does not treat a sub-100% run as a regression. Verdict becomes
  PASS_WITH_VARIANCE so reports can distinguish "always passes" from
  "usually passes".

Per-layer pass rates (outcome_pass_rate, trajectory_pass_rate, etc.)
are always computed regardless of variance_allowed so reports
can show which layer is the weakest link.

Stddev uses population stddev (divide by N, not N-1) — consistent with
τ-bench's pass^k metric framing. With small N the sample stddev would
overestimate variance; population stddev is the right denominator when N
is the full set of runs we actually ran.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from windtunnel.api.score import GATE_LAYER_ORDER, GateLayer, Score
from windtunnel.api.trace import Trace


@dataclass
class ScenarioRunResult:
    """The outcome of one scenario run: its Score and the Trace it scored."""
    score: Score
    trace: Trace
    hook_artifacts: list[object] = field(default_factory=list, repr=False, compare=False)


@dataclass
class AggregateResult:
    """Aggregated result across N runs of one scenario.

    verdict:
      PASS               — 100% pass rate (or N=1 pass)
      FAIL               — <100% pass rate, variance_allowed=False
      PASS_WITH_VARIANCE — <100% pass rate, variance_allowed=True
      INVALID            — no runs, or at least one run has invalid test setup
      PARTIAL            — alias for PASS_WITH_VARIANCE (never used internally,
                           kept for backward compat with callers that check for it)
    """
    verdict: str           # PASS | FAIL | PASS_WITH_VARIANCE | INVALID
    passed: int            # number of valid runs that satisfied every gate
    total: int             # total runs
    pass_rate: float       # passed / total
    stddev: float          # population stddev of per-run pass/fail (0 or 1)

    # Per-layer pass rates
    outcome_pass_rate: float
    trajectory_pass_rate: float
    constraint_pass_rate: float
    integrity_pass_rate: float

    # Gate/risk metadata
    gate_layers: tuple[GateLayer, ...] = GATE_LAYER_ORDER
    risk_weight: int = 0
    failure_risk: float = 0.0

    @property
    def robustness_pass_rate(self) -> float:
        """Compatibility alias for the old perturbation-marker pass rate."""
        return self.integrity_pass_rate


def aggregate_runs(
    runs: list[ScenarioRunResult],
    variance_allowed: bool = False,
    gate_layers: Sequence[GateLayer] = GATE_LAYER_ORDER,
) -> AggregateResult:
    """Aggregate N ScenarioRunResults into a single AggregateResult.

    Args:
        runs:             list of ScenarioRunResult, one per run.
        variance_allowed: if True, sub-100% gate pass rate is
                          PASS_WITH_VARIANCE rather than FAIL.
        gate_layers:      agent-behavior layers that determine each run's gate.
                          Defaults to every layer (outcome, trajectory,
                          constraint) — strict by default. A layer no scenario
                          configured a check for always scores passed=True, so
                          including it here costs nothing; a caller with a
                          real scenario should still pass
                          scenario.resolved_gate_layers() so an explicit,
                          narrower selection is honored. Integrity is always
                          required separately, regardless of this argument.

    Returns:
        AggregateResult with verdict, counts, pass_rate, stddev, and
        per-layer pass rates.
    """
    selected_gates = tuple(gate_layers)
    invalid_gates = [layer for layer in selected_gates if layer not in GATE_LAYER_ORDER]
    if invalid_gates:
        raise ValueError(f"unknown gate layers: {invalid_gates}")
    if len(set(selected_gates)) != len(selected_gates):
        raise ValueError("gate_layers must not contain duplicates")

    if not runs:
        return AggregateResult(
            verdict="INVALID",
            passed=0,
            total=0,
            pass_rate=0.0,
            stddev=0.0,
            outcome_pass_rate=0.0,
            trajectory_pass_rate=0.0,
            constraint_pass_rate=0.0,
            integrity_pass_rate=0.0,
            gate_layers=selected_gates,
        )

    n = len(runs)

    # Per-layer and full-gate booleans.
    outcome_bits = [1 if r.score.outcome.passed else 0 for r in runs]
    trajectory_bits = [1 if r.score.trajectory.passed else 0 for r in runs]
    constraint_bits = [1 if r.score.constraint.passed else 0 for r in runs]
    integrity_bits = [1 if r.score.integrity.passed else 0 for r in runs]
    gate_bits = [1 if r.score.gate_passed(selected_gates) else 0 for r in runs]

    passed = sum(gate_bits)
    pass_rate = passed / n

    # Population stddev of the full per-run gate result.
    mean = pass_rate
    variance = sum((x - mean) ** 2 for x in gate_bits) / n
    stddev = math.sqrt(variance)

    # Per-layer rates
    outcome_pass_rate = sum(outcome_bits) / n
    trajectory_pass_rate = sum(trajectory_bits) / n
    constraint_pass_rate = sum(constraint_bits) / n
    integrity_pass_rate = sum(integrity_bits) / n

    risk_weight = max(run.score.failure_cost.risk_weight for run in runs)

    # Invalid test conditions are not evidence of agent failure or success.
    if integrity_pass_rate < 1.0:
        verdict = "INVALID"
    elif passed == n:
        verdict = "PASS"
    elif variance_allowed:
        verdict = "PASS_WITH_VARIANCE"
    else:
        verdict = "FAIL"

    failure_risk = 0.0 if verdict == "INVALID" else risk_weight * (1.0 - pass_rate)

    return AggregateResult(
        verdict=verdict,
        passed=passed,
        total=n,
        pass_rate=pass_rate,
        stddev=stddev,
        outcome_pass_rate=outcome_pass_rate,
        trajectory_pass_rate=trajectory_pass_rate,
        constraint_pass_rate=constraint_pass_rate,
        integrity_pass_rate=integrity_pass_rate,
        gate_layers=selected_gates,
        risk_weight=risk_weight,
        failure_risk=failure_risk,
    )
