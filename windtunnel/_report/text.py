"""Markdown and JSON renderers for report data."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

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


# ─── Metric formatting (wt results / wt compare) ─────────────────────────────


def _format_number(value: object) -> str:
    """Integers verbatim, other numbers to four significant digits."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{value}={count}" for value, count in counts.items())


def format_metric_summary(summary: dict[str, Any] | None) -> str:
    """Render one MetricSummary.to_dict() as a compact human line."""
    if summary is None:
        return "absent"
    kind = summary.get("kind")
    count = summary.get("count", 0)
    if kind == "bool":
        rate = float(summary.get("rate") or 0.0)
        return f"{rate:.0%} true ({summary.get('true_count', 0)}/{count})"
    if kind == "number":
        return (
            f"mean {_format_number(summary.get('mean'))}  "
            f"min {_format_number(summary.get('min'))}  "
            f"max {_format_number(summary.get('max'))}  (n={count})"
        )
    prefix = "mixed: " if kind == "mixed" else ""
    return f"{prefix}{_format_counts(summary.get('counts') or {})}  (n={count})"


def format_metric_delta(entry: dict[str, Any]) -> str:
    """Render one compute_metric_deltas() entry as ``baseline -> candidate (delta)``."""
    baseline = entry.get("baseline")
    candidate = entry.get("candidate")
    delta = entry.get("delta")
    kind = entry.get("kind")
    if baseline is None or candidate is None or delta is None:
        return f"{format_metric_summary(baseline)} -> {format_metric_summary(candidate)}"
    if kind == "number":
        return (
            f"mean {_format_number(baseline.get('mean'))} -> "
            f"{_format_number(candidate.get('mean'))} ({float(delta):+.4g})"
        )
    if kind == "bool":
        return (
            f"rate {float(baseline.get('rate') or 0.0):.0%} -> "
            f"{float(candidate.get('rate') or 0.0):.0%} ({float(delta) * 100:+.0f}pp)"
        )
    changes = {value: diff for value, diff in dict(delta).items() if diff}
    rendered = ", ".join(f"{value} {diff:+d}" for value, diff in changes.items())
    return (
        f"{_format_counts(baseline.get('counts') or {})} -> "
        f"{_format_counts(candidate.get('counts') or {})} ({rendered or 'no change'})"
    )
