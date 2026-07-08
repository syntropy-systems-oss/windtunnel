"""Per-run aggregation — multi-run pass rates, stddev, and deploy-gate verdict.

Per-run-must-pass semantics:
  When running a scenario N times, ALL N runs must pass outcome for the
  scenario to count as PASS. A single miss → FAIL unless the
  scenario opts in via variance_allowed=True.

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
from dataclasses import dataclass, field

from windtunnel.api.score import Score
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
      PARTIAL            — alias for PASS_WITH_VARIANCE (never used internally,
                           kept for backward compat with callers that check for it)
    """
    verdict: str           # PASS | FAIL | PASS_WITH_VARIANCE
    passed: int            # number of runs that passed outcome layer
    total: int             # total runs
    pass_rate: float       # passed / total
    stddev: float          # population stddev of per-run pass/fail (0 or 1)

    # Per-layer pass rates
    outcome_pass_rate: float
    trajectory_pass_rate: float
    constraint_pass_rate: float
    robustness_pass_rate: float


def aggregate_runs(
    runs: list[ScenarioRunResult],
    variance_allowed: bool = False,
) -> AggregateResult:
    """Aggregate N ScenarioRunResults into a single AggregateResult.

    Args:
        runs:             list of ScenarioRunResult, one per run.
        variance_allowed: if True, sub-100% pass rate is PASS_WITH_VARIANCE
                          rather than FAIL.

    Returns:
        AggregateResult with verdict, counts, pass_rate, stddev, and
        per-layer pass rates.
    """
    if not runs:
        return AggregateResult(
            verdict="FAIL",
            passed=0,
            total=0,
            pass_rate=0.0,
            stddev=0.0,
            outcome_pass_rate=0.0,
            trajectory_pass_rate=0.0,
            constraint_pass_rate=0.0,
            robustness_pass_rate=0.0,
        )

    n = len(runs)

    # Per-run outcome booleans (the gate variable)
    outcome_bits = [1 if r.score.outcome.passed else 0 for r in runs]
    trajectory_bits = [1 if r.score.trajectory.passed else 0 for r in runs]
    constraint_bits = [1 if r.score.constraint.passed else 0 for r in runs]
    robustness_bits = [1 if r.score.robustness.passed else 0 for r in runs]

    passed = sum(outcome_bits)
    pass_rate = passed / n

    # Population stddev of per-run outcome bits
    mean = pass_rate
    variance = sum((x - mean) ** 2 for x in outcome_bits) / n
    stddev = math.sqrt(variance)

    # Per-layer rates
    outcome_pass_rate = pass_rate
    trajectory_pass_rate = sum(trajectory_bits) / n
    constraint_pass_rate = sum(constraint_bits) / n
    robustness_pass_rate = sum(robustness_bits) / n

    # Verdict: per-run-must-pass unless variance_allowed
    if passed == n:
        verdict = "PASS"
    elif variance_allowed:
        verdict = "PASS_WITH_VARIANCE"
    else:
        verdict = "FAIL"

    return AggregateResult(
        verdict=verdict,
        passed=passed,
        total=n,
        pass_rate=pass_rate,
        stddev=stddev,
        outcome_pass_rate=outcome_pass_rate,
        trajectory_pass_rate=trajectory_pass_rate,
        constraint_pass_rate=constraint_pass_rate,
        robustness_pass_rate=robustness_pass_rate,
    )
