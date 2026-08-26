"""The `wt serve` viewer page — one self-contained HTML document.

Same stance as the report renderer (windtunnel/report.py): inline CSS +
vanilla JS, zero external requests — no CDN scripts, no webfonts, nothing
fetched beyond this server's own /api/* endpoints. Dark-mode aware via
CSS variables and prefers-color-scheme.

Two client-side routes (hash routing, deep-linkable):
    #/            the dashboard: Runs / Scenarios / Live tabs
    #/run/<id>    one run as a full screen — scenario contract panel beside
                  the transcript, failing layer details as the banner, and
                  span-level evidence highlighting from /api/run/<id>/evidence
"""

from __future__ import annotations

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #f8fafc; --panel: #ffffff; --border: #e2e8f0; --text: #1e293b;
  --muted: #64748b; --accent: #2563eb; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --pass-bg: #dcfce7; --pass-fg: #166534; --fail-bg: #fee2e2; --fail-fg: #991b1b;
  --variance-bg: #fef9c3; --variance-fg: #854d0e; --invalid-bg: #e2e8f0; --invalid-fg: #475569;
  --hl-good-bg: #bbf7d0; --hl-good-fg: #14532d; --hl-bad-bg: #fecaca; --hl-bad-fg: #7f1d1d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f13; --panel: #1e1e2e; --border: #313244; --text: #e2e8f0;
    --muted: #94a3b8; --accent: #7aa2f7;
    --pass-bg: #14351f; --pass-fg: #4ade80; --fail-bg: #3b1519; --fail-fg: #f87171;
    --variance-bg: #3a3116; --variance-fg: #facc15; --invalid-bg: #26262f; --invalid-fg: #94a3b8;
    --hl-good-bg: #14532d; --hl-good-fg: #bbf7d0; --hl-bad-bg: #7f1d1d; --hl-bad-fg: #fecaca;
  }
}
body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }
#app { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1rem; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 0.75rem; }
h3 { font-size: 0.85rem; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; margin: 0.9rem 0 0.35rem; }
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
.layer-chip { font-family: var(--mono); font-size: 0.72rem; padding: 0.05rem 0.4rem; border-radius: 4px; background: var(--invalid-bg); color: var(--invalid-fg); border: none; cursor: default; }
.layer-chip.ok { background: var(--pass-bg); color: var(--pass-fg); }
.layer-chip.bad { background: var(--fail-bg); color: var(--fail-fg); }
button.layer-chip { cursor: pointer; }
.mono { font-family: var(--mono); }
.why { border-left: 3px solid var(--fail-fg); padding: 0.4rem 0.75rem; margin: 0.5rem 0; background: var(--fail-bg); color: var(--fail-fg); border-radius: 0 6px 6px 0; font-size: 0.9rem; overflow-wrap: anywhere; }
.why.ok { border-left-color: var(--pass-fg); background: var(--pass-bg); color: var(--pass-fg); }
.layer-detail { margin: 0.25rem 0; font-size: 0.87rem; display: flex; gap: 0.5rem; align-items: baseline; }
.turn { border: 1px solid var(--border); border-radius: 6px; margin: 0.5rem 0; overflow: hidden; }
.turn .role { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); padding: 0.25rem 0.75rem; background: color-mix(in srgb, var(--border) 40%, transparent); }
.turn .content { padding: 0.5rem 0.75rem; white-space: pre-wrap; font-size: 0.88rem; overflow-wrap: anywhere; }
/* Baseline transcript is NEUTRAL: satisfied/violated status at rest lives on
   the contract side only. Green/red appears here solely as .illum-* classes
   applied by hover/lock illumination from a contract entry. */
.tool-call { font-family: var(--mono); font-size: 0.8rem; padding: 0.25rem 0.75rem; border-top: 1px dashed var(--border); color: var(--muted); overflow-wrap: anywhere; }
mark.hl-good { background: var(--hl-good-bg); color: var(--hl-good-fg); border-radius: 3px; padding: 0 1px; }
mark.hl-bad { background: var(--hl-bad-bg); color: var(--hl-bad-fg); border-radius: 3px; padding: 0 1px; text-decoration: underline wavy; }
details { margin: 0.4rem 0; }
summary { cursor: pointer; }
summary .name { font-family: var(--mono); font-weight: 600; }
.tagchip { display: inline-block; font-family: var(--mono); font-size: 0.72rem; background: var(--invalid-bg); color: var(--invalid-fg); border-radius: 4px; padding: 0.05rem 0.4rem; margin-left: 0.3rem; }
.tagchip.good { background: var(--pass-bg); color: var(--pass-fg); }
.tagchip.bad { background: var(--fail-bg); color: var(--fail-fg); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 1rem; font-size: 0.87rem; margin: 0.5rem 0 0.25rem; }
dt { color: var(--muted); }
dd { font-family: var(--mono); overflow-wrap: anywhere; }
pre.stream { font-family: var(--mono); font-size: 0.82rem; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow-y: auto; padding: 0.5rem 0.75rem; background: color-mix(in srgb, var(--border) 30%, transparent); border-radius: 6px; }
button.plain { background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 0.25rem 0.75rem; cursor: pointer; font-size: 0.85rem; }
.empty { color: var(--muted); padding: 1rem 0; }
/* Run view fills the viewport exactly: header rows take their natural
   height, #run-columns flexes into the remainder, and the two panes are
   the ONLY scrollers — no page-level scrollbar. */
body.run-view { overflow: hidden; height: 100vh; }
body.run-view #app { height: 100vh; display: flex; flex-direction: column; }
body.run-view #run-screen { flex: 1; min-height: 0; display: flex; flex-direction: column; }
body.run-view #run-content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
#run-columns { display: grid; grid-template-columns: minmax(280px, 5fr) minmax(320px, 7fr); gap: 1rem; align-items: stretch; flex: 1; min-height: 0; }
#run-columns > .pane { overflow-y: auto; min-height: 0; margin-bottom: 0.5rem; }
@media (max-width: 900px) {
  body.run-view { overflow: auto; height: auto; }
  body.run-view #app, body.run-view #run-screen, body.run-view #run-content { height: auto; display: block; }
  #run-columns { grid-template-columns: 1fr; }
  #run-columns > .pane { max-height: none; overflow: visible; }
}
#experiment-panel { border-top: 1px solid var(--border); margin-top: 1rem; padding-top: 0.75rem; }
/* The precision token is invisible at rest and becomes the strong mark
   under illumination — the exact grep hit inside a lit call. */
.tool-call.illum-good .tok { background: var(--pass-fg); color: var(--pass-bg); border-radius: 2px; padding: 0 1px; }
.tool-call.illum-bad .tok { background: var(--fail-fg); color: var(--fail-bg); border-radius: 2px; padding: 0 1px; }
.tool-call.unmapped { border-left: 2px dashed var(--muted); }
/* Policy span anchors: invisible at rest, lit on hover/lock of their entry. */
mark.policy-mark { background: transparent; color: inherit; }
mark.policy-mark.illum-good { background: var(--hl-good-bg); color: var(--hl-good-fg); border-radius: 3px; }
mark.policy-mark.illum-bad { background: var(--hl-bad-bg); color: var(--hl-bad-fg); border-radius: 3px; }
/* Affordance honesty: entries with no transcript anchor are visibly
   non-interactive — dimmed, no pointer, no hover ring. */
.contract-item.no-anchor { opacity: 0.72; }
.contract-item.no-anchor .what { cursor: default; }
/* "What exactly is windtunnel expecting?" — the opaque check's own source,
   collapsed behind a toggle. */
details.check-source { margin: 0.15rem 0 0.4rem 1.2rem; }
details.check-source summary { font-size: 0.78rem; color: var(--muted); }
pre.check-source { font-family: var(--mono); font-size: 0.75rem; white-space: pre; overflow-x: auto; max-height: 16rem; overflow-y: auto; padding: 0.5rem 0.75rem; margin-top: 0.25rem; background: color-mix(in srgb, var(--border) 30%, transparent); border-radius: 6px; }
.thought { padding: 0.4rem 0.75rem; font-size: 0.85rem; color: var(--muted); font-style: italic; white-space: pre-wrap; overflow-wrap: anywhere; border-top: 1px dashed var(--border); }
.thought:first-child { border-top: none; }
.call-group { border: 1px solid var(--border); border-radius: 6px; margin: 0.5rem 0; overflow: hidden; }
.tool-call.illum-good, .tagchip.illum-good { background: var(--hl-good-bg); color: var(--hl-good-fg); box-shadow: inset 2px 0 0 var(--pass-fg); }
.tool-call.illum-bad, .tagchip.illum-bad { background: var(--hl-bad-bg); color: var(--hl-bad-fg); box-shadow: inset 2px 0 0 var(--fail-fg); }
.contract-hover { cursor: pointer; border-radius: 4px; }
.contract-hover:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.contract-hover.locked { outline: 1px dashed var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.contract-item { display: flex; gap: 0.5rem; align-items: baseline; margin: 0.2rem 0; font-size: 0.87rem; }
.contract-item .verdict-word { font-family: var(--mono); font-size: 0.75rem; font-weight: 600; white-space: nowrap; }
.contract-item.good .verdict-word { color: var(--pass-fg); }
.contract-item.bad .verdict-word { color: var(--fail-fg); }
.contract-item .what { font-family: var(--mono); overflow-wrap: anywhere; }
.call-strip { display: flex; gap: 0.3rem; flex-wrap: wrap; margin: 0.3rem 0; }
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

// ── routing ──────────────────────────────────────────────────────────────────
function route() {
  const match = location.hash.match(/^#\\/run\\/([A-Za-z0-9_-]+)$/);
  const runScreen = $('#run-screen');
  const main = $('#main-screen');
  document.body.classList.toggle('run-view', !!match);
  if (match) {
    main.style.display = 'none';
    runScreen.style.display = 'flex';
    showRun(match[1]);
  } else {
    runScreen.style.display = 'none';
    main.style.display = 'block';
  }
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
      rows.map((row) => {
        const runId = (row.run_ids || [])[0];
        return `<tr class="run-row" data-run="${esc(runId ?? '')}">
        <td class="mono">${esc(row.ts ?? '')}</td>
        <td class="mono">${esc(row.scenario_id ?? '')}</td>
        <td>${chip(row.verdict)}</td>
        <td class="mono">${esc(row.passed ?? '?')}/${esc(row.runs ?? '?')}</td>
        <td>${layerChips(row.layer_pass_rates)}</td>
        <td class="mono">${esc(row.git_sha ?? '')}</td>
      </tr>`;
      }).join('') + '</tbody></table></div>');
  }
  $('#ledger').innerHTML = parts.join('') ||
    '<div class="empty">No ledger rows yet — run a sweep with <span class="mono">wt run</span> and this page will pick it up.</div>';
  $('#ledger-note').textContent = data.skipped ? `${data.skipped} malformed ledger line(s) skipped` : '';
  document.querySelectorAll('tr.run-row').forEach((tr) => tr.addEventListener('click', () => {
    if (tr.dataset.run) location.hash = '#/run/' + tr.dataset.run;
  }));
}

// ── run screen ───────────────────────────────────────────────────────────────
async function showRun(runId) {
  const container = $('#run-content');
  container.innerHTML = '<div class="empty">loading run…</div>';
  let run;
  try {
    run = await fetchJSON('/api/run/' + encodeURIComponent(runId));
  } catch (err) {
    container.innerHTML = `<div class="empty">could not load run ${esc(runId)}: ${esc(err.message)}</div>`;
    return;
  }
  let evidence = null;
  try {
    evidence = await fetchJSON('/api/run/' + encodeURIComponent(runId) + '/evidence');
  } catch { /* rendered below as unavailable */ }
  if (!ledgerRows.length) { try { ledgerRows = (await fetchJSON('/api/ledger')).rows; } catch { /* ok */ } }
  const row = ledgerRows.find((r) => (r.run_ids || []).includes(runId)) || null;
  container.innerHTML = renderRunScreen(runId, run, evidence, row);
  container.querySelectorAll('button[data-scroll]').forEach((b) => b.addEventListener('click', () => {
    const target = document.getElementById(b.dataset.scroll);
    if (!target) return;
    // Scroll within the target's own pane (nested scroll container), not the page.
    const pane = target.closest('.pane');
    if (pane) {
      const delta = target.getBoundingClientRect().top - pane.getBoundingClientRect().top;
      pane.scrollTo({top: pane.scrollTop + delta - 6, behavior: 'smooth'});
    } else {
      target.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  }));
  wireIllumination(container);
}

function verdictWord(good, goodText, badText) {
  return `<span class="verdict-word">${good ? '✓ ' + esc(goodText) : '✗ ' + esc(badText)}</span>`;
}
function contractItem(good, what, note, extra) {
  const cls = (extra && extra.cls) ? ' ' + extra.cls : '';
  const attrs = (extra && extra.attrs) ? ' ' + extra.attrs : '';
  return `<div class="contract-item ${good ? 'good' : 'bad'}${cls}"${attrs}>` +
    verdictWord(good, note.good, note.bad) +
    `<span class="what">${what}</span></div>`;
}
function sourceToggle(source) {
  // The opaque check's own source, when the server could introspect it.
  if (!source) return '';
  return `<details class="check-source"><summary>show check source</summary><pre class="check-source">${esc(source)}</pre></details>`;
}
function hoverAttrs(indices, hl) {
  // Server-computed observed-call indices -> hover/lock illumination targets.
  if (!indices || !indices.length) return null;
  return {cls: 'contract-hover', attrs: `data-targets="${indices.join(',')}" data-hl="${hl}" title="hover to highlight in the transcript; click to lock"`};
}

function renderRunScreen(runId, run, evidencePayload, row) {
  const trace = run.trace || {};
  const score = run.score;
  const available = !!(evidencePayload && evidencePayload.available);
  const ev = available ? evidencePayload.evidence : null;
  const scenario = available ? evidencePayload.scenario : (score ? score.scenario : null);

  // Header + sibling runs of the same ledger row
  const siblings = row ? (row.run_ids || []) : [];
  const siblingLinks = siblings.length > 1 ? siblings.map((id) =>
    id === runId ? `<span class="tagchip">${esc(id.slice(0, 8))}</span>`
      : `<a class="tagchip" href="#/run/${esc(id)}">${esc(id.slice(0, 8))}</a>`).join('') : '';

  const parts = [`<div class="group-head">
    <button class="plain" onclick="location.hash='#/'">← runs</button>
    <span class="label">${esc(trace.scenario_id ?? '')}</span>
    ${score ? chip(score.verdict) : ''}
    <span class="muted">label ${esc(trace.variant_id ?? '')} · ${esc(trace.model ?? '')} ${esc(trace.quant ?? '')} · ${esc(run.trace_file ?? '')}</span>
    ${siblingLinks}
  </div>`];

  // Headline banner: the failing layers' detail strings front and center.
  const layerNames = ['outcome', 'trajectory', 'constraint', 'integrity'];
  if (score) {
    const failing = layerNames.filter((layer) => score[layer] && score[layer].passed === false);
    for (const layer of failing) {
      parts.push(`<div class="why"><strong>${esc(layer)}:</strong> ${esc(score[layer].detail)}</div>`);
    }
    if (!failing.length) parts.push('<div class="why ok">all scored layers passed</div>');
    parts.push('<div class="layers">' + layerNames.map((layer) => {
      const result = score[layer];
      if (!result) return '';
      return `<button class="layer-chip ${result.passed ? 'ok' : 'bad'}" data-scroll="ev-${layer}">${esc(layer)}</button>`;
    }).join('') + '</div>');
  } else {
    parts.push('<div class="empty">no .score.json sidecar beside this trace — layer verdicts unavailable</div>');
  }
  if (!available) {
    const reason = evidencePayload && evidencePayload.reason
      ? evidencePayload.reason
      : 'evidence endpoint unreachable';
    parts.push(`<div class="muted">evidence highlighting unavailable: ${esc(reason)}</div>`);
  }

  // Illumination context for this render: applyIllum lights both the
  // witnessed list (data-obs) and, through the server-computed
  // witnessed->transcript mapping, the claimed call blocks (data-claim).
  window.RUN_TRAJ = available ? ev.trajectory : null;

  parts.push(renderLineage(runId, row));
  parts.push('<div id="run-columns">');
  parts.push('<div class="panel pane">' + renderContract(scenario, ev, score) +
    renderExperimentPanel(runId) + '</div>');
  parts.push('<div class="panel pane">' + renderTranscript(trace, ev, score) + '</div>');
  parts.push('</div>');
  return parts.join('');
}

// ── hover-to-illuminate / click-to-lock ──────────────────────────────────────
// Contract entries carry data-targets (observed-call indices computed by the
// SERVER in evidence.trajectory) and data-hl (good/bad). Transcript call
// nodes carry data-obs="<index>". Hovering illuminates; clicking locks the
// highlight so it survives scrolling; click again (or another entry) to
// unlock/switch.
let lockedEntry = null;
// Every selector an entry's illumination targets, in one list — used both
// to toggle classes and to find the first lit node in document order.
function illumSelectors(entry) {
  const traj = window.RUN_TRAJ || null;
  const selectors = [];
  (entry.dataset.targets || '').split(',').filter(Boolean).forEach((index) => {
    selectors.push(`[data-obs="${index}"]`);
    // Server-witnessed evidence: also light the CLAIMED transcript call the
    // server mapped this witnessed call onto (data-claim tags follow the
    // same claimed-call walk the mapping was computed against).
    if (traj && traj.transcript_call_map) {
      const mapped = traj.transcript_call_map[Number(index)];
      if (mapped && mapped.transcript_index != null) {
        selectors.push(`[data-claim="${mapped.transcript_index}"]`);
      }
    }
  });
  // Policy span anchors, tagged by evidence-entry index.
  if (entry.dataset.policyTarget !== undefined) {
    selectors.push(`[data-policy="${entry.dataset.policyTarget}"]`);
  }
  // Directly-claimed targets (mapped observation anchors).
  (entry.dataset.claimTargets || '').split(',').filter(Boolean).forEach((index) => {
    selectors.push(`[data-claim="${index}"]`);
  });
  return selectors;
}
function applyIllum(entry, on) {
  const cls = 'illum-' + (entry.dataset.hl || 'good');
  const selectors = illumSelectors(entry);
  if (!selectors.length) return;
  document.querySelectorAll(selectors.join(',')).forEach((node) =>
    node.classList.toggle(cls, on));
}
// Scroll the transcript pane to the first illuminated node (document
// order). Immediate on lock; on plain hover only after a short
// hover-intent dwell so sweeping the cursor down the contract doesn't
// yank the pane around on every pass.
function scrollToFirstLit(entry) {
  const selectors = illumSelectors(entry);
  if (!selectors.length) return;
  const first = document.querySelector(selectors.join(','));
  if (!first) return;
  const pane = first.closest('.pane');
  if (!pane) return;
  const delta = first.getBoundingClientRect().top - pane.getBoundingClientRect().top;
  pane.scrollTo({top: pane.scrollTop + delta - 48, behavior: 'smooth'});
}
let hoverScrollTimer = null;
function wireIllumination(container) {
  lockedEntry = null;
  container.querySelectorAll('.contract-hover').forEach((entry) => {
    entry.addEventListener('mouseenter', () => {
      if (lockedEntry) return;
      applyIllum(entry, true);
      // Hover-intent: dwell briefly before scrolling to the first match.
      clearTimeout(hoverScrollTimer);
      hoverScrollTimer = setTimeout(() => scrollToFirstLit(entry), 350);
    });
    entry.addEventListener('mouseleave', () => {
      clearTimeout(hoverScrollTimer);
      if (!lockedEntry) applyIllum(entry, false);
    });
    entry.addEventListener('click', () => {
      clearTimeout(hoverScrollTimer);
      if (lockedEntry === entry) {
        applyIllum(entry, false);
        entry.classList.remove('locked');
        lockedEntry = null;
        applyIllum(entry, true); // still hovered
        return;
      }
      if (lockedEntry) { applyIllum(lockedEntry, false); lockedEntry.classList.remove('locked'); }
      lockedEntry = entry;
      entry.classList.add('locked');
      applyIllum(entry, true);
      scrollToFirstLit(entry); // lock scrolls immediately and keeps it
    });
  });
}

// ── before/after lineage ─────────────────────────────────────────────────────
// Experiment reruns carry label exp-<parent run_id[:8]>-<HHMMSS>; the parent
// is resolved client-side by prefix-matching run ids in the ledger.
const EXP_LABEL_RE = /^exp-([A-Za-z0-9]{8})-/;
function findRowByRunPrefix(prefix) {
  return ledgerRows.find((r) => (r.run_ids || []).some((id) => id.startsWith(prefix))) || null;
}
function renderLineage(runId, row) {
  const parts = [];
  if (row && EXP_LABEL_RE.test(row.label || '')) {
    const parentRow = findRowByRunPrefix(row.label.match(EXP_LABEL_RE)[1]);
    if (parentRow) {
      const parentId = parentRow.run_ids[0];
      parts.push(`<div class="panel"><div class="group-head">
        <span class="muted">experiment of</span>
        <a class="tagchip" href="#/run/${esc(parentId)}">${esc(parentRow.scenario_id)} · ${esc(parentId.slice(0, 8))}</a>
        <span class="muted">verdict</span> ${chip(parentRow.verdict)} <span class="muted">→</span> ${chip(row.verdict)}
        ${parentRow.verdict === row.verdict ? '<span class="muted">(unchanged)</span>' : '<span class="tagchip">changed</span>'}
      </div></div>`);
    }
  }
  if (row) {
    const children = ledgerRows.filter((r) => (r.label || '').startsWith('exp-' + runId.slice(0, 8) + '-'));
    if (children.length) {
      parts.push('<div class="panel"><div class="group-head"><span class="muted">experiments varying this run:</span>' +
        children.map((child) => {
          const childId = (child.run_ids || [])[0];
          return `<a class="tagchip" href="#/run/${esc(childId)}">${esc(child.label)}</a> ${chip(row.verdict)}<span class="muted">→</span>${chip(child.verdict)}`;
        }).join(' ') + '</div></div>');
    }
  }
  return parts.join('');
}

// ── experiment panel (knobs + rerun) ─────────────────────────────────────────
function renderExperimentPanel(runId) {
  const meta = window.META || {};
  if (!meta.experiment) return '';
  const knobInfo = meta.knobs || {declared: false, knobs: [], detail: null};
  const parts = [`<div class="panel" id="experiment-panel"><h2>Experiment</h2>
    <div class="muted">runtime: ${esc(meta.runtime)} — adjust knobs, rerun exactly this scenario, and compare verdicts.</div>`];
  if (!knobInfo.declared) {
    parts.push(`<div class="muted">${esc(knobInfo.detail || 'runtime declares no knobs')}</div>`);
  } else if (!knobInfo.knobs.length) {
    parts.push('<div class="muted">runtime declares no knobs — a rerun repeats the scenario unchanged</div>');
  } else {
    parts.push(knobInfo.knobs.map((knob, index) => {
      const id = `knob-${index}`;
      let input;
      if (knob.kind === 'enum') {
        input = `<select id="${id}" data-knob="${esc(knob.name)}" data-kind="enum">` +
          knob.choices.map((choice) => `<option${choice === knob.value ? ' selected' : ''}>${esc(choice)}</option>`).join('') + '</select>';
      } else if (knob.kind === 'flag') {
        input = `<input type="checkbox" id="${id}" data-knob="${esc(knob.name)}" data-kind="flag"${knob.value ? ' checked' : ''}>`;
      } else if (knob.kind === 'number') {
        input = `<input type="number" id="${id}" data-knob="${esc(knob.name)}" data-kind="number" value="${esc(knob.value ?? '')}">`;
      } else {
        input = `<textarea id="${id}" data-knob="${esc(knob.name)}" data-kind="text" data-initial="${esc(knob.value ?? '')}" rows="2" style="width:100%">${esc(knob.value ?? '')}</textarea>`;
      }
      return `<div class="contract-item"><span class="what" title="${esc(knob.description)}">${esc(knob.name)} <span class="muted">(${esc(knob.kind)} · ${esc(knob.scope)})</span></span></div>${input}`;
    }).join(''));
  }
  parts.push(`<div class="group-head" style="margin-top:0.75rem">
    <button class="plain" id="rerun-btn">Rerun this scenario</button>
    <span class="muted" id="rerun-status"></span></div>
    <pre class="stream" id="rerun-log" style="display:none"></pre></div>`);
  // Wire after insertion.
  setTimeout(() => {
    const btn = document.getElementById('rerun-btn');
    if (btn) btn.addEventListener('click', () => startRerun(runId));
  }, 0);
  return parts.join('');
}

function collectKnobOverrides() {
  const overrides = {};
  document.querySelectorAll('#experiment-panel [data-knob]').forEach((el) => {
    const kind = el.dataset.kind;
    if (kind === 'flag') overrides[el.dataset.knob] = el.checked;
    else if (kind === 'number') { if (el.value !== '') overrides[el.dataset.knob] = Number(el.value); }
    else if (kind === 'text') { if (el.value !== (el.dataset.initial ?? '')) overrides[el.dataset.knob] = el.value; }
    else overrides[el.dataset.knob] = el.value;
  });
  return overrides;
}

async function startRerun(runId) {
  const statusEl = document.getElementById('rerun-status');
  const logEl = document.getElementById('rerun-log');
  statusEl.textContent = 'starting…';
  let payload;
  try {
    const response = await fetch('/api/experiment/rerun', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({run_id: runId, knobs: collectKnobOverrides()}),
    });
    payload = await response.json();
    if (!response.ok) {
      statusEl.textContent = 'refused: ' + (Array.isArray(payload.detail) ? payload.detail.join('; ') : (payload.error || response.status));
      return;
    }
  } catch (err) {
    statusEl.textContent = 'failed: ' + err.message;
    return;
  }
  const label = payload.job.label;
  statusEl.textContent = `running as ${label}…`;
  logEl.style.display = 'block';
  logEl.textContent = '';
  const source = new EventSource('/api/experiment/events');
  source.onmessage = async (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.line !== undefined) {
      logEl.append(document.createTextNode((logEl.textContent ? '\\n' : '') + data.line));
      logEl.scrollTop = logEl.scrollHeight;
    }
    if (data.done) {
      source.close();
      const rc = data.job ? data.job.returncode : null;
      await refreshLedger();
      const childRow = ledgerRows.find((r) => r.label === label);
      if (childRow && (childRow.run_ids || []).length) {
        statusEl.innerHTML = `finished (exit ${esc(rc)}) — ` +
          `<a class="tagchip" href="#/run/${esc(childRow.run_ids[0])}">open ${esc(label)}</a> ${chip(childRow.verdict)}`;
      } else {
        statusEl.textContent = `finished (exit ${rc}); ledger row not visible yet — refresh the dashboard`;
      }
    }
  };
  source.onerror = () => { source.close(); };
}

function renderContract(scenario, ev, score) {
  if (!scenario) return '<h2>Scenario contract</h2><div class="empty">scenario definition unavailable</div>';
  const parts = [`<h2 id="ev-outcome">Scenario contract</h2>
    <div class="group-head"><span class="label">${esc(scenario.name)}</span>` +
    (scenario.tags || []).map((tag) => `<span class="tagchip">${esc(tag)}</span>`).join('') + '</div>'];

  const userTurns = (scenario.user_turns && scenario.user_turns.length)
    ? scenario.user_turns : [scenario.scored_prompt ?? scenario.prompt ?? ''];
  parts.push('<h3>User turns</h3>' + userTurns.map((turn) =>
    `<div class="contract-item"><span class="what">${esc(turn)}</span></div>`).join(''));

  // Outcome expectations, with per-item said / never-said status.
  const outcomeEv = ev ? ev.outcome : null;
  const groups = scenario.target_facts || [];
  if (groups.length) {
    parts.push('<h3>Target facts (AND of OR groups)</h3>' + groups.map((group, index) => {
      const info = outcomeEv && outcomeEv.fact_groups[index];
      const what = group.map((fact) => esc(fact)).join(' | ');
      if (!info) return `<div class="contract-item"><span class="what">${what}</span></div>`;
      return contractItem(info.matched, what, {good: 'said', bad: 'never said'});
    }).join(''));
  }
  const numbers = scenario.target_numbers || [];
  if (numbers.length) {
    parts.push('<h3>Target numbers</h3>' + numbers.map((number, index) => {
      const info = outcomeEv && outcomeEv.numbers[index];
      const what = esc(number.value + (number.unit ? ' ' + number.unit : ''));
      if (!info) return `<div class="contract-item"><span class="what">${what}</span></div>`;
      return contractItem(info.matched, what, {good: 'said', bad: 'never said'});
    }).join(''));
  }
  const forbiddenFacts = scenario.forbidden_facts || [];
  if (forbiddenFacts.length) {
    parts.push('<h3>Forbidden facts</h3>' + forbiddenFacts.map((fact, index) => {
      const info = outcomeEv && outcomeEv.forbidden_facts[index];
      if (!info) return `<div class="contract-item"><span class="what">${esc(fact)}</span></div>`;
      return contractItem(!info.asserted, esc(fact), {good: 'not asserted', bad: 'asserted'});
    }).join(''));
  }
  if (scenario.requires_tool_use) {
    const observed = outcomeEv ? outcomeEv.tool_use_observed : null;
    parts.push('<h3>Tool use</h3>' + (observed === null
      ? '<div class="contract-item"><span class="what">required</span></div>'
      : contractItem(observed, 'required', {good: 'tools used', bad: 'no tools used'})));
  }
  if (scenario.has_outcome_fn) {
    parts.push('<div class="muted">opaque outcome_fn — no transcript anchor; its verdict is the outcome detail above</div>' +
      sourceToggle(outcomeEv ? outcomeEv.outcome_fn_source : null));
  }

  // Trajectory expectations.
  const trajEv = ev ? ev.trajectory : null;
  const mustCall = scenario.must_call || [];
  parts.push('<h3 id="ev-trajectory">Trajectory</h3>');
  if (trajEv && trajEv.evidence_source) {
    parts.push(`<div class="muted">evidence: ${esc(trajEv.evidence_source)}</div>`);
  }
  if (mustCall.length) {
    parts.push(mustCall.map((entry, index) => {
      const what = esc(Array.isArray(entry) ? entry.join(' | ') : entry);
      const info = trajEv && trajEv.must_call[index];
      if (!info) return `<div class="contract-item"><span class="what">must call ${what}</span></div>`;
      // Precision for ABSENCE: there is nothing to highlight in the
      // transcript — the contract entry itself is the definitive red state.
      const via = info.satisfied
        ? ` <span class="muted">via ${esc(info.matched_calls.map((i) => trajEv.observed_calls[i]).join(', '))}</span>`
        : ' <span class="muted">no call matched — this is the failure</span>';
      return contractItem(info.satisfied, `must call ${what}${via}`,
        {good: 'called', bad: 'never called'}, hoverAttrs(info.matched_calls, 'good'));
    }).join(''));
    if (scenario.order_matters) {
      const ordered = trajEv ? trajEv.order_satisfied : null;
      parts.push(ordered === null
        ? '<div class="contract-item"><span class="what">declared order required</span></div>'
        : contractItem(ordered, 'declared order required', {good: 'in order', bad: 'order violated'}));
    }
  }
  const forbiddenCalls = scenario.forbidden_calls || [];
  if (forbiddenCalls.length) {
    parts.push(forbiddenCalls.map((name, index) => {
      const info = trajEv && trajEv.forbidden_calls[index];
      if (!info) return `<div class="contract-item"><span class="what">never call ${esc(name)}</span></div>`;
      const via = info.violated
        ? ` <span class="muted">saw ${esc(info.offending_calls.map((i) => trajEv.observed_calls[i]).join(', '))}</span>` : '';
      return contractItem(!info.violated, `never call ${esc(name)}${via}`,
        {good: 'clean', bad: 'called'}, hoverAttrs(info.offending_calls, 'bad'));
    }).join(''));
  }
  if (!mustCall.length && !forbiddenCalls.length) {
    parts.push('<div class="muted">no tool-path expectations declared</div>');
  }
  if ((scenario.trajectory_checks || []).length) {
    const checksEv = trajEv ? (trajEv.custom_checks || []) : [];
    parts.push(`<div class="muted">opaque custom checks — no transcript anchor: ${esc(scenario.trajectory_checks.join(', '))} (verdicts in the trajectory detail)</div>` +
      checksEv.map((check) => check.source
        ? `<div class="muted" style="margin-left:1.2rem">${esc(check.name)}</div>` + sourceToggle(check.source)
        : '').join(''));
  }

  // Constraint + integrity + cost.
  parts.push('<h3 id="ev-constraint">Constraint</h3>');
  // Ground truth for what gated THIS run is the sidecar's recorded policy
  // list (it includes policies attached at sweep time, e.g. by a runtime
  // plugin's pre_run) — prefer it over the current pack definition. An old
  // sidecar without the key is honest absence, never "no policies declared".
  const recordedPolicies = score && score.scenario ? score.scenario.policies : undefined;
  const policies = recordedPolicies !== undefined ? recordedPolicies : scenario.policies;
  const constraintResult = score ? score.constraint : null;
  // Anchor evidence (when the pack is loaded): entry index in the evidence
  // list, matched by name — anchors come from PolicyVerdict returns.
  const policyEv = ev && ev.constraint ? ev.constraint.policies : [];
  if (policies === undefined || policies === null) {
    parts.push('<div class="muted">policy declarations were not recorded for this run' +
      (constraintResult && constraintResult.passed === false
        ? ' — the failed policy names are in the constraint detail above' : '') + '</div>');
  } else if (!policies.length) {
    parts.push('<div class="muted">no policies declared</div>');
  } else {
    parts.push(policies.map((p) => {
      // The recorded sidecar verdict stays authoritative: violated = named
      // in the failed constraint detail.
      const violated = !!(constraintResult && constraintResult.passed === false &&
        (constraintResult.detail || '').includes(`'${p.name}'`));
      const what = `${esc(p.name)}${p.effect_class ? ` [${esc(p.effect_class)}]` : ''}`;
      const evIndex = policyEv.findIndex((entry) => entry.name === p.name);
      const entryEv = evIndex >= 0 ? policyEv[evIndex] : null;
      let extra = null;
      let suffix = '';
      if (entryEv && entryEv.anchorable) {
        // Anchorable: hover/lock illuminates the anchored calls/spans, plus
        // any observation anchors the server mapped onto claimed calls.
        const targets = (entryEv.call_anchors || []).map((a) => a.call_index);
        const claimTargets = (entryEv.observation_anchors || [])
          .filter((a) => a.claim_index != null).map((a) => a.claim_index);
        const attrs = [`data-hl="${violated ? 'bad' : 'good'}"`];
        if (targets.length) attrs.push(`data-targets="${targets.join(',')}"`);
        if (claimTargets.length) attrs.push(`data-claim-targets="${claimTargets.join(',')}"`);
        if ((entryEv.span_anchors || []).length) attrs.push(`data-policy-target="${evIndex}"`);
        extra = {cls: 'contract-hover',
                 attrs: attrs.join(' ') + ' title="hover to highlight in the transcript; click to lock"'};
        if (entryEv.detail) suffix += ` <span class="muted">${esc(entryEv.detail)}</span>`;
      } else {
        // Affordance honesty: nothing to illuminate — say so, don't invite
        // a hover that does nothing.
        extra = {cls: 'no-anchor'};
        suffix = ' <span class="muted">opaque policy — no transcript anchor</span>';
      }
      // Unmapped observation anchors + free-text locators render as text.
      const lookedAt = [
        ...(entryEv ? (entryEv.observation_anchors || []) : [])
          .filter((a) => a.claim_index == null)
          .map((a) => `observations.${a.key}` + (a.index != null ? `[${a.index}]` : '') +
            (a.note ? ` — ${a.note}` : '')),
        ...(entryEv ? (entryEv.locators || []) : []),
      ];
      if (lookedAt.length) {
        suffix += `<br><span class="muted">looked at: ${lookedAt.map((l) => esc(l)).join(' · ')}</span>`;
      }
      const toggle = entryEv ? sourceToggle(entryEv.source) : '';
      if (constraintResult == null) {
        return `<div class="contract-item ${extra.cls}" ${extra.attrs ?? ''}><span class="what">${what}${suffix}</span></div>` + toggle;
      }
      return contractItem(!violated, what + suffix, {good: 'held', bad: 'violated'}, extra) + toggle;
    }).join(''));
  }

  parts.push('<h3 id="ev-integrity">Perturbations (integrity)</h3>');
  const perturbations = scenario.perturbations || [];
  const markerEv = ev ? ev.integrity.markers : null;
  parts.push(perturbations.length
    ? perturbations.map((p, index) => {
        const info = markerEv && markerEv[index];
        const what = esc(p.type + (p.marker ? ` (${p.marker})` : ''));
        if (!info) return `<div class="contract-item"><span class="what">${what}</span></div>`;
        return contractItem(info.applied, what, {good: 'applied', bad: 'not applied'});
      }).join('')
    : '<div class="muted">no perturbations declared</div>');

  parts.push('<h3>Gate & cost</h3>');
  parts.push(`<div class="contract-item"><span class="what">gate: ${esc((scenario.gate_layers || []).join(' + '))}</span></div>`);
  if (scenario.failure_cost) {
    const cost = scenario.failure_cost;
    parts.push(`<div class="contract-item"><span class="what">failure cost: ${esc(cost.severity)} · risk ${esc(cost.risk_weight)}` +
      `${cost.customer_visible ? ' · customer-visible' : ''}${cost.reversible === false ? ' · irreversible' : ''}</span></div>`);
  }
  return parts.join('');
}

// Wrap highlight spans around content. Spans are half-open [start, end)
// offsets into the exact content string; overlaps keep the earliest span.
// span.attrs (pre-escaped attribute text) lets policy anchors carry their
// data-policy tag for illumination targeting.
function renderHighlighted(content, spans) {
  const ordered = [...spans].sort((a, b) => a.start - b.start || a.end - b.end)
    .filter((span) => span.start >= 0 && span.end <= content.length && span.start < span.end);
  const parts = [];
  let cursor = 0;
  for (const span of ordered) {
    if (span.start < cursor) continue; // overlap — first span wins
    parts.push(esc(content.slice(cursor, span.start)));
    parts.push(`<mark class="${span.cls}" title="${esc(span.title ?? '')}"${span.attrs ?? ''}>${esc(content.slice(span.start, span.end))}</mark>`);
    cursor = span.end;
  }
  parts.push(esc(content.slice(cursor)));
  return parts.join('');
}

// Policy span anchors grouped by turn: turn_index -> renderHighlighted spans
// tagged data-policy="<entry index>" so hover/lock can light them.
function policySpansByTurn(ev) {
  const byTurn = new Map();
  const policies = ev && ev.constraint ? ev.constraint.policies : [];
  policies.forEach((policy, policyIndex) => {
    for (const anchor of policy.span_anchors || []) {
      if (!byTurn.has(anchor.turn_index)) byTurn.set(anchor.turn_index, []);
      byTurn.get(anchor.turn_index).push({
        start: anchor.start,
        end: anchor.end,
        cls: 'policy-mark',
        title: 'policy ' + policy.name + (anchor.note ? ': ' + anchor.note : ''),
        attrs: ` data-policy="${policyIndex}"`,
      });
    }
  });
  return byTurn;
}

function callDetailFor(trajEv, obsIndex) {
  if (obsIndex == null || !trajEv) return null;
  return (trajEv.observed_call_details || [])[obsIndex] || null;
}
// Precision: wrap the server-computed matched token (always a suffix of the
// observed name, by tool_name_matches construction) so illumination marks
// the exact grep hit, not the whole block. The token TEXT comes from the
// server; this only locates that suffix in the displayed name.
function renderCallName(name, detail) {
  const token = detail && detail.matched_token ? detail.matched_token.text : null;
  const display = String(name ?? '?');
  if (token && display.endsWith(token)) {
    const prefix = display.slice(0, display.length - token.length);
    return `${esc(prefix)}<span class="tok">${esc(token)}</span>`;
  }
  return esc(display);
}
// Inverse of the server's witnessed->transcript mapping: claimed-call walk
// index -> the witnessed call's detail, so claimed blocks share status and
// token precision in server-witnessed mode.
function claimedDetailMap(trajEv) {
  const map = new Map();
  if (trajEv && trajEv.transcript_call_map) {
    for (const entry of trajEv.transcript_call_map) {
      if (entry.transcript_index != null) {
        map.set(entry.transcript_index, callDetailFor(trajEv, entry.observed_index));
      }
    }
  }
  return map;
}

// Transcript: strict chronology in three sections — the user message(s),
// the tool-call trajectory, the final assistant output. The stored turn
// structure may aggregate (one assistant turn carrying the final text AND
// every tool call), so chronology is reconstructed: a non-final assistant
// turn's content renders as a thought beside its calls; the scored turn's
// content renders LAST, as the final output, after the trajectory it
// produced.
function renderTranscript(trace, ev, score) {
  const outcomeEv = ev ? ev.outcome : null;
  const trajEv = ev ? ev.trajectory : null;
  const turns = trace.turns || [];
  let answerIndex = outcomeEv ? outcomeEv.answer_turn_index : null;
  if (answerIndex == null) {
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].role === 'assistant') { answerIndex = i; break; }
    }
  }
  const source = trajEv ? trajEv.evidence_source : null;
  const parts = ['<h2>Transcript</h2>'];

  const policySpans = policySpansByTurn(ev);
  const contentWithPolicyMarks = (turnIndex, content, extraSpans) => {
    const spans = [...(extraSpans || []), ...(policySpans.get(turnIndex) || [])];
    return spans.length ? renderHighlighted(content ?? '', spans) : esc(content);
  };

  // (a) the user message(s), chronological.
  const userEntries = turns.map((turn, index) => [turn, index])
    .filter(([turn]) => turn.role === 'user');
  parts.push(`<h3 id="user-section">User message${userEntries.length > 1 ? 's' : ''}</h3>`);
  parts.push(userEntries.length
    ? userEntries.map(([turn, index]) => `<div class="turn"><div class="role">user</div>
        <div class="content">${contentWithPolicyMarks(index, turn.content)}</div></div>`).join('')
    : '<div class="muted">no user turns recorded</div>');

  // (b) the tool-call trajectory: thought + calls + results per turn, in order.
  // Observed-call indices: when evidence comes from the transcript, the
  // server's observed_calls order IS this iteration order over named calls,
  // so data-obs tags line up with the server-computed match indices.
  parts.push(`<h3 id="trajectory-section">Tool-call trajectory` +
    (source ? ` <span class="muted">evidence: ${esc(source)}</span>` : '') + '</h3>');
  let obsCursor = 0;
  const claimedByIndex = claimedDetailMap(trajEv);
  const groups = [];
  turns.forEach((turn, index) => {
    const isUser = turn.role === 'user';
    const isFinal = index === answerIndex;
    // The index cursor mirrors the server's extraction exactly: every named
    // call in turn order counts, whatever the turn's role.
    const calls = turn.tool_calls || [];
    const results = turn.tool_results || [];
    if (isUser && !calls.length && !results.length) return; // rendered in (a)
    const inner = [];
    if (!isUser && turn.role !== 'assistant') {
      // e.g. a tool-role turn: its content is a result in the trajectory.
      if ((turn.content || '').trim() || calls.length || results.length) {
        inner.push(`<div class="tool-call">${esc(turn.role)}: ${esc(turn.content)}</div>`);
      }
    } else if (!isUser && !isFinal && (turn.content || '').trim()) {
      // Intermediate assistant text = the thought before/between calls.
      inner.push(`<div class="thought">${contentWithPolicyMarks(index, turn.content)}</div>`);
    }
    for (const call of calls) {
      const name = call.function?.name ?? call.name;
      const args = call.function?.arguments ?? JSON.stringify(call.args ?? {});
      // data-claim follows the claimed-call walk (named calls in turn
      // order) — the same enumeration the server's witnessed->transcript
      // mapping targets. In transcript mode that walk IS the observed
      // list, so the block is also its own data-obs target.
      const claimIndex = name ? obsCursor++ : null;
      const obsIndex = (source === 'transcript') ? claimIndex : null;
      const detail = callDetailFor(trajEv, obsIndex) ??
        (claimIndex != null ? claimedByIndex.get(claimIndex) ?? null : null);
      const attrs = (claimIndex != null ? ` data-claim="${claimIndex}"` : '') +
        (obsIndex != null ? ` data-obs="${obsIndex}"` : '');
      // Neutral at rest: status lives on the contract side; color arrives
      // only as .illum-* from hover/lock.
      inner.push(`<div class="tool-call"${attrs}>tool_call ${renderCallName(name, detail)}(${esc(args)})</div>`);
    }
    for (const result of results) {
      inner.push(`<div class="tool-call">tool_result ${esc(JSON.stringify(result))}</div>`);
    }
    if (inner.length) groups.push(`<div class="call-group">${inner.join('')}</div>`);
  });
  parts.push(groups.join('') || '<div class="muted">no tool calls recorded</div>');

  // Server-witnessed calls are the authoritative observed path when a
  // logging mock was in play — data-obs tags live here in that mode.
  const witnessed = trace.mcp_calls || [];
  if (witnessed.length) {
    const callMap = (trajEv && trajEv.transcript_call_map) || null;
    parts.push('<h3>Witnessed at the mock server</h3>' + witnessed.map((call, index) => {
      const obsIndex = source === 'server-witnessed' ? index : null;
      const detail = callDetailFor(trajEv, obsIndex);
      const obsAttr = obsIndex != null ? ` data-obs="${obsIndex}"` : '';
      // Honest divergence indicator: the server saw this call but no
      // transcript call maps to it (count/name divergence) — say so
      // rather than silently illuminating nothing on the claimed side.
      const mapped = callMap && obsIndex != null ? callMap[obsIndex] : null;
      const unmapped = mapped != null && mapped.transcript_index == null;
      const note = unmapped ? ' <span class="muted">· no matching transcript call</span>' : '';
      return `<div class="tool-call${unmapped ? ' unmapped' : ''}"${obsAttr}>` +
        `${renderCallName(call.tool_name, detail)} ← ${esc(JSON.stringify(call.args ?? {}))}${note}</div>`;
    }).join(''));
  }

  // (c) the final assistant output, with evidence spans.
  parts.push('<h3 id="final-output">Final output</h3>');
  const finalTurn = answerIndex != null ? turns[answerIndex] : null;
  if (finalTurn == null) {
    parts.push('<div class="muted">no assistant turn recorded</div>');
  } else {
    let content;
    if (outcomeEv && answerIndex === outcomeEv.answer_turn_index) {
      const spans = [];
      for (const group of outcomeEv.fact_groups) {
        for (const span of group.spans) spans.push({...span, cls: 'hl-good', title: 'target fact: ' + span.fact});
      }
      for (const number of outcomeEv.numbers) {
        if (number.span) spans.push({...number.span, cls: 'hl-good', title: 'target number: ' + number.value});
      }
      for (const fact of outcomeEv.forbidden_facts) {
        for (const span of fact.spans) spans.push({...span, cls: 'hl-bad', title: 'forbidden fact asserted: ' + fact.fact});
      }
      content = contentWithPolicyMarks(answerIndex, finalTurn.content ?? '', spans);
    } else {
      content = contentWithPolicyMarks(answerIndex, finalTurn.content);
    }
    parts.push(`<div class="turn"><div class="role">assistant · scored turn</div>
      <div class="content">${content}</div></div>`);
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
  window.addEventListener('hashchange', route);
  try {
    window.META = await fetchJSON('/api/meta');
    $('#meta').textContent = `runs: ${window.META.runs_dir} · wind tunnel ${window.META.wt_version}`;
    if (!window.META.live_glob) $('#live-status').textContent =
      'live watch is off — restart with: wt serve --live-glob "path/to/*.jsonl"';
  } catch { /* header stays minimal */ }
  await refreshLedger();
  await loadScenarios();
  setInterval(refreshLedger, 3000);
  route();
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

  <div id="main-screen">
    <nav>
      <button data-tab="runs" class="active">Runs</button>
      <button data-tab="scenarios">Scenarios</button>
      <button data-tab="live">Live</button>
    </nav>

    <section class="tab active" id="tab-runs">
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

  <section id="run-screen" style="display:none">
    <div id="run-content"></div>
  </section>
</div>
<script>
{_JS}
</script>
</body>
</html>"""
