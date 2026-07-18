"""Run scoring, experiment integrity, gating, and failure-risk metadata.

A score is a tuple, not a single number. Each layer is independently
pass/fail with a diagnostic detail string. A scenario can pass outcome
and fail trajectory and that distinction is visible in reports.

FailureCost is authored per-scenario and maps to a deterministic risk weight
used by aggregate/report consumers. It does not weaken the fail-closed gate:
any gated regression still fails, regardless of weight.

Design:
- Pure dataclasses, stdlib only.
- Verdict enum kept simple: PASS/FAIL/SKIP/INVALID. Aggregate verdict uses
  the same vocabulary plus PASS_WITH_VARIANCE so reports stay consistent.
- Severity is a Literal type for type-checker enforcement without
  requiring a dependency on typing_extensions (Literal is in stdlib
  from 3.8+).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

SCORE_FORMAT_VERSION = 2
GateLayer = Literal["outcome", "trajectory", "constraint"]
GATE_LAYER_ORDER: tuple[GateLayer, ...] = ("outcome", "trajectory", "constraint")


class Verdict(Enum):
    """Per-layer pass/fail verdict."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    INVALID = "INVALID"


SeverityLevel = Literal["low", "medium", "high", "critical"]

_SEVERITY_RISK: dict[SeverityLevel, int] = {
    "low": 1,
    "medium": 4,
    "high": 16,
    "critical": 64,
}


@dataclass
class LayerResult:
    """Result for one scoring layer: pass/fail + human-readable detail."""
    passed: bool
    detail: str


@dataclass
class FailureCost:
    """Per-scenario cost with a stable, inspectable risk weight.

    Defaults to the safest/cheapest profile: low severity, internal,
    reversible, no side effect performed. Scenarios that can cause
    irreversible customer-visible damage must override these.

    Risk weight = severity base (1/4/16/64) plus 2 when customer-visible,
    4 when irreversible, and 8 when a side effect was actually performed.
    This keeps the dimensions readable while ensuring a bare critical failure
    carries more risk than ten bare low-severity failures.
    """
    severity: SeverityLevel = "low"
    customer_visible: bool = False
    reversible: bool = True
    side_effect_performed: bool = False

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RISK:
            raise ValueError(f"unknown failure severity: {self.severity!r}")
        for field_name in ("customer_visible", "reversible", "side_effect_performed"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")

    @property
    def risk_weight(self) -> int:
        """Return the deterministic risk weight for reports and automation."""
        return (
            _SEVERITY_RISK[self.severity]
            + (2 if self.customer_visible else 0)
            + (0 if self.reversible else 4)
            + (8 if self.side_effect_performed else 0)
        )


@dataclass(init=False)
class Score:
    """One run's agent results plus experiment-integrity result.

    Outcome, trajectory, and constraint describe agent behavior. Integrity
    describes whether the declared test condition was actually applied; an
    integrity failure makes the run INVALID rather than an agent failure.

    ``robustness=`` remains accepted as a compatibility spelling for the old
    perturbation-marker field. New code should use ``integrity=``.
    """
    outcome: LayerResult
    trajectory: LayerResult
    constraint: LayerResult
    integrity: LayerResult
    failure_cost: FailureCost

    def __init__(
        self,
        outcome: LayerResult,
        trajectory: LayerResult,
        constraint: LayerResult,
        integrity: LayerResult | None = None,
        failure_cost: FailureCost | None = None,
        *,
        robustness: LayerResult | None = None,
    ) -> None:
        if integrity is not None and robustness is not None:
            raise TypeError("pass integrity or legacy robustness, not both")
        resolved_integrity = integrity if integrity is not None else robustness
        if resolved_integrity is None:
            raise TypeError("missing required integrity layer")
        self.outcome = outcome
        self.trajectory = trajectory
        self.constraint = constraint
        self.integrity = resolved_integrity
        self.failure_cost = failure_cost or FailureCost()

    @property
    def robustness(self) -> LayerResult:
        """Compatibility alias for the 0.8 perturbation-marker layer."""
        return self.integrity

    def layer(self, name: GateLayer) -> LayerResult:
        """Return one gateable agent-behavior layer by name."""
        if name == "outcome":
            return self.outcome
        if name == "trajectory":
            return self.trajectory
        if name == "constraint":
            return self.constraint
        raise ValueError(f"unknown gate layer: {name!r}")

    def gate_passed(self, gate_layers: Collection[GateLayer]) -> bool:
        """Return whether this is a valid run satisfying every selected gate."""
        return self.integrity.passed and all(self.layer(layer).passed for layer in gate_layers)


def score_to_dict(score: Score) -> dict[str, Any]:
    """Serialize a Score to the flat dict shape consumed by report.load_runs().

    Top-level keys: outcome/trajectory/constraint/integrity (each
    {"passed", "detail"}) + failure_cost. This is the canonical v2
    `.score.json` sidecar layer shape — see windtunnel/report.py
    `_cell_from_run` for the reader.
    """
    return {
        "windtunnel_score": SCORE_FORMAT_VERSION,
        "outcome": {"passed": score.outcome.passed, "detail": score.outcome.detail},
        "trajectory": {"passed": score.trajectory.passed, "detail": score.trajectory.detail},
        "constraint": {"passed": score.constraint.passed, "detail": score.constraint.detail},
        "integrity": {"passed": score.integrity.passed, "detail": score.integrity.detail},
        "failure_cost": {
            "severity": score.failure_cost.severity,
            "customer_visible": score.failure_cost.customer_visible,
            "reversible": score.failure_cost.reversible,
            "side_effect_performed": score.failure_cost.side_effect_performed,
            "risk_weight": score.failure_cost.risk_weight,
        },
    }


class ScoreFormatError(ValueError):
    """Raised when a persisted score payload has an unsupported shape."""


def score_from_dict(payload: Mapping[str, Any]) -> Score:
    """Load a v2 score or migrate an unversioned v0.8 score in memory."""
    raw: Mapping[str, Any] = payload
    nested = payload.get("score")
    if isinstance(nested, Mapping):
        raw = nested

    version = payload.get("windtunnel_score", raw.get("windtunnel_score", 1))
    if type(version) is not int:
        raise ScoreFormatError("windtunnel_score must be an integer")
    if version not in {1, SCORE_FORMAT_VERSION}:
        raise ScoreFormatError(
            f"unsupported windtunnel_score version {version}; expected 1 or {SCORE_FORMAT_VERSION}"
        )

    if version == SCORE_FORMAT_VERSION and "integrity" not in raw:
        raise ScoreFormatError("v2 score payload requires integrity")
    integrity_key = "integrity" if "integrity" in raw else "robustness"
    try:
        failure_raw = raw.get("failure_cost", {})
        if not isinstance(failure_raw, Mapping):
            raise TypeError("failure_cost must be an object")
        severity = failure_raw.get("severity", "low")
        if not isinstance(severity, str) or severity not in _SEVERITY_RISK:
            raise TypeError(f"failure_cost.severity is invalid: {severity!r}")
        return Score(
            outcome=_layer_from_dict(raw["outcome"], "outcome"),
            trajectory=_layer_from_dict(raw["trajectory"], "trajectory"),
            constraint=_layer_from_dict(raw["constraint"], "constraint"),
            integrity=_layer_from_dict(raw[integrity_key], integrity_key),
            failure_cost=FailureCost(
                severity=severity,
                customer_visible=_bool_from_dict(
                    failure_raw, "customer_visible", default=False
                ),
                reversible=_bool_from_dict(failure_raw, "reversible", default=True),
                side_effect_performed=_bool_from_dict(
                    failure_raw, "side_effect_performed", default=False
                ),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreFormatError(f"invalid score payload: {exc}") from exc


def _layer_from_dict(raw: object, label: str) -> LayerResult:
    if not isinstance(raw, Mapping):
        raise TypeError(f"{label} must be an object")
    passed = raw.get("passed")
    detail = raw.get("detail", "")
    if type(passed) is not bool or not isinstance(detail, str):
        raise TypeError(f"{label} requires boolean passed and string detail")
    return LayerResult(passed=passed, detail=detail)


def _bool_from_dict(raw: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise TypeError(f"failure_cost.{key} must be a boolean")
    return value
