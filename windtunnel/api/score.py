"""Run scoring, experiment integrity, gating, and failure-risk metadata.

A score is a tuple, not a single number. Each layer is independently
pass/fail with a diagnostic detail string. A scenario can pass outcome
and fail trajectory and that distinction is visible in reports.

A layer may also carry named metrics next to its verdict — measurements
such as ``{"final_correct": True, "revisions": 9}`` that iteration tooling
aggregates across runs. Metrics never change pass/fail; they replace the
old habit of encoding numbers in ``detail`` for callers to re-parse.

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

import math
import numbers
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeAlias

SCORE_FORMAT_VERSION = 2
GateLayer = Literal["outcome", "trajectory", "constraint"]
GATE_LAYER_ORDER: tuple[GateLayer, ...] = ("outcome", "trajectory", "constraint")
SCORE_LAYER_ORDER: tuple[str, ...] = (*GATE_LAYER_ORDER, "integrity")

MetricValue: TypeAlias = bool | int | float | str
"""One named metric value: a flag, a count or measure, or a category label."""


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
    """Result for one scoring layer: pass/fail + human-readable detail.

    metrics: optional named measurements produced alongside the verdict,
        e.g. ``{"final_correct": True, "revisions": 9}``. They never affect
        ``passed``; they exist so `wt results` and `wt compare` can aggregate
        them (rate for booleans, mean/min/max for numbers, counts for
        strings) instead of re-parsing ``detail``. Names are non-empty
        strings. Values are bool, int, float (finite), or str; other integral
        and real numbers (e.g. numpy scalars) are converted to int/float.
        Anything else raises, so a bad metric inside ``outcome_fn`` fails the
        layer with a diagnostic rather than corrupting a sidecar.
    """
    passed: bool
    detail: str
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metrics = validate_metrics(self.metrics)


def validate_metrics(metrics: Mapping[str, object]) -> dict[str, MetricValue]:
    """Return a validated, plain-typed copy of a metrics mapping.

    Raises TypeError/ValueError naming the offending metric. Shared by
    LayerResult construction and sidecar loading so both reject the same
    shapes.
    """
    if not isinstance(metrics, Mapping):
        raise TypeError(f"metrics must be a mapping of name to value, got {type(metrics).__name__}")
    validated: dict[str, MetricValue] = {}
    for name, value in metrics.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"metric names must be non-empty strings, got {name!r}")
        if isinstance(value, bool | str):
            validated[name] = value
        elif isinstance(value, numbers.Integral):
            validated[name] = int(value)
        elif isinstance(value, numbers.Real):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"metric {name!r} must be a finite number, got {value!r}")
            validated[name] = number
        else:
            raise TypeError(
                f"metric {name!r} must be bool, int, float, or str, got {type(value).__name__}"
            )
    return validated


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

    @property
    def metrics(self) -> dict[str, MetricValue]:
        """Every layer's metrics in one flat map keyed ``"<layer>.<name>"``.

        Qualifying by layer keeps two layers that both report e.g. ``count``
        from silently overwriting each other.
        """
        flat: dict[str, MetricValue] = {}
        for layer_name in SCORE_LAYER_ORDER:
            layer: LayerResult = getattr(self, layer_name)
            for name, value in layer.metrics.items():
                flat[f"{layer_name}.{name}"] = value
        return flat


def _layer_to_dict(layer: LayerResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"passed": layer.passed, "detail": layer.detail}
    if layer.metrics:
        payload["metrics"] = dict(layer.metrics)
    return payload


def score_to_dict(score: Score) -> dict[str, Any]:
    """Serialize a Score to the flat dict shape consumed by report.load_runs().

    Top-level keys: outcome/trajectory/constraint/integrity (each
    {"passed", "detail"}, plus "metrics" only when the layer reported any)
    + failure_cost. This is the canonical v2 `.score.json` sidecar layer
    shape — see windtunnel/report.py `_cell_from_run` for the reader. A
    score without metrics serializes byte-for-byte as it did before metrics
    existed, so older readers are unaffected.
    """
    return {
        "windtunnel_score": SCORE_FORMAT_VERSION,
        "outcome": _layer_to_dict(score.outcome),
        "trajectory": _layer_to_dict(score.trajectory),
        "constraint": _layer_to_dict(score.constraint),
        "integrity": _layer_to_dict(score.integrity),
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
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise TypeError(f"{label}.metrics must be an object")
    return LayerResult(passed=passed, detail=detail, metrics=dict(metrics))


def _bool_from_dict(raw: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise TypeError(f"failure_cost.{key} must be a boolean")
    return value
