"""Static report generator for the agent reliability bench.

Produces:
  - report.html: self-contained HTML with JSON island (no server, no external deps)
  - Markdown summary: terminal-readable, no HTML tags
  - JSON report data: the same structure embedded in the HTML JSON island

Design decisions:
  - No Jinja2 — pure stdlib string building keeps the package dep-free.
  - JSON island pattern: <script type="application/json" id="bench-data">{...}</script>
    All data is embedded; JS reads it at runtime for the diff view / drill-down.
  - Diff view is an interactive toggle in the single HTML file — two <select>
    dropdowns let the user pick labels A and B; vanilla JS highlights changed cells.
  - Runs/ layout consumed: <runs>/<scenario_id>/<agent_id>/<variant_id>/.../*.json
    Score sidecar: <same path>.score.json (written by the bench runner).
  - One score per (scenario_id, variant_id) — if multiple runs exist, the latest
    (by filename lexicographic order, which is chronological per storage_path())
    is used. Aggregation across N runs happens upstream (api/aggregate.py);
    the report shows the latest run per cell.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

# ─── Run loader ───────────────────────────────────────────────────────────────

def load_runs(runs_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Walk runs_dir and return one cell per (scenario_id, variant_id).

    Layout: <runs>/<scenario_id>/<agent_id>/<variant_id>/<model>/<quant>/<ts>_<id>.json
    Score sidecar: same path + ".score.json"

    When multiple runs exist for the same (scenario_id, variant_id), the
    lexicographically latest filename is used (= chronologically latest,
    per storage_path() timestamp naming).

    Returns:
        dict keyed by (scenario_id, variant_id), values are:
          {"trace": <trace dict>, "score": <score dict>}
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return {}

    # Collect: (scenario_id, variant_id) -> latest (trace_path, score_path)
    candidates: dict[tuple[str, str], tuple[Path, Path]] = {}

    for trace_path in sorted(runs_dir.rglob("*.json")):
        # Skip score sidecars
        if trace_path.name.endswith(".score.json"):
            continue
        score_path = trace_path.with_suffix(".score.json")
        if not score_path.exists():
            continue

        # Derive scenario_id and variant_id from the path structure:
        # <runs>/<scenario_id>/<agent_id>/<variant_id>/<model>/<quant>/<filename>
        parts = trace_path.relative_to(runs_dir).parts
        if len(parts) < 6:
            continue  # not deep enough — skip

        scenario_id = parts[0]
        variant_id = parts[2]
        key = (scenario_id, variant_id)

        # Keep latest (sorted rglob gives lexicographic order = chronological)
        candidates[key] = (trace_path, score_path)

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, (trace_path, score_path) in candidates.items():
        try:
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
            result[key] = {"trace": trace_data, "score": score_data}
        except (json.JSONDecodeError, OSError):
            continue

    return result


# ─── Data model builders ──────────────────────────────────────────────────────

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
    """Build a report cell dict from raw trace + score dicts."""
    outcome = score_data.get("outcome", {})
    trajectory = score_data.get("trajectory", {})
    constraint = score_data.get("constraint", {})
    robustness = score_data.get("robustness", {})
    fc = score_data.get("failure_cost", {})

    # Verdict: PASS if outcome passed, FAIL otherwise
    verdict = "PASS" if outcome.get("passed", False) else "FAIL"

    return {
        "verdict": verdict,
        "tool_call_count": _tool_call_count(trace_data),
        "layers": {
            "outcome": {
                "passed": outcome.get("passed", False),
                "detail": outcome.get("detail", ""),
            },
            "trajectory": {
                "passed": trajectory.get("passed", False),
                "detail": trajectory.get("detail", ""),
            },
            "constraint": {
                "passed": constraint.get("passed", False),
                "detail": constraint.get("detail", ""),
            },
            "robustness": {
                "passed": robustness.get("passed", False),
                "detail": robustness.get("detail", ""),
            },
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
    }


def _build_report_data(cells: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """Build the JSON-island data structure from loaded cells.

    Structure:
      {
        "latest_run_ts": "<ISO>",
        "scenario_count": N,
        "variants": ["baseline", "variant_b", ...],
        "scenarios": [
          {
            "scenario_id": "sc_alpha",
            "cells": {
              "baseline": { <cell> },
              "variant_b": { <cell> },
              ...
            }
          },
          ...
        ],
        "summary": {
          "outcome_pass_rate": 0.75,
          "trajectory_pass_rate": 0.75,
          "constraint_pass_rate": 0.75,
          "robustness_pass_rate": 0.75,
        }
      }
    """
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

    # Collect ordered scenario_ids and variant_ids
    scenario_ids_seen: list[str] = []
    variant_ids_seen: list[str] = []
    for sc_id, var_id in sorted(cells.keys()):
        if sc_id not in scenario_ids_seen:
            scenario_ids_seen.append(sc_id)
        if var_id not in variant_ids_seen:
            variant_ids_seen.append(var_id)

    # Latest run timestamp
    latest_ts = ""
    for _key, run in cells.items():
        ts = run["trace"].get("started_at", "")
        if ts > latest_ts:
            latest_ts = ts

    # Build per-scenario rows
    scenarios_list: list[dict[str, Any]] = []
    for sc_id in scenario_ids_seen:
        row_cells: dict[str, Any] = {}
        for var_id in variant_ids_seen:
            key = (sc_id, var_id)
            if key in cells:
                run = cells[key]
                row_cells[var_id] = _cell_from_run(run["trace"], run["score"])
        scenarios_list.append({
            "scenario_id": sc_id,
            "cells": row_cells,
        })

    # Summary: per-layer pass rates across all cells
    all_cells = [
        _cell_from_run(run["trace"], run["score"])
        for run in cells.values()
    ]
    n = len(all_cells)

    def _layer_rate(layer: str) -> float:
        if n == 0:
            return 0.0
        return sum(1 for c in all_cells if c["layers"][layer]["passed"]) / n

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


# ─── Diff computation ─────────────────────────────────────────────────────────

def compute_diff(
    runs_dir: Path,
    label_a: str,
    label_b: str,
) -> list[dict[str, Any]]:
    """Return scenarios that changed verdict between label_a and label_b.

    A "label" here is a variant_id. Compares the verdict for each
    scenario across the two variants and returns only the scenarios
    where the verdict changed.

    Returns list of dicts:
      {
        "scenario_id": str,
        "direction": "regression" | "improvement",
        "verdict_a": "PASS" | "FAIL",
        "verdict_b": "PASS" | "FAIL",
      }
    """
    cells = load_runs(runs_dir=runs_dir)

    # Collect all scenario_ids
    scenario_ids = sorted({sc_id for sc_id, _var in cells})

    result: list[dict[str, Any]] = []
    for sc_id in scenario_ids:
        run_a = cells.get((sc_id, label_a))
        run_b = cells.get((sc_id, label_b))

        if run_a is None or run_b is None:
            continue  # can't diff if one side is missing

        cell_a = _cell_from_run(run_a["trace"], run_a["score"])
        cell_b = _cell_from_run(run_b["trace"], run_b["score"])

        verdict_a = cell_a["verdict"]
        verdict_b = cell_b["verdict"]

        if verdict_a == verdict_b:
            continue  # no change — exclude from diff

        # Regression: was PASS, now FAIL
        # Improvement: was FAIL, now PASS
        if verdict_a == "PASS" and verdict_b == "FAIL":
            direction = "regression"
        elif verdict_a == "FAIL" and verdict_b == "PASS":
            direction = "improvement"
        else:
            # PASS_WITH_VARIANCE transitions etc.
            direction = "regression" if verdict_b != "PASS" else "improvement"

        result.append({
            "scenario_id": sc_id,
            "direction": direction,
            "verdict_a": verdict_a,
            "verdict_b": verdict_b,
        })

    return result


# ─── Markdown generator ───────────────────────────────────────────────────────

def generate_markdown(
    runs_dir: Path,
    out: TextIO | None = None,
) -> None:
    """Generate a terminal-readable markdown summary of the bench results.

    Writes to `out` (defaults to sys.stdout). No HTML tags — plain markdown
    with pipe-tables.
    """
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

    # Header
    lines.append(f"# Agent Bench Report — {ts}")
    lines.append("")
    lines.append(f"**Scenarios:** {scenario_count}  |  **Variants:** {len(variants)}")
    lines.append("")

    # Summary: per-layer pass rates
    lines.append("## Pass Rates (all cells)")
    lines.append("")
    lines.append("| Layer | Pass Rate |")
    lines.append("|-------|-----------|")
    for layer in ("outcome", "trajectory", "constraint", "robustness"):
        rate = summary[f"{layer}_pass_rate"]
        pct = f"{rate * 100:.1f}%"
        lines.append(f"| {layer.capitalize()} | {pct} |")
    lines.append("")

    # Scenario matrix
    lines.append("## Scenario Matrix")
    lines.append("")

    if not variants:
        lines.append("_No runs found._")
        lines.append("")
        print("\n".join(lines), file=out, end="")
        return

    # Table header
    header = ["Scenario"] + variants
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    # Pass counts per variant
    pass_counts: dict[str, int] = {v: 0 for v in variants}
    total = len(scenarios)

    for sc in scenarios:
        row = [f"`{sc['scenario_id']}`"]
        for var in variants:
            cell = sc["cells"].get(var)
            if cell is None:
                row.append("—")
            else:
                verdict = cell["verdict"]
                tc = cell["tool_call_count"]
                icon = "PASS" if verdict == "PASS" else ("VAR" if "VARIANCE" in verdict else "FAIL")
                sev = cell["failure_cost"]["severity"]
                sev_tag = f"[{sev}]" if sev != "low" else ""
                row.append(f"{icon} n={tc}{(' ' + sev_tag) if sev_tag else ''}")
                if verdict == "PASS":
                    pass_counts[var] += 1
        lines.append("| " + " | ".join(row) + " |")

    # Summary row
    summary_row = ["**PASS**"]
    for var in variants:
        summary_row.append(f"**{pass_counts[var]}/{total}**")
    lines.append("| " + " | ".join(summary_row) + " |")
    lines.append("")

    # Per-layer breakdown per variant
    lines.append("## Per-Layer Breakdown")
    lines.append("")

    layer_labels = [
        ("outcome", "Outcome"),
        ("trajectory", "Trajectory"),
        ("constraint", "Constraint"),
        ("robustness", "Robustness"),
    ]

    layer_header = ["Layer"] + variants
    lines.append("| " + " | ".join(layer_header) + " |")
    lines.append("|" + "|".join(["---"] * len(layer_header)) + "|")

    for layer_key, layer_label in layer_labels:
        row = [layer_label]
        for var in variants:
            # Count passes for this layer across all scenarios for this variant
            layer_passes = 0
            layer_total = 0
            for sc in scenarios:
                cell = sc["cells"].get(var)
                if cell is not None:
                    layer_total += 1
                    if cell["layers"][layer_key]["passed"]:
                        layer_passes += 1
            if layer_total == 0:
                row.append("—")
            else:
                pct = f"{layer_passes}/{layer_total}"
                row.append(pct)
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    print("\n".join(lines), file=out, end="")


# ─── JSON generator ───────────────────────────────────────────────────────────

def generate_json(
    runs_dir: Path,
    out: TextIO | None = None,
) -> None:
    """Generate the JSON-island report data as a standalone JSON document.

    The HTML report already embeds this exact structure in
    ``<script id="bench-data">``. The JSON format deliberately reuses that
    data model rather than creating a second report schema.
    """
    if out is None:
        out = sys.stdout

    cells = load_runs(runs_dir=runs_dir)
    data = _build_report_data(cells)
    print(json.dumps(data, indent=2, ensure_ascii=False), file=out)


# ─── HTML generator ───────────────────────────────────────────────────────────

def generate_html(
    runs_dir: Path,
    out_path: Path,
) -> None:
    """Generate a self-contained report.html with JSON island + vanilla JS.

    The HTML is fully self-contained — no external CSS/JS. The data is
    embedded as a JSON island for client-side rendering. The diff view
    and drill-down are implemented in inline vanilla JS.
    """
    cells = load_runs(runs_dir=runs_dir)
    data = _build_report_data(cells)

    json_island = json.dumps(data, indent=2, ensure_ascii=False)
    ts = data["latest_run_ts"] or "unknown"
    scenario_count = data["scenario_count"]
    summary = data["summary"]

    def _pct(rate: float) -> str:
        return f"{rate * 100:.1f}%"

    html_content = _build_html(
        json_island=json_island,
        ts=ts,
        scenario_count=scenario_count,
        summary=summary,
        pct_fn=_pct,
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")


def _build_html(
    json_island: str,
    ts: str,
    scenario_count: int,
    summary: dict[str, Any],
    pct_fn: Callable[[float], str],
) -> str:
    """Build the complete HTML string."""

    css = _CSS
    js = _JS
    summary_html = _build_summary_html(ts, scenario_count, summary, pct_fn)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Bench Report</title>
<style>
{css}
</style>
</head>
<body>
<div id="app">
  <header id="report-header">
    {summary_html}
  </header>

  <section id="diff-controls">
    <h2>Diff View</h2>
    <div class="diff-selectors">
      <label>Compare: <select id="diff-a"></select></label>
      <span>&rarr;</span>
      <label><select id="diff-b"></select></label>
      <button id="diff-btn">Show Diff</button>
      <button id="diff-clear-btn">Clear</button>
    </div>
    <div id="diff-summary"></div>
  </section>

  <section id="matrix-section">
    <h2>Scenario Matrix</h2>
    <div id="matrix-container"></div>
  </section>

  <section id="drilldown-section" style="display:none">
    <h2>Drill-Down: <span id="drilldown-title"></span></h2>
    <button id="drilldown-close">Close</button>
    <div id="drilldown-content"></div>
  </section>
</div>

<script type="application/json" id="bench-data">
{json_island}
</script>
<script>
{js}
</script>
</body>
</html>"""


def _build_summary_html(
    ts: str,
    scenario_count: int,
    summary: dict[str, Any],
    pct_fn: Callable[[float], str],
) -> str:
    outcome_pct = pct_fn(summary["outcome_pass_rate"])
    trajectory_pct = pct_fn(summary["trajectory_pass_rate"])
    constraint_pct = pct_fn(summary["constraint_pass_rate"])
    robustness_pct = pct_fn(summary["robustness_pass_rate"])

    return f"""<h1>Agent Bench Report</h1>
    <div class="summary-grid">
      <div class="summary-card">
        <div class="summary-label">Latest Run</div>
        <div class="summary-value">{ts}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Scenarios</div>
        <div class="summary-value">{scenario_count}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Outcome</div>
        <div class="summary-value">{outcome_pct}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Trajectory</div>
        <div class="summary-value">{trajectory_pct}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Constraint</div>
        <div class="summary-value">{constraint_pct}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Robustness</div>
        <div class="summary-value">{robustness_pct}</div>
      </div>
    </div>"""


# ─── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: #0f0f13; color: #e2e8f0; line-height: 1.5; }
#app { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }

header { margin-bottom: 2rem; }
h1 { font-size: 1.5rem; margin-bottom: 1rem; color: #f8fafc; }
h2 { font-size: 1.1rem; margin-bottom: 0.75rem; color: #94a3b8; letter-spacing: 0.05em; text-transform: uppercase; }

.summary-grid { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.summary-card { background: #1e1e2e; border: 1px solid #313244; border-radius: 8px; padding: 0.75rem 1.25rem; min-width: 140px; }
.summary-label { font-size: 0.7rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.08em; }
.summary-value { font-size: 1.1rem; font-weight: 600; color: #c0caf5; margin-top: 0.2rem; }

section { margin-bottom: 2rem; }

.diff-selectors { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
select { background: #1e1e2e; color: #e2e8f0; border: 1px solid #313244; border-radius: 4px; padding: 0.3rem 0.5rem; font-size: 0.9rem; }
button { background: #3b4261; color: #e2e8f0; border: 1px solid #444b6e; border-radius: 4px; padding: 0.3rem 0.8rem; cursor: pointer; font-size: 0.85rem; }
button:hover { background: #444b6e; }
#diff-summary { font-size: 0.9rem; color: #94a3b8; }

table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th { background: #1e1e2e; color: #94a3b8; font-weight: 600; padding: 0.5rem 0.75rem; text-align: left; border-bottom: 2px solid #313244; position: sticky; top: 0; z-index: 1; }
td { padding: 0.4rem 0.75rem; border-bottom: 1px solid #1e1e2e; vertical-align: top; }
tr:hover td { background: #1e1e2e; }

.verdict-pass { color: #a6e3a1; font-weight: 600; }
.verdict-fail { color: #f38ba8; font-weight: 600; }
.verdict-variance { color: #f9e2af; font-weight: 600; }
.verdict-missing { color: #45475a; }

.sev-low { }
.sev-medium { border-left: 3px solid #f9e2af; }
.sev-high { border-left: 3px solid #fab387; }
.sev-critical { border-left: 3px solid #f38ba8; font-weight: 700; }

.cell-detail { font-size: 0.75rem; color: #64748b; margin-top: 0.15rem; }
.layer-pills { display: flex; gap: 3px; margin-top: 0.2rem; flex-wrap: wrap; }
.layer-pill { font-size: 0.65rem; padding: 1px 4px; border-radius: 3px; }
.layer-pass { background: #1e3a2e; color: #a6e3a1; }
.layer-fail { background: #3a1e2e; color: #f38ba8; }

.diff-regression td { background: #2a1e2e !important; }
.diff-improvement td { background: #1e2a1e !important; }

.clickable-cell { cursor: pointer; }
.clickable-cell:hover { text-decoration: underline; text-decoration-style: dotted; }

#drilldown-section { background: #1e1e2e; border: 1px solid #313244; border-radius: 8px; padding: 1.25rem; }
#drilldown-close { margin-bottom: 1rem; }
#drilldown-title { color: #c0caf5; }
.drilldown-turn { border: 1px solid #313244; border-radius: 6px; padding: 0.75rem; margin-bottom: 0.5rem; }
.turn-role { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.25rem; font-weight: 600; }
.role-user { color: #89b4fa; }
.role-assistant { color: #a6e3a1; }
.role-tool { color: #f9e2af; }
.turn-content { font-size: 0.85rem; white-space: pre-wrap; word-break: break-word; }
.turn-tool-call { background: #12121c; border-radius: 4px; padding: 0.5rem; margin-top: 0.5rem; font-size: 0.8rem; font-family: monospace; }
.score-breakdown { margin-top: 1rem; }
.score-breakdown h4 { font-size: 0.85rem; color: #94a3b8; margin-bottom: 0.5rem; }
.score-row { display: flex; align-items: baseline; gap: 0.5rem; margin-bottom: 0.25rem; font-size: 0.85rem; }
.score-layer-name { width: 90px; color: #64748b; font-size: 0.75rem; text-transform: uppercase; }
.score-layer-detail { color: #94a3b8; font-size: 0.8rem; }
"""


# ─── JS ───────────────────────────────────────────────────────────────────────

_JS = r"""
(function() {
  const raw = document.getElementById('bench-data').textContent;
  const data = JSON.parse(raw);

  const { scenarios, variants } = data;

  // ── Populate diff selects ────────────────────────────────────────────────
  const selA = document.getElementById('diff-a');
  const selB = document.getElementById('diff-b');
  variants.forEach((v, i) => {
    const optA = new Option(v, v); selA.appendChild(optA);
    const optB = new Option(v, v); selB.appendChild(optB);
    if (i === 0) selA.value = v;
    if (i === 1) selB.value = v;
  });

  // ── Build matrix table ───────────────────────────────────────────────────
  const container = document.getElementById('matrix-container');
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  const thScenario = document.createElement('th');
  thScenario.textContent = 'Scenario';
  headerRow.appendChild(thScenario);
  variants.forEach(v => {
    const th = document.createElement('th');
    th.textContent = v;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  scenarios.forEach(sc => {
    const tr = document.createElement('tr');
    tr.dataset.scenarioId = sc.scenario_id;

    const tdName = document.createElement('td');
    tdName.textContent = sc.scenario_id;
    tr.appendChild(tdName);

    variants.forEach(v => {
      const td = document.createElement('td');
      const cell = sc.cells[v];
      if (!cell) {
        td.textContent = '—';
        td.className = 'verdict-missing';
      } else {
        const verdict = cell.verdict;
        const sev = cell.failure_cost.severity;

        // Verdict line — no <br><sub> inside td; use separate child elements
        const verdictSpan = document.createElement('span');
        verdictSpan.textContent = verdict === 'PASS' ? '✓ PASS'
          : verdict.includes('VARIANCE') ? '~ VAR'
          : '✗ FAIL';
        verdictSpan.className = verdict === 'PASS' ? 'verdict-pass'
          : verdict.includes('VARIANCE') ? 'verdict-variance'
          : 'verdict-fail';
        td.appendChild(verdictSpan);

        // Tool count detail — separate div, not <br><sub>
        const detail = document.createElement('div');
        detail.className = 'cell-detail';
        detail.textContent = `n=${cell.tool_call_count}`;
        td.appendChild(detail);

        // Layer pills — separate div
        const pills = document.createElement('div');
        pills.className = 'layer-pills';
        ['outcome','trajectory','constraint','robustness'].forEach(layer => {
          const pill = document.createElement('span');
          pill.className = 'layer-pill ' + (cell.layers[layer].passed ? 'layer-pass' : 'layer-fail');
          pill.title = layer + ': ' + cell.layers[layer].detail;
          pill.textContent = layer[0].toUpperCase();
          pills.appendChild(pill);
        });
        td.appendChild(pills);

        // Severity class on TD itself (not inside a nested tag)
        td.classList.add('sev-' + sev);
        td.classList.add('clickable-cell');

        // Click → drill-down
        td.addEventListener('click', () => showDrilldown(sc.scenario_id, v, cell));
      }
      tr.appendChild(td);
    });

    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.appendChild(table);

  // ── Diff view ────────────────────────────────────────────────────────────
  document.getElementById('diff-btn').addEventListener('click', () => {
    const a = selA.value;
    const b = selB.value;
    let regressions = 0, improvements = 0;

    // Clear previous highlights
    tbody.querySelectorAll('tr').forEach(tr => {
      tr.classList.remove('diff-regression', 'diff-improvement');
    });

    scenarios.forEach(sc => {
      const cellA = sc.cells[a];
      const cellB = sc.cells[b];
      if (!cellA || !cellB) return;
      if (cellA.verdict === cellB.verdict) return;

      const tr = tbody.querySelector(`tr[data-scenario-id="${sc.scenario_id}"]`);
      if (!tr) return;

      if (cellA.verdict === 'PASS' && cellB.verdict !== 'PASS') {
        tr.classList.add('diff-regression');
        regressions++;
      } else if (cellA.verdict !== 'PASS' && cellB.verdict === 'PASS') {
        tr.classList.add('diff-improvement');
        improvements++;
      }
    });

    const summaryEl = document.getElementById('diff-summary');
    summaryEl.textContent = `${a} → ${b}: ${regressions} regression(s), ${improvements} improvement(s)`;
  });

  document.getElementById('diff-clear-btn').addEventListener('click', () => {
    tbody.querySelectorAll('tr').forEach(tr => {
      tr.classList.remove('diff-regression', 'diff-improvement');
    });
    document.getElementById('diff-summary').textContent = '';
  });

  // ── Drill-down panel ─────────────────────────────────────────────────────
  function showDrilldown(scenarioId, variantId, cell) {
    const section = document.getElementById('drilldown-section');
    const title = document.getElementById('drilldown-title');
    const content = document.getElementById('drilldown-content');

    title.textContent = `${scenarioId} / ${variantId}`;
    content.innerHTML = '';

    // Score breakdown
    const scoreDiv = document.createElement('div');
    scoreDiv.className = 'score-breakdown';
    const scoreH4 = document.createElement('h4');
    scoreH4.textContent = 'Score';
    scoreDiv.appendChild(scoreH4);

    ['outcome','trajectory','constraint','robustness'].forEach(layer => {
      const row = document.createElement('div');
      row.className = 'score-row';
      const nameEl = document.createElement('span');
      nameEl.className = 'score-layer-name';
      nameEl.textContent = layer;
      const passEl = document.createElement('span');
      passEl.className = cell.layers[layer].passed ? 'verdict-pass' : 'verdict-fail';
      passEl.textContent = cell.layers[layer].passed ? '✓' : '✗';
      const detailEl = document.createElement('span');
      detailEl.className = 'score-layer-detail';
      detailEl.textContent = cell.layers[layer].detail;
      row.appendChild(nameEl);
      row.appendChild(passEl);
      row.appendChild(detailEl);
      scoreDiv.appendChild(row);
    });

    // failure_cost
    const fc = cell.failure_cost;
    const fcRow = document.createElement('div');
    fcRow.className = 'score-row';
    fcRow.style.marginTop = '0.5rem';
    const fcLabel = document.createElement('span');
    fcLabel.className = 'score-layer-name';
    fcLabel.textContent = 'failure_cost';
    const fcVal = document.createElement('span');
    fcVal.className = 'score-layer-detail';
    fcVal.textContent = `severity=${fc.severity} customer_visible=${fc.customer_visible} reversible=${fc.reversible}`;
    fcRow.appendChild(fcLabel);
    fcRow.appendChild(fcVal);
    scoreDiv.appendChild(fcRow);

    content.appendChild(scoreDiv);

    // Trace turns
    const turnsH4 = document.createElement('h4');
    turnsH4.textContent = 'Trace';
    turnsH4.style.margin = '1rem 0 0.5rem';
    content.appendChild(turnsH4);

    (cell.trace.turns || []).forEach(turn => {
      const turnDiv = document.createElement('div');
      turnDiv.className = 'drilldown-turn';

      const roleDiv = document.createElement('div');
      roleDiv.className = `turn-role role-${turn.role}`;
      roleDiv.textContent = turn.role;
      turnDiv.appendChild(roleDiv);

      if (turn.content) {
        const contentDiv = document.createElement('div');
        contentDiv.className = 'turn-content';
        contentDiv.textContent = turn.content;
        turnDiv.appendChild(contentDiv);
      }

      if (turn.tool_calls && turn.tool_calls.length > 0) {
        turn.tool_calls.forEach(tc => {
          const tcDiv = document.createElement('div');
          tcDiv.className = 'turn-tool-call';
          // Normalize both OpenAI wire shape and flat shape
          const name = (tc.function && tc.function.name) || tc.name || '?';
          const args = (tc.function && tc.function.arguments) || tc.args || {};
          tcDiv.textContent = `call: ${name}(${typeof args === 'string' ? args : JSON.stringify(args)})`;
          turnDiv.appendChild(tcDiv);
        });
      }

      content.appendChild(turnDiv);
    });

    section.style.display = 'block';
    section.scrollIntoView({ behavior: 'smooth' });
  }

  document.getElementById('drilldown-close').addEventListener('click', () => {
    document.getElementById('drilldown-section').style.display = 'none';
  });
})();
"""
