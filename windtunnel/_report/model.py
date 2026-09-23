"""Build presentation-neutral report cells, summaries, and diffs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from windtunnel._report.load import LoadedRun, RunGroup, load_runs
from windtunnel.api.aggregate import MetricSummary, aggregate_metrics
from windtunnel.api.score import GATE_LAYER_ORDER, FailureCost, score_from_dict

_DEFAULT_GATE_LAYERS = list(GATE_LAYER_ORDER)


def _tool_call_count(trace_data: dict[str, Any]) -> int:
    """Count total tool calls across all turns in a trace dict."""
    count = 0
    for turn in trace_data.get("turns", []):
        count += len(turn.get("tool_calls") or [])
    return count


def _cell_from_run(
    trace_data: dict[str, Any],
    score_data: dict[str, Any],
) -> dict[str, Any]:
    """Build a report cell from raw trace and score dictionaries."""
    outcome = score_data.get("outcome", {})
    trajectory = score_data.get("trajectory", {})
    constraint = score_data.get("constraint", {})
    integrity = score_data.get("integrity", score_data.get("robustness", {}))
    fc = score_data.get("failure_cost", {})
    scenario_data = score_data.get("scenario", {})
    if not isinstance(scenario_data, dict):
        scenario_data = {}

    aggregate = score_data.get("_aggregate")
    if not isinstance(aggregate, dict):
        aggregate = None
    layer_pass_rates = aggregate.get("layer_pass_rates", {}) if aggregate else {}

    # Default to every layer, not just outcome: a sidecar with no recorded
    # gate_layers (pre-declared-gates format) still scored trajectory/
    # constraint, and a not-configured layer always reports passed=True, so
    # gating on it costs nothing while a genuine failure recorded there no
    # longer gets silently waved through as a legacy artifact.
    gate_layers = scenario_data.get("gate_layers", _DEFAULT_GATE_LAYERS)
    if not isinstance(gate_layers, list):
        gate_layers = _DEFAULT_GATE_LAYERS
    raw_layers = {
        "outcome": outcome,
        "trajectory": trajectory,
        "constraint": constraint,
    }
    gate_passed = all(
        isinstance(raw_layers.get(name), dict) and raw_layers[name].get("passed", False)
        for name in gate_layers
    )
    integrity_passed = bool(integrity.get("passed", False))
    aggregate_integrity_rate = layer_pass_rates.get("integrity")
    aggregate_integrity_valid = (
        float(aggregate_integrity_rate) == 1.0
        if isinstance(aggregate_integrity_rate, int | float)
        else integrity_passed
    )
    verdict = str(aggregate.get("verdict")) if aggregate is not None else (
        "INVALID" if not integrity_passed else ("PASS" if gate_passed else "FAIL")
    )

    def _layer(name: str, raw: dict[str, Any]) -> dict[str, Any]:
        aggregate_rate = layer_pass_rates.get(name)
        rate = (
            float(aggregate_rate)
            if isinstance(aggregate_rate, int | float)
            else (1.0 if raw.get("passed", False) else 0.0)
        )
        detail = str(raw.get("detail", ""))
        if aggregate is not None:
            detail = f"aggregate pass rate={rate:.0%}; latest run: {detail}"
        return {"passed": rate == 1.0, "pass_rate": rate, "detail": detail}

    failure_cost = FailureCost(
        severity=fc.get("severity", "low"),
        customer_visible=bool(fc.get("customer_visible", False)),
        reversible=bool(fc.get("reversible", True)),
        side_effect_performed=bool(fc.get("side_effect_performed", False)),
    )
    risk_weight = int(fc.get("risk_weight", failure_cost.risk_weight))
    aggregate_pass_rate = aggregate.get("pass_rate") if aggregate is not None else None
    if aggregate is not None and not isinstance(aggregate_pass_rate, int | float):
        legacy_rates = aggregate.get("layer_pass_rates", {})
        if isinstance(legacy_rates, dict):
            legacy_gate_rate = legacy_rates.get("outcome")
            if isinstance(legacy_gate_rate, int | float):
                aggregate_pass_rate = legacy_gate_rate
    gate_pass_rate = (
        float(aggregate_pass_rate)
        if isinstance(aggregate_pass_rate, int | float)
        else (1.0 if gate_passed and integrity_passed else 0.0)
    )
    has_perturbations = bool(scenario_data.get("has_perturbations", False))
    failure_risk_raw = aggregate.get("failure_risk") if aggregate is not None else None
    failure_risk = (
        float(failure_risk_raw)
        if isinstance(failure_risk_raw, int | float)
        else (0.0 if verdict == "INVALID" else risk_weight * (1.0 - gate_pass_rate))
    )

    return {
        "verdict": verdict,
        "gate_layers": list(gate_layers),
        "gate_pass_rate": gate_pass_rate,
        "tool_call_count": _tool_call_count(trace_data),
        "layers": {
            "outcome": _layer("outcome", outcome),
            "trajectory": _layer("trajectory", trajectory),
            "constraint": _layer("constraint", constraint),
            "integrity": _layer("integrity", integrity),
        },
        "robustness": {
            "applicable": has_perturbations,
            "passed": (
                has_perturbations and gate_pass_rate == 1.0 and aggregate_integrity_valid
            ),
            "pass_rate": (
                gate_pass_rate if has_perturbations and aggregate_integrity_valid else None
            ),
        },
        "failure_cost": {
            "severity": failure_cost.severity,
            "customer_visible": failure_cost.customer_visible,
            "reversible": failure_cost.reversible,
            "side_effect_performed": failure_cost.side_effect_performed,
            "risk_weight": risk_weight,
        },
        "failure_risk": failure_risk,
        "trace": {
            "run_id": trace_data.get("run_id", ""),
            "started_at": trace_data.get("started_at", ""),
            "finished_at": trace_data.get("finished_at", ""),
            "model": trace_data.get("model", ""),
            "quant": trace_data.get("quant", ""),
            "turns": trace_data.get("turns", []),
        },
        "aggregate": aggregate,
    }


def _build_report_data(cells: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """Build the presentation-neutral report data structure."""
    if not cells:
        return {
            "latest_run_ts": "",
            "scenario_count": 0,
            "variants": [],
            "scenarios": [],
            "summary": {
                "outcome_pass_rate": 0.0,
                "trajectory_pass_rate": 0.0,
                "constraint_pass_rate": 0.0,
                "integrity_pass_rate": 0.0,
                "robustness_pass_rate": None,
                "total_failure_risk": 0.0,
            },
        }

    scenario_ids_seen: list[str] = []
    variant_ids_seen: list[str] = []
    for scenario_id, variant_id in sorted(cells):
        if scenario_id not in scenario_ids_seen:
            scenario_ids_seen.append(scenario_id)
        if variant_id not in variant_ids_seen:
            variant_ids_seen.append(variant_id)

    latest_ts = ""
    for run in cells.values():
        ts = run["trace"].get("started_at", "")
        if ts > latest_ts:
            latest_ts = ts

    scenarios_list: list[dict[str, Any]] = []
    for scenario_id in scenario_ids_seen:
        row_cells: dict[str, Any] = {}
        for variant_id in variant_ids_seen:
            key = (scenario_id, variant_id)
            if key in cells:
                run = cells[key]
                row_cells[variant_id] = _cell_from_run(run["trace"], run["score"])
        scenarios_list.append({"scenario_id": scenario_id, "cells": row_cells})

    all_cells = [_cell_from_run(run["trace"], run["score"]) for run in cells.values()]
    count = len(all_cells)

    def _layer_rate(layer: str) -> float:
        if count == 0:
            return 0.0
        return sum(float(cell["layers"][layer]["pass_rate"]) for cell in all_cells) / count

    robustness_rates = [
        float(cell["robustness"]["pass_rate"])
        for cell in all_cells
        if cell["robustness"]["pass_rate"] is not None
    ]
    summary = {
        "outcome_pass_rate": _layer_rate("outcome"),
        "trajectory_pass_rate": _layer_rate("trajectory"),
        "constraint_pass_rate": _layer_rate("constraint"),
        "integrity_pass_rate": _layer_rate("integrity"),
        "robustness_pass_rate": (
            sum(robustness_rates) / len(robustness_rates) if robustness_rates else None
        ),
        "total_failure_risk": sum(float(cell["failure_risk"]) for cell in all_cells),
    }

    return {
        "latest_run_ts": latest_ts,
        "scenario_count": len(scenario_ids_seen),
        "variants": variant_ids_seen,
        "scenarios": scenarios_list,
        "summary": summary,
    }


def compute_diff(
    runs_dir: Path,
    label_a: str,
    label_b: str,
) -> list[dict[str, Any]]:
    """Return scenarios whose verdict changed between two labels."""
    cells = load_runs(runs_dir=runs_dir)
    scenario_ids = sorted({scenario_id for scenario_id, _variant in cells})

    result: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        run_a = cells.get((scenario_id, label_a))
        run_b = cells.get((scenario_id, label_b))
        if run_a is None or run_b is None:
            continue

        cell_a = _cell_from_run(run_a["trace"], run_a["score"])
        cell_b = _cell_from_run(run_b["trace"], run_b["score"])
        verdict_a = cell_a["verdict"]
        verdict_b = cell_b["verdict"]
        if verdict_a == verdict_b:
            continue

        direction = (
            "improvement" if _verdict_rank(verdict_b) > _verdict_rank(verdict_a) else "regression"
        )
        result.append(
            {
                "scenario_id": scenario_id,
                "direction": direction,
                "verdict_a": verdict_a,
                "verdict_b": verdict_b,
                "risk_a": cell_a["failure_risk"],
                "risk_b": cell_b["failure_risk"],
                "risk_delta": float(cell_b["failure_risk"]) - float(cell_a["failure_risk"]),
            }
        )

    return sorted(
        result,
        key=lambda item: (
            0 if item["direction"] == "regression" else 1,
            -float(item["risk_delta"]),
            str(item["scenario_id"]),
        ),
    )


def _verdict_rank(verdict: object) -> int:
    """Order aggregate verdicts from hard failure to stable pass."""
    if verdict == "PASS":
        return 3
    if isinstance(verdict, str) and "VARIANCE" in verdict:
        return 2
    if verdict == "FAIL":
        return 1
    return 0


# ─── Per-label results and metric deltas ─────────────────────────────────────


def _run_row(run: LoadedRun) -> dict[str, Any]:
    """One saved run as `wt results` reports it: identity, verdict, metrics."""
    return {
        "path": str(run.path),
        "run_id": str(run.trace.get("run_id", "")),
        "started_at": str(run.trace.get("started_at", "")),
        "verdict": _cell_from_run(run.trace, run.score)["verdict"],
        "metrics": score_from_dict(run.score).metrics,
    }


def summarize_group(group: RunGroup) -> dict[str, Any]:
    """Summarize one (scenario, label) group: pass counts and aggregated metrics.

    Run verdicts use the report's own per-run rule; the headline verdict is
    the ledger aggregate's when the group came from one (so variance
    allowances are honored), otherwise it is derived strictly from the runs.
    """
    rows = [_run_row(run) for run in group.runs]
    total = len(rows)
    passed = sum(1 for row in rows if row["verdict"] == "PASS")
    invalid = sum(1 for row in rows if row["verdict"] == "INVALID")
    ledger_verdict = group.aggregate.get("verdict") if group.aggregate is not None else None
    if isinstance(ledger_verdict, str):
        verdict = ledger_verdict
    elif invalid:
        verdict = "INVALID"
    else:
        verdict = "PASS" if passed == total else "FAIL"
    metrics = aggregate_metrics(row["metrics"] for row in rows)
    return {
        "scenario_id": group.scenario_id,
        "label": group.label,
        "verdict": verdict,
        "runs": total,
        "passed": passed,
        "failed": total - passed - invalid,
        "invalid": invalid,
        "pass_rate": passed / total if total else 0.0,
        "selection": "latest_aggregate" if group.aggregate is not None else "all_traces",
        "metrics": {name: summary.to_dict() for name, summary in metrics.items()},
        "traces": rows,
    }


def group_metric_summaries(group: RunGroup) -> dict[str, MetricSummary]:
    """Aggregate every run's Score.metrics in one group."""
    return aggregate_metrics(score_from_dict(run.score).metrics for run in group.runs)


def metric_delta(
    baseline: MetricSummary | None,
    candidate: MetricSummary | None,
) -> dict[str, Any]:
    """Compare one metric's summaries across two labels.

    ``delta`` is the candidate minus the baseline: the mean for numbers, the
    rate for booleans, and a per-value count difference for strings and
    mixed values. It is None when the metric is missing on one side or the
    two sides disagree on its kind.
    """
    reference = candidate if candidate is not None else baseline
    kind = reference.kind if reference is not None else "mixed"
    delta: float | dict[str, int] | None = None
    if baseline is not None and candidate is not None:
        if baseline.kind != candidate.kind:
            kind = "mixed"
        elif kind == "number" and baseline.mean is not None and candidate.mean is not None:
            delta = candidate.mean - baseline.mean
        elif kind == "bool" and baseline.rate is not None and candidate.rate is not None:
            delta = candidate.rate - baseline.rate
        elif baseline.counts is not None and candidate.counts is not None:
            values = sorted(set(baseline.counts) | set(candidate.counts))
            delta = {
                value: candidate.counts.get(value, 0) - baseline.counts.get(value, 0)
                for value in values
            }
    return {
        "kind": kind,
        "baseline": baseline.to_dict() if baseline is not None else None,
        "candidate": candidate.to_dict() if candidate is not None else None,
        "delta": delta,
    }


def compute_metric_deltas(
    groups: dict[tuple[str, str], RunGroup],
    label_a: str,
    label_b: str,
) -> list[dict[str, Any]]:
    """Return per-scenario metric deltas of ``label_b`` against ``label_a``.

    Mirrors compute_diff's scope: only scenarios with runs under both labels.
    Entries are sorted by scenario, then metric name.
    """
    scenario_ids = sorted({scenario_id for scenario_id, _label in groups})
    result: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        group_a = groups.get((scenario_id, label_a))
        group_b = groups.get((scenario_id, label_b))
        if group_a is None or group_b is None:
            continue
        metrics_a = group_metric_summaries(group_a)
        metrics_b = group_metric_summaries(group_b)
        for name in sorted(set(metrics_a) | set(metrics_b)):
            result.append(
                {
                    "scenario_id": scenario_id,
                    "baseline_label": label_a,
                    "label": label_b,
                    "metric": name,
                    **metric_delta(metrics_a.get(name), metrics_b.get(name)),
                }
            )
    return result
