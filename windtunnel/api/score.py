"""Score, LayerResult, FailureCost — the four-layer scoring data types.

A score is a tuple, not a single number. Each layer is independently
pass/fail with a diagnostic detail string. A scenario can pass outcome
and fail trajectory and that distinction is visible in reports.

FailureCost is authored per-scenario and attached to Score for weighted
aggregation — a single critical/customer_visible/irreversible regression
outweighs ten low/internal/reversible ones.

Design:
- Pure dataclasses, stdlib only.
- Verdict enum kept simple: PASS/FAIL/SKIP. Aggregate verdict uses
  the same "PASS"/"FAIL" vocabulary so reports stay consistent.
- Severity is a Literal type for type-checker enforcement without
  requiring a dependency on typing_extensions (Literal is in stdlib
  from 3.8+).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class Verdict(Enum):
    """Per-layer pass/fail verdict."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


SeverityLevel = Literal["low", "medium", "high", "critical"]


@dataclass
class LayerResult:
    """Result for one scoring layer: pass/fail + human-readable detail."""
    passed: bool
    detail: str


@dataclass
class FailureCost:
    """Per-scenario cost annotation for weighted aggregation.

    Defaults to the safest/cheapest profile: low severity, internal,
    reversible, no side effect performed. Scenarios that can cause
    irreversible customer-visible damage must override these.
    """
    severity: SeverityLevel = "low"
    customer_visible: bool = False
    reversible: bool = True
    side_effect_performed: bool = False


@dataclass
class Score:
    """Four-layer score for one scenario run.

    Each layer is independently evaluated and independently pass/fail.
    The failure_cost is attached here so the aggregate report can weight
    regressions appropriately.
    """
    outcome: LayerResult
    trajectory: LayerResult
    constraint: LayerResult
    robustness: LayerResult
    failure_cost: FailureCost = field(default_factory=FailureCost)


def score_to_dict(score: Score) -> dict[str, Any]:
    """Serialize a Score to the flat dict shape consumed by report.load_runs().

    Top-level keys: outcome/trajectory/constraint/robustness (each
    {"passed", "detail"}) + failure_cost. This is the canonical
    `.score.json` sidecar layer shape — see windtunnel/report.py
    `_cell_from_run` for the reader.
    """
    return {
        "outcome": {"passed": score.outcome.passed, "detail": score.outcome.detail},
        "trajectory": {"passed": score.trajectory.passed, "detail": score.trajectory.detail},
        "constraint": {"passed": score.constraint.passed, "detail": score.constraint.detail},
        "robustness": {"passed": score.robustness.passed, "detail": score.robustness.detail},
        "failure_cost": {
            "severity": score.failure_cost.severity,
            "customer_visible": score.failure_cost.customer_visible,
            "reversible": score.failure_cost.reversible,
            "side_effect_performed": score.failure_cost.side_effect_performed,
        },
    }
