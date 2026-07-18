"""Load trace/score pairs and aggregate ledger records from disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from windtunnel.api.score import ScoreFormatError, score_from_dict, score_to_dict
from windtunnel.api.trace import TRACE_FORMAT_VERSION, is_trace_json_path


def load_runs(runs_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the latest reportable run for each scenario/variant pair."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return {}

    candidates: dict[
        tuple[str, str],
        list[tuple[Path, dict[str, Any], dict[str, Any]]],
    ] = {}

    for trace_path in sorted(runs_dir.rglob("*.json")):
        if not is_trace_json_path(trace_path):
            continue
        score_path = trace_path.with_suffix(".score.json")
        if not score_path.exists():
            continue

        try:
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(trace_data, dict) or not isinstance(score_data, dict):
            continue
        trace_version = trace_data.get("windtunnel_trace", 0)
        if type(trace_version) is not int or trace_version not in {0, TRACE_FORMAT_VERSION}:
            continue
        try:
            normalized_score = score_to_dict(score_from_dict(score_data))
        except ScoreFormatError:
            continue
        score_data = {**score_data, **normalized_score}
        scenario_id = trace_data.get("scenario_id")
        variant_id = trace_data.get("variant_id")
        if not isinstance(scenario_id, str) or not isinstance(variant_id, str):
            continue
        candidates.setdefault((scenario_id, variant_id), []).append(
            (trace_path, trace_data, score_data)
        )

    result: dict[tuple[str, str], dict[str, Any]] = {}
    aggregates = _load_latest_aggregates(runs_dir)
    for key, grouped_runs in candidates.items():
        aggregate = aggregates.get(key)
        selected_runs = grouped_runs
        if aggregate is not None:
            aggregate_run_ids = {str(run_id) for run_id in aggregate.get("run_ids", [])}
            matching = [
                candidate
                for candidate in grouped_runs
                if str(candidate[1].get("run_id", "")) in aggregate_run_ids
            ]
            if matching:
                selected_runs = matching
            else:
                aggregate = None

        _path, trace_data, score_data = max(
            selected_runs,
            key=lambda candidate: (
                str(candidate[1].get("started_at", "")),
                candidate[0].name,
            ),
        )
        if aggregate is not None:
            score_data = {**score_data, "_aggregate": aggregate}
        result[key] = {"trace": trace_data, "score": score_data}

    return result


def _load_latest_aggregates(
    runs_dir: Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load the last valid ledger aggregate for each scenario/variant pair."""
    ledger_path = runs_dir / "ledger.ndjsonl"
    if not ledger_path.is_file():
        return {}

    aggregates: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        ledger_version = record.get("windtunnel_ledger", 0)
        if type(ledger_version) is not int or ledger_version not in {0, 1}:
            continue
        layer_rates = record.get("layer_pass_rates")
        if isinstance(layer_rates, dict) and "integrity" not in layer_rates:
            legacy_integrity = layer_rates.get("robustness")
            if isinstance(legacy_integrity, int | float):
                record = {
                    **record,
                    "layer_pass_rates": {**layer_rates, "integrity": legacy_integrity},
                }
        scenario_id = record.get("scenario_id")
        label = record.get("label")
        if isinstance(scenario_id, str) and isinstance(label, str):
            aggregates[(scenario_id, label)] = record
    return aggregates
