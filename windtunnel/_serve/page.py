"""The `wt serve` viewer page — one self-contained HTML document.

Same stance as the report renderer (windtunnel/report.py): inline CSS +
vanilla JS, zero external requests — no CDN scripts, no webfonts, nothing
fetched beyond this server's own /api/* endpoints. Dark-mode aware via
CSS variables and prefers-color-scheme.
"""

from __future__ import annotations

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #f8fafc; --panel: #ffffff; --border: #e2e8f0; --text: #1e293b;
  --muted: #64748b; --accent: #2563eb; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --pass-bg: #dcfce7; --pass-fg: #166534; --fail-bg: #fee2e2; --fail-fg: #991b1b;
  --variance-bg: #fef9c3; --variance-fg: #854d0e; --invalid-bg: #e2e8f0; --invalid-fg: #475569;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f13; --panel: #1e1e2e; --border: #313244; --text: #e2e8f0;
    --muted: #94a3b8; --accent: #7aa2f7;
    --pass-bg: #14351f; --pass-fg: #4ade80; --fail-bg: #3b1519; --fail-fg: #f87171;
    --variance-bg: #3a3116; --variance-fg: #facc15; --invalid-bg: #26262f; --invalid-fg: #94a3b8;
  }
}
body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }
#app { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1rem; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 0.75rem; }
header { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
header .meta { color: var(--muted); font-size: 0.85rem; font-family: var(--mono); }
nav { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border); }
nav button { background: none; border: none; color: var(--muted); font-size: 0.95rem; padding: 0.5rem 1rem; cursor: pointer; border-bottom: 2px solid transparent; }
nav button.active { color: var(--text); border-bottom-color: var(--accent); }
section.tab { display: none; }
section.tab.active { display: block; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
.group-head { display: flex; gap: 0.75rem; align-items: baseline; flex-wrap: wrap; margin-bottom: 0.5rem; }
.group-head .label { font-weight: 600; font-family: var(--mono); }
.group-head .muted, .muted { color: var(--muted); font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 0.3rem 0.6rem; border-bottom: 1px solid var(--border); }
td { padding: 0.35rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
tr.run-row { cursor: pointer; }
tr.run-row:hover td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.chip { display: inline-block; padding: 0.05rem 0.55rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; font-family: var(--mono); white-space: nowrap; }
.chip.PASS { background: var(--pass-bg); color: var(--pass-fg); }
.chip.FAIL { background: var(--fail-bg); color: var(--fail-fg); }
.chip.PASS_WITH_VARIANCE { background: var(--variance-bg); color: var(--variance-fg); }
.chip.INVALID, .chip.UNKNOWN { background: var(--invalid-bg); color: var(--invalid-fg); }
.layers { display: flex; gap: 0.3rem; flex-wrap: wrap; }
.layer-chip { font-family: var(--mono); font-size: 0.72rem; padding: 0.05rem 0.4rem; border-radius: 4px; background: var(--invalid-bg); color: var(--invalid-fg); }
.layer-chip.ok { background: var(--pass-bg); color: var(--pass-fg); }
.layer-chip.bad { background: var(--fail-bg); color: var(--fail-fg); }
.mono { font-family: var(--mono); }
.why { border-left: 3px solid var(--fail-fg); padding: 0.4rem 0.75rem; margin: 0.5rem 0; background: var(--fail-bg); color: var(--fail-fg); border-radius: 0 6px 6px 0; font-size: 0.9rem; }
.why.ok { border-left-color: var(--pass-fg); background: var(--pass-bg); color: var(--pass-fg); }
.layer-detail { margin: 0.25rem 0; font-size: 0.87rem; display: flex; gap: 0.5rem; align-items: baseline; }
.turn { border: 1px solid var(--border); border-radius: 6px; margin: 0.5rem 0; overflow: hidden; }
.turn .role { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); padding: 0.25rem 0.75rem; background: color-mix(in srgb, var(--border) 40%, transparent); }
.turn .content { padding: 0.5rem 0.75rem; white-space: pre-wrap; font-size: 0.88rem; overflow-wrap: anywhere; }
.tool-call { font-family: var(--mono); font-size: 0.8rem; padding: 0.25rem 0.75rem; border-top: 1px dashed var(--border); color: var(--muted); overflow-wrap: anywhere; }
details { margin: 0.4rem 0; }
summary { cursor: pointer; }
summary .name { font-family: var(--mono); font-weight: 600; }
.tagchip { display: inline-block; font-family: var(--mono); font-size: 0.72rem; background: var(--invalid-bg); color: var(--invalid-fg); border-radius: 4px; padding: 0.05rem 0.4rem; margin-left: 0.3rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 1rem; font-size: 0.87rem; margin: 0.5rem 0 0.25rem; }
dt { color: var(--muted); }
dd { font-family: var(--mono); overflow-wrap: anywhere; }
pre.stream { font-family: var(--mono); font-size: 0.82rem; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow-y: auto; padding: 0.5rem 0.75rem; background: color-mix(in srgb, var(--border) 30%, transparent); border-radius: 6px; }
button.plain { background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 0.25rem 0.75rem; cursor: pointer; font-size: 0.85rem; }
#drilldown:empty { display: none; }
.empty { color: var(--muted); padding: 1rem 0; }
"""

_JS = """
'use strict';
const $ = (sel) => document.querySelector(sel);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
  (ch) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const chip = (verdict) => {
  const known = ['PASS', 'FAIL', 'PASS_WITH_VARIANCE', 'INVALID'];
  const cls = known.includes(verdict) ? verdict : 'UNKNOWN';
  return `<span class="chip ${cls}">${esc(verdict ?? 'UNKNOWN')}</span>`;
};
const pct = (rate) => (typeof rate === 'number' ? Math.round(rate * 100) + '%' : '—');
async function fetchJSON(url) {
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('section.tab').forEach((s) => s.classList.toggle('active', s.id === 'tab-' + name));
  if (name === 'live') startLive();
}

// ── runs dashboard ───────────────────────────────────────────────────────────
let ledgerRows = [];
async function refreshLedger() {
  try {
    const data = await fetchJSON('/api/ledger');
    ledgerRows = data.rows;
    renderLedger(data);
  } catch (err) {
    $('#ledger').innerHTML = `<div class="empty">could not load ledger: ${esc(err.message)}</div>`;
  }
}
function layerChips(rates) {
  if (!rates) return '';
  return '<span class="layers">' + ['outcome', 'trajectory', 'constraint', 'integrity'].map((layer) => {
    const rate = rates[layer];
    const cls = rate === 1 ? 'ok' : (typeof rate === 'number' ? 'bad' : '');
    return `<span class="layer-chip ${cls}" title="${esc(layer)} pass rate">${esc(layer[0].toUpperCase())} ${pct(rate)}</span>`;
  }).join('') + '</span>';
}
function renderLedger(data) {
  const groups = new Map();
  for (const row of ledgerRows) {
    const key = `${row.label ?? ''}\\u0000${row.pack ?? ''}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const parts = [];
  for (const [key, rows] of groups) {
    const [label, pack] = key.split('\\u0000');
    const counts = {};
    for (const row of rows) counts[row.verdict] = (counts[row.verdict] || 0) + 1;
    const summary = Object.entries(counts).map(([verdict, n]) => `${n} ${verdict}`).join(' · ');
    parts.push(`<div class="panel">
      <div class="group-head">
        <span class="label">${esc(label)}</span>
        <span class="muted">pack: ${esc(pack || '—')}</span>
        <span class="muted">${esc(summary)}</span>
      </div>
      <table><thead><tr>
        <th>when (UTC)</th><th>scenario</th><th>verdict</th><th>runs</th><th>layers</th><th>git</th>
      </tr></thead><tbody>` +
      rows.map((row, i) => `<tr class="run-row" data-key="${esc(key)}" data-index="${i}">
        <td class="mono">${esc(row.ts ?? '')}</td>
        <td class="mono">${esc(row.scenario_id ?? '')}</td>
        <td>${chip(row.verdict)}</td>
        <td class="mono">${esc(row.passed ?? '?')}/${esc(row.runs ?? '?')}</td>
        <td>${layerChips(row.layer_pass_rates)}</td>
        <td class="mono">${esc(row.git_sha ?? '')}</td>
      </tr>`).join('') + '</tbody></table></div>');
  }
  $('#ledger').innerHTML = parts.join('') ||
    '<div class="empty">No ledger rows yet — run a sweep with <span class="mono">wt run</span> and this page will pick it up.</div>';
  $('#ledger-note').textContent = data.skipped ? `${data.skipped} malformed ledger line(s) skipped` : '';
  document.querySelectorAll('tr.run-row').forEach((tr) => tr.addEventListener('click', () => {
    const rows = groups.get(tr.dataset.key);
    if (rows) openRow(rows[Number(tr.dataset.index)]);
  }));
}
async function openRow(row) {
  const runIds = row.run_ids || [];
  const container = $('#drilldown');
  container.innerHTML = `<div class="panel"><h2>Run drill-down</h2>
    <div class="group-head"><span class="label">${esc(row.scenario_id)}</span>${chip(row.verdict)}
      <span class="muted">label ${esc(row.label ?? '')} · ${esc(runIds.length)} run(s)</span>
      <button class="plain" onclick="this.closest('.panel').parentElement.innerHTML=''">close</button>
    </div><div id="drill-runs">loading…</div></div>`;
  container.scrollIntoView({behavior: 'smooth'});
  const rendered = [];
  for (const runId of runIds.slice(0, 10)) {
    try {
      rendered.push(renderRun(runId, await fetchJSON('/api/run/' + encodeURIComponent(runId))));
    } catch (err) {
      rendered.push(`<div class="empty">run ${esc(runId)}: ${esc(err.message)}</div>`);
    }
  }
  if (runIds.length > 10) rendered.push(`<div class="muted">…${runIds.length - 10} more run(s) not shown</div>`);
  $('#drill-runs').innerHTML = rendered.join('<hr style="border-color: var(--border)">') ||
    '<div class="empty">this ledger row carries no run ids</div>';
}
function renderRun(runId, payload) {
  const trace = payload.trace || {};
  const score = payload.score;
  const parts = [`<div class="group-head" style="margin-top:0.75rem">
    <span class="mono muted">run ${esc(runId.slice(0, 8))} · ${esc(payload.trace_file ?? '')}</span>
    <span class="mono muted">${esc(trace.model ?? '')} ${esc(trace.quant ?? '')}</span></div>`];
  if (score) {
    parts.push(chip(score.verdict));
    const layers = ['outcome', 'trajectory', 'constraint', 'integrity'];
    const failing = layers.filter((layer) => score[layer] && score[layer].passed === false);
    // The "why it failed" line, front and center.
    for (const layer of failing) {
      parts.push(`<div class="why"><strong>${esc(layer)}:</strong> ${esc(score[layer].detail)}</div>`);
    }
    if (!failing.length) parts.push('<div class="why ok">all scored layers passed</div>');
    parts.push(layers.map((layer) => {
      const result = score[layer];
      if (!result) return '';
      const cls = result.passed ? 'ok' : 'bad';
      return `<div class="layer-detail"><span class="layer-chip ${cls}">${esc(layer)}</span><span>${esc(result.detail)}</span></div>`;
    }).join(''));
    if (score.gate_layers) parts.push(`<div class="muted">gate: ${esc(score.gate_layers.join(' + '))}</div>`);
  } else {
    parts.push('<div class="empty">no .score.json sidecar beside this trace</div>');
  }
  for (const turn of trace.turns || []) {
    const toolCalls = (turn.tool_calls || []).map((call) => {
      const name = call.function?.name ?? call.name ?? '?';
      const args = call.function?.arguments ?? JSON.stringify(call.args ?? {});
      return `<div class="tool-call">tool_call ${esc(name)}(${esc(args)})</div>`;
    }).join('');
    const toolResults = (turn.tool_results || []).map((result) =>
      `<div class="tool-call">tool_result ${esc(JSON.stringify(result))}</div>`).join('');
    parts.push(`<div class="turn"><div class="role">${esc(turn.role)}</div>
      <div class="content">${esc(turn.content)}</div>${toolCalls}${toolResults}</div>`);
  }
  const witnessed = trace.mcp_calls || [];
  if (witnessed.length) {
    parts.push('<div class="muted">tool calls witnessed at the mock server:</div>' + witnessed.map((call) =>
      `<div class="tool-call">${esc(call.tool_name)} ← ${esc(JSON.stringify(call.args ?? {}))}</div>`).join(''));
  }
  if ((trace.worker_warnings || []).length) {
    parts.push(`<div class="muted">warnings: ${esc(trace.worker_warnings.join(' | '))}</div>`);
  }
  return parts.join('');
}

// ── scenario browser ─────────────────────────────────────────────────────────
async function loadScenarios() {
  try {
    const data = await fetchJSON('/api/scenarios');
    renderScenarios(data.packs || []);
  } catch (err) {
    $('#scenarios').innerHTML = `<div class="empty">could not load scenarios: ${esc(err.message)}</div>`;
  }
}
const dl = (pairs) => '<dl>' + pairs.filter(([, v]) => v !== '' && v != null)
  .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('') + '</dl>';
function renderScenarios(packs) {
  $('#scenarios').innerHTML = packs.map((pack) => {
    const surface = pack.tool_surface || {};
    const tools = surface.tools ? surface.tools.map((t) => `<span class="tagchip">${esc(t)}</span>`).join('') : esc(surface.detail || '');
    return `<div class="panel">
      <div class="group-head"><span class="label">${esc(pack.name)}</span>
        ${pack.owner ? `<span class="muted">owner: ${esc(pack.owner)}</span>` : ''}
        ${pack.transport_only ? '<span class="tagchip">transport-only</span>' : ''}
        <span class="muted">${pack.scenarios.length} scenario(s)</span></div>
      <div class="muted">tool surface: ${tools}</div>` +
      pack.scenarios.map((scenario) => `<details>
        <summary><span class="name">${esc(scenario.name)}</span>` +
          scenario.tags.map((tag) => `<span class="tagchip">${esc(tag)}</span>`).join('') +
          ` ${scenario.perturbations.length ? '<span class="tagchip">perturbed</span>' : ''}</summary>` +
        dl([
          ['user turns', (scenario.user_turns.length ? scenario.user_turns : [scenario.scored_prompt])
            .map((turn) => esc(turn)).join('<br>')],
          ['target facts', scenario.target_facts.map((group) => esc(group.join(' | '))).join(' AND ')],
          ['target numbers', scenario.target_numbers.map((n) => esc(n.value + (n.unit ? ' ' + n.unit : ''))).join(', ')],
          ['forbidden facts', esc(scenario.forbidden_facts.join(', '))],
          ['must_call', scenario.must_call.map((entry) => esc(Array.isArray(entry) ? entry.join(' | ') : entry)).join(' → ')],
          ['forbidden_calls', esc(scenario.forbidden_calls.join(', '))],
          ['order matters', scenario.order_matters ? 'yes' : ''],
          ['requires tool use', scenario.requires_tool_use ? 'yes' : ''],
          ['custom outcome_fn', scenario.has_outcome_fn ? 'yes' : ''],
          ['trajectory checks', esc(scenario.trajectory_checks.join(', '))],
          ['policies', scenario.policies.map((p) => esc(p.name + (p.effect_class ? ` [${p.effect_class}]` : ''))).join(', ')],
          ['gate layers', esc(scenario.gate_layers.join(' + '))],
          ['perturbations', scenario.perturbations.map((p) => esc(p.type + (p.marker ? ` (${p.marker})` : ''))).join('<br>')],
          ['requires tools', esc(scenario.requires_tools.join(', '))],
          ['requires files', esc(scenario.requires_files.join(', '))],
          ['failure cost', esc(`${scenario.failure_cost.severity} · risk ${scenario.failure_cost.risk_weight}` +
            (scenario.failure_cost.customer_visible ? ' · customer-visible' : '') +
            (scenario.failure_cost.reversible ? '' : ' · irreversible'))],
          ['variance allowed', scenario.variance_allowed ? 'yes' : ''],
          ['reference cases', scenario.reference_case_count ? String(scenario.reference_case_count) : ''],
        ]) + '</details>').join('') + '</div>';
  }).join('') || '<div class="empty">no scenario packs discovered</div>';
}

// ── live watch ───────────────────────────────────────────────────────────────
let liveSource = null;
const liveBlocks = new Map();
function startLive() {
  if (liveSource || !window.META || !window.META.live_glob) return;
  liveSource = new EventSource('/api/live');
  $('#live-status').textContent = 'tailing ' + window.META.live_glob;
  liveSource.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    appendLive(payload.file, payload.line);
  };
  liveSource.onerror = () => { $('#live-status').textContent = 'stream disconnected — retrying…'; };
}
function deltaText(obj) {
  // Generic streaming-text detection: an event whose type mentions a delta
  // and that carries a string payload renders as appended text.
  if (typeof obj !== 'object' || obj === null) return null;
  const kind = String(obj.type ?? obj.event ?? '');
  const text = [obj.text, obj.delta, obj.content].find((v) => typeof v === 'string');
  if (kind.includes('delta') && text !== undefined) return text;
  return null;
}
function appendLive(file, line) {
  let block = liveBlocks.get(file);
  if (!block) {
    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.innerHTML = `<div class="group-head"><span class="label">${esc(file)}</span></div><pre class="stream"></pre>`;
    $('#live-files').prepend(panel);
    block = panel.querySelector('pre.stream');
    liveBlocks.set(file, block);
  }
  let parsed = null;
  try { parsed = JSON.parse(line); } catch { /* raw line */ }
  const delta = deltaText(parsed);
  if (delta !== null) {
    block.append(document.createTextNode(delta));
  } else {
    if (block.childNodes.length) block.append(document.createTextNode('\\n'));
    block.append(document.createTextNode(line));
  }
  while (block.childNodes.length > 4000) block.removeChild(block.firstChild);
  block.scrollTop = block.scrollHeight;
}

// ── boot ─────────────────────────────────────────────────────────────────────
async function boot() {
  document.querySelectorAll('nav button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  try {
    window.META = await fetchJSON('/api/meta');
    $('#meta').textContent = `runs: ${window.META.runs_dir} · wind tunnel ${window.META.wt_version}`;
    if (!window.META.live_glob) $('#live-status').textContent =
      'live watch is off — restart with: wt serve --live-glob "path/to/*.jsonl"';
  } catch { /* header stays minimal */ }
  await refreshLedger();
  await loadScenarios();
  setInterval(refreshLedger, 3000);
}
boot();
"""

PAGE_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wind Tunnel — run viewer</title>
<style>
{_CSS}
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>Wind Tunnel run viewer</h1>
    <span class="meta" id="meta"></span>
  </header>
  <nav>
    <button data-tab="runs" class="active">Runs</button>
    <button data-tab="scenarios">Scenarios</button>
    <button data-tab="live">Live</button>
  </nav>

  <section class="tab active" id="tab-runs">
    <div id="drilldown"></div>
    <h2>Sweep ledger <span class="muted" id="ledger-note"></span></h2>
    <div id="ledger"><div class="empty">loading…</div></div>
  </section>

  <section class="tab" id="tab-scenarios">
    <h2>Scenario packs</h2>
    <div id="scenarios"><div class="empty">loading…</div></div>
  </section>

  <section class="tab" id="tab-live">
    <h2>Live watch <span class="muted" id="live-status"></span></h2>
    <div id="live-files"></div>
  </section>
</div>
<script>
{_JS}
</script>
</body>
</html>"""
