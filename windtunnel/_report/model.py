"""Build presentation-neutral report cells, summaries, and diffs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from windtunnel._report.load import load_runs


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
    robustness = score_data.get("robustness", {})
    fc = score_data.get("failure_cost", {})

    aggregate = score_data.get("_aggregate")
    if not isinstance(aggregate, dict):
        aggregate = None
    layer_pass_rates = aggregate.get("layer_pass_rates", {}) if aggregate else {}

    verdict = (
        str(aggregate.get("verdict"))
        if aggregate is not None
        else ("PASS" if outcome.get("passed", False) else "FAIL")
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

    return {
        "verdict": verdict,
        "tool_call_count": _tool_call_count(trace_data),
        "layers": {
            "outcome": _layer("outcome", outcome),
            "trajectory": _layer("trajectory", trajectory),
            "constraint": _layer("constraint", constraint),
            "robustness": _layer("robustness", robustness),
        },
        "failure_cost": {
            "severity": fc.get("severity", "low"),
            "customer_visible": fc.get("customer_visible", False),
            "reversible": fc.get("reversible", True),
            "side_effect_performed": fc.get("side_effect_performed", False),
        },
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
                "robustness_pass_rate": 0.0,
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

    summary = {
        "outcome_pass_rate": _layer_rate("outcome"),
        "trajectory_pass_rate": _layer_rate("trajectory"),
        "constraint_pass_rate": _layer_rate("constraint"),
        "robustness_pass_rate": _layer_rate("robustness"),
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
            }
        )

    return result


def _verdict_rank(verdict: object) -> int:
    """Order aggregate verdicts from hard failure to stable pass."""
    if verdict == "PASS":
        return 2
    if isinstance(verdict, str) and "VARIANCE" in verdict:
        return 1
    return 0
