"""Markdown and JSON renderers for report data."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from windtunnel._report.load import load_runs
from windtunnel._report.model import _build_report_data


def generate_markdown(
    runs_dir: Path,
    out: TextIO | None = None,
) -> None:
    """Generate a terminal-readable Markdown summary of bench results."""
    if out is None:
        out = sys.stdout

    cells = load_runs(runs_dir=runs_dir)
    data = _build_report_data(cells)

    ts = data["latest_run_ts"] or "unknown"
    scenario_count = data["scenario_count"]
    variants = data["variants"]
    scenarios = data["scenarios"]
    summary = data["summary"]

    lines: list[str] = []
    lines.append(f"# Agent Bench Report — {ts}")
    lines.append("")
    lines.append(f"**Scenarios:** {scenario_count}  |  **Variants:** {len(variants)}")
    lines.append("")
    lines.append("## Pass Rates (all cells)")
    lines.append("")
    lines.append("| Layer | Pass Rate |")
    lines.append("|-------|-----------|")
    for layer in ("outcome", "trajectory", "constraint", "integrity"):
        rate = summary[f"{layer}_pass_rate"]
        lines.append(f"| {layer.capitalize()} | {rate * 100:.1f}% |")
    robustness_rate = summary["robustness_pass_rate"]
    robustness_text = "N/A" if robustness_rate is None else f"{robustness_rate * 100:.1f}%"
    lines.append(f"| Robustness cases | {robustness_text} |")
    lines.append(f"| Failure risk | {summary['total_failure_risk']:.2f} |")
    lines.append("")
    lines.append("## Scenario Matrix")
    lines.append("")

    if not variants:
        lines.append("_No runs found._")
        lines.append("")
        print("\n".join(lines), file=out, end="")
        return

    header = ["Scenario"] + variants
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    pass_counts: dict[str, int] = {variant: 0 for variant in variants}
    total = len(scenarios)
    for scenario in scenarios:
        row = [f"`{scenario['scenario_id']}`"]
        for variant in variants:
            cell = scenario["cells"].get(variant)
            if cell is None:
                row.append("—")
            else:
                verdict = cell["verdict"]
                tool_count = cell["tool_call_count"]
                icon = (
                    "PASS" if verdict == "PASS"
                    else "VAR" if "VARIANCE" in verdict
                    else "INVALID" if verdict == "INVALID"
                    else "FAIL"
                )
                severity = cell["failure_cost"]["severity"]
                severity_tag = f"[{severity}]" if severity != "low" else ""
                row.append(f"{icon} n={tool_count}{(' ' + severity_tag) if severity_tag else ''}")
                if verdict == "PASS" or "VARIANCE" in verdict:
                    pass_counts[variant] += 1
        lines.append("| " + " | ".join(row) + " |")

    summary_row = ["**PASS**"]
    for variant in variants:
        summary_row.append(f"**{pass_counts[variant]}/{total}**")
    lines.append("| " + " | ".join(summary_row) + " |")
    lines.append("")
    lines.append("## Per-Layer Breakdown")
    lines.append("")

    layer_labels = [
        ("outcome", "Outcome"),
        ("trajectory", "Trajectory"),
        ("constraint", "Constraint"),
        ("integrity", "Integrity"),
    ]
    layer_header = ["Layer"] + variants
    lines.append("| " + " | ".join(layer_header) + " |")
    lines.append("|" + "|".join(["---"] * len(layer_header)) + "|")

    for layer_key, layer_label in layer_labels:
        row = [layer_label]
        for variant in variants:
            rates: list[float] = []
            for scenario in scenarios:
                cell = scenario["cells"].get(variant)
                if cell is not None:
                    rates.append(float(cell["layers"][layer_key]["pass_rate"]))
            row.append("—" if not rates else f"{sum(rates) / len(rates):.0%}")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    print("\n".join(lines), file=out, end="")


def generate_json(
    runs_dir: Path,
    out: TextIO | None = None,
) -> None:
    """Generate report data as a standalone JSON document."""
    if out is None:
        out = sys.stdout

    cells = load_runs(runs_dir=runs_dir)
    data = _build_report_data(cells)
    print(json.dumps(data, indent=2, ensure_ascii=False), file=out)
