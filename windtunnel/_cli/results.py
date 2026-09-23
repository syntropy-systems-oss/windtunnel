"""`wt results`: per-scenario pass counts and aggregated metrics for run labels."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from windtunnel._report.load import load_run_groups
from windtunnel._report.model import summarize_group
from windtunnel._report.text import format_metric_summary

RESULTS_OUTPUT_VERSION = 1


def _cmd_results(args: argparse.Namespace) -> int:
    """Summarize saved runs per (label, scenario): runs, pass counts, metrics.

    Reads trace + score sidecar pairs only; never provisions a runtime. Run
    selection is the report's: a label re-used across sweeps reports the
    latest sweep recorded in the ledger. Exit codes: 0 when anything was
    summarized, 2 when the runs directory or every requested label is
    missing.
    """
    runs_dir = Path(args.runs)
    requested: list[str] = list(args.label or [])
    patterns: list[str] = list(args.scenario or [])
    if not runs_dir.is_dir():
        print(f"wt results: runs directory not found: {runs_dir}", file=sys.stderr)
        return 2

    groups = load_run_groups(runs_dir)
    present = sorted({label for _scenario_id, label in groups})
    missing = [label for label in requested if label not in present]
    if missing:
        print(
            f"wt results: no runs with label(s): {', '.join(missing)} "
            f"(labels present: {', '.join(present) or '(none)'})",
            file=sys.stderr,
        )
    labels = [label for label in (requested or present) if label in present]
    results = [
        summarize_group(groups[key])
        for label in labels
        for key in sorted(key for key in groups if key[1] == label)
        if not patterns or any(fnmatch.fnmatchcase(key[0], pattern) for pattern in patterns)
    ]
    if not results:
        if not requested:
            print(f"wt results: no scored runs found under {runs_dir}", file=sys.stderr)
        elif labels:
            print("wt results: no scenario matched --scenario", file=sys.stderr)
        return 2

    if args.json:
        document = {
            "windtunnel_results": RESULTS_OUTPUT_VERSION,
            "runs_dir": str(runs_dir),
            "labels": labels,
            "results": results,
        }
        print(json.dumps(document, indent=2, ensure_ascii=False))
    else:
        print(_render_text(runs_dir, labels, results))
    return 0


def _render_text(runs_dir: Path, labels: list[str], results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for label in labels:
        rows = [row for row in results if row["label"] == label]
        if not rows:
            continue
        runs = sum(int(row["runs"]) for row in rows)
        if lines:
            lines.append("")
        lines.append(
            f"label {label}: {len(rows)} scenario(s), {runs} run(s) under {runs_dir}"
        )
        for row in rows:
            counts = f"{row['passed']}/{row['runs']} pass ({float(row['pass_rate']):.0%})"
            if row["invalid"]:
                counts += f", {row['invalid']} invalid"
            lines.append(f"  {row['scenario_id']:<40} {row['verdict']:<18} {counts}")
            metrics: dict[str, dict[str, Any]] = row["metrics"]
            width = max((len(name) for name in metrics), default=0)
            for name, summary in metrics.items():
                lines.append(f"    {name:<{width}}  {format_metric_summary(summary)}")
    return "\n".join(lines)
