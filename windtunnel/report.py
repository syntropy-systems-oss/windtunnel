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
  - One cell per (scenario_id, variant_id). When the CLI ledger contains the
    aggregate for a multi-run batch, the cell verdict and layer rates come from
    that aggregate; the latest trace in the batch remains the drill-down sample.
    Trace-only directories without a ledger retain the historical latest-run
    fallback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from windtunnel._report.load import (
    _load_latest_aggregates as _load_latest_aggregates_impl,
)
from windtunnel._report.load import (
    load_runs as _load_runs_impl,
)
from windtunnel._report.model import (
    _build_report_data as _build_report_data_impl,
)
from windtunnel._report.model import (
    _cell_from_run as _cell_from_run_impl,
)
from windtunnel._report.model import (
    _tool_call_count as _tool_call_count_impl,
)
from windtunnel._report.model import (
    _verdict_rank as _verdict_rank_impl,
)
from windtunnel._report.model import (
    compute_diff as _compute_diff_impl,
)
from windtunnel._report.text import (
    generate_json as _generate_json_impl,
)
from windtunnel._report.text import (
    generate_markdown as _generate_markdown_impl,
)

# Compatibility facade: report consumers keep the same imports while loading,
# modeling, and text rendering evolve independently behind this module.
load_runs = _load_runs_impl
_load_latest_aggregates = _load_latest_aggregates_impl
_tool_call_count = _tool_call_count_impl
_cell_from_run = _cell_from_run_impl
_build_report_data = _build_report_data_impl
compute_diff = _compute_diff_impl
_verdict_rank = _verdict_rank_impl
generate_markdown = _generate_markdown_impl
generate_json = _generate_json_impl

# ─── HTML renderer ────────────────────────────────────────────────────────────

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
    integrity_pct = pct_fn(summary["integrity_pass_rate"])
    robustness_rate = summary["robustness_pass_rate"]
    robustness_pct = pct_fn(robustness_rate) if robustness_rate is not None else "N/A"
    failure_risk = f"{float(summary['total_failure_risk']):.2f}"

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
        <div class="summary-label">Integrity</div>
        <div class="summary-value">{integrity_pct}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Robustness cases</div>
        <div class="summary-value">{robustness_pct}</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Failure risk</div>
        <div class="summary-value">{failure_risk}</div>
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
          : verdict === 'INVALID' ? '! INVALID'
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
        ['outcome','trajectory','constraint','integrity'].forEach(layer => {
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

      const verdictRank = verdict => verdict === 'PASS' ? 2
        : verdict.includes('VARIANCE') ? 1
        : 0;
      if (verdictRank(cellB.verdict) < verdictRank(cellA.verdict)) {
        tr.classList.add('diff-regression');
        regressions++;
      } else {
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

    ['outcome','trajectory','constraint','integrity'].forEach(layer => {
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

    if (cell.robustness.applicable) {
      const robustRow = document.createElement('div');
      robustRow.className = 'score-row';
      const robustName = document.createElement('span');
      robustName.className = 'score-layer-name';
      robustName.textContent = 'robustness';
      const robustValue = document.createElement('span');
      robustValue.className = cell.robustness.passed ? 'verdict-pass' : 'verdict-fail';
      robustValue.textContent = cell.robustness.pass_rate === null
        ? 'invalid experiment'
        : `${Math.round(cell.robustness.pass_rate * 100)}% gate pass`;
      robustRow.appendChild(robustName);
      robustRow.appendChild(robustValue);
      scoreDiv.appendChild(robustRow);
    }

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
    fcVal.textContent = `severity=${fc.severity} risk_weight=${fc.risk_weight} customer_visible=${fc.customer_visible} reversible=${fc.reversible}`;
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
