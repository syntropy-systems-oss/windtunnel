---
description: "Task guide for wt serve — the local, read-only web viewer over a runs/ directory: ledger dashboard, run drill-down, scenario browser, and live JSONL tail."
---
# Viewing runs

`wt serve` hosts a local, read-only web viewer over a runs/ directory: watch
sweeps land in the ledger, open a run to see the conversation and the four
layer verdicts, browse the discovered scenario packs (the test cases and the
tool surface they declare), and optionally tail whatever JSONL your runtime
writes while a sweep is in flight.

```bash
wt serve                              # view ./runs at http://127.0.0.1:8686/
wt serve --runs-dir runs --port 8686
wt serve --pack-source packs/mine.py:PACK
wt serve --live-glob "logs/*.jsonl"
```

The page is fully self-contained — inline CSS and vanilla JS, zero external
requests — and the server is read-only by construction: only GET is
implemented, and no endpoint mutates anything under the runs/ directory (or
anywhere else). It binds `127.0.0.1` by default; pass `--host` explicitly if
you really want it reachable off-box.

## Runs dashboard

The dashboard parses `runs/ledger.ndjsonl` — the append-only ledger `wt run`
writes one row per scenario aggregate — and renders it newest first, grouped
by variant label and pack. Each row shows the aggregate verdict chip
(`PASS` / `FAIL` / `PASS_WITH_VARIANCE` / `INVALID`), the per-run pass count,
the four per-layer pass rates, and the git SHA the sweep ran at. The page
polls the ledger every few seconds, so a sweep writing right now appears as
it lands; malformed or torn lines are skipped and counted, never fatal.

## Run screen

Clicking a ledger row opens the run as a full screen of its own
(`#/run/<run_id>` — deep-linkable, back button returns to the dashboard).
It loads the run's saved trace plus its `.score.json` sidecar — the same
artifacts `wt report`, `wt triage`, and `wt rescore` consume — and puts the
*why* front and center: every failing layer's `detail` string is the
headline banner.

The run view fills the viewport exactly — header plus panes, no
page-level scrollbar; the two panes are the only scrollers.

Below the banner, two independently scrolling panes: the **scenario contract**
(left — user turns, target fact groups, `must_call` / `forbidden_calls`,
recorded policies, declared perturbations, gate layers, failure cost) stays
visible while you scroll the **transcript** (right), which renders in
strict chronology as three sections: the user message, the tool-call
trajectory, and the final assistant output. When the trace stores
intermediate assistant text between tool calls, each step renders as
thought + call + result grouped; a trace that aggregates everything into
one turn renders its calls without thoughts — the viewer never invents
what the artifact doesn't carry. When a logging mock was in play, the tool
calls witnessed at the tool server itself (`mcp_calls`) render as the
authoritative observed path, independent of what the transcript claims.

Constraint policies are read from the sidecar's record of what actually
gated the run — including policies attached at sweep time (for example by
a runtime plugin's `pre_run`), which a pack reload cannot reconstruct. A
sidecar written before this record existed shows "policy declarations were
not recorded", never a false "no policies declared" beside a failed
constraint chip.

### Evidence highlighting

Scoring is pure over (Scenario, Trace), so the viewer re-runs the same
matching primitives against the stored trace and shows *where* each
expectation was met or missed:

- target facts and numbers that matched are highlighted green in the scored
  assistant turn, at the exact spans the matcher found;
- asserted forbidden facts are highlighted red (negation-aware — a
  disclaimed mention is not an assertion);
- each contract entry carries its own verdict: "said" / "never said" for
  facts, "called via `<observed name>`" / "never called" for `must_call`,
  "clean" / "called" for `forbidden_calls`, "held" / "violated" for
  recorded policies, "applied" / "not applied" for perturbation markers —
  and offending tool calls are flagged red in the transcript;
- hovering a `must_call` or `forbidden_calls` entry illuminates every
  matching tool call in the transcript (green for required, red for
  forbidden); clicking locks the highlight so it survives scrolling —
  click again, or another entry, to unlock or switch. Hover also scrolls
  the transcript pane to the first lit match after a short hover-intent
  dwell (locking scrolls immediately), so sweeping the cursor down the
  contract never thrashes the pane. The baseline transcript stays
  neutral — green/red appears only under illumination; at-rest status
  lives on the contract side. Matches are computed server-side by the
  same `tool_name_matches` comparisons the trajectory evaluator uses, at
  token precision: the exact canonical name is marked inside a
  platform-decorated call, not the whole block. When the evidence source
  is the server's own call log, a server-computed witnessed→transcript
  mapping (greedy in-order name walk) lights the corresponding claimed
  call blocks too; a witnessed call the transcript never claimed is
  labeled "no matching transcript call" rather than silently skipped. A
  miss is precision too: "never called" carries the definitive red state
  on the contract side — "no call matched — this is the failure" —
  because absence has nothing to highlight;
- a policy that returns an anchored verdict (`PolicyVerdict` with
  `EvidenceAnchor` references — see [writing a
  scenario](writing-a-scenario.md)) is hoverable like any call entry: its
  witnessed-call and span anchors illuminate, and its free-text locators
  render as "looked at: …" under the entry. Entries with nothing to
  illuminate — plain-predicate policies, custom `TrajectoryCheck`s,
  `outcome_fn` — are visibly non-interactive and say so ("opaque policy —
  no transcript anchor"); nothing ever looks hoverable and does nothing;
- the layer chips in the banner jump to their evidence entries within the
  contract pane.

One design law governs this: **the viewer never disagrees with the
scorer**. The span-returning matcher variants live beside the boolean
matchers in core (`windtunnel.api._matching`), the forbidden-facts gate and
its span scan are one algorithm, and an equivalence suite pins that a span
is found exactly when the boolean matcher passes. Opaque callables —
`outcome_fn`, `Policy` predicates, custom `TrajectoryCheck`s — are listed
by name with their verdicts in the layer details; no span is ever
fabricated for them.

Evidence needs the run's scenario definition: packs are discovered exactly
like `wt run`, so a run executed with a `--pack-source` needs `wt serve`
started with the same flag. Without it the run screen still shows the
transcript and the four layer details, and says evidence is unavailable
rather than guessing.

## Scenario browser

Scenario packs are discovered exactly like `wt run`: the built-in dims, the
`windtunnel.scenario_packs` entry-point group, and any `--pack-source`. For
each scenario the browser shows the authored expectations — user turns,
target facts, `must_call` / `forbidden_calls`, declared perturbations, the
resolved gate layers, and the `FailureCost` risk weight — plus the tool
surface the pack declares. The tool listing is best-effort and honest:
`wt serve` never starts a mock server, so a server that only knows its tools
once started reports that instead of a fabricated listing.

## Experiment mode: knobs and scoped reruns

```bash
wt serve --experiment --runtime <your-runtime> --pack-source packs/mine.py:PACK
```

`--experiment` (off by default) turns the run screen into an experiment
bench. A runtime that implements the optional `describe_knobs()` capability
([writing a runtime](writing-a-runtime.md)) declares its adjustable
parameters — name, kind (`text` / `enum` / `number` / `flag`), current
value, description — and the run screen renders them as a knob panel. Wind
Tunnel never knows what a knob means, only its shape.

"Rerun this scenario" spawns an ordinary `wt run` subprocess scoped to
exactly that scenario (`--scenario <id> --pack <pack> --knob NAME=VALUE
...`), streams its output live over SSE, and appends the result to the same
ledger under an experiment label:

```
exp-<parent run_id[:8]>-<HHMMSS>
```

so the new row links back to the run it varies: the experiment's run screen
shows "experiment of `<parent>`" with the verdict delta (for example
`PASS → FAIL`), and the parent's screen lists every experiment that varied
it. One rerun runs at a time — concurrent requests are refused — and knob
overrides are validated strictly against the runtime's declaration; an
override the runtime never declared is rejected, never silently dropped.

Without `--experiment` the server keeps its read-only construction: POST is
unimplemented (the stock 501) and the experiment endpoints do not exist.
With it, the serve process itself still writes nothing — all artifacts come
from the spawned `wt run`.

## Live watch

`--live-glob <pattern>` tails JSONL files matching a glob and streams newly
appended complete lines to the Live tab over SSE. It is deliberately
generic — point it at whatever JSONL your runtime writes; the viewer knows
nothing about any particular runtime's log naming. Files that already exist
stream only lines appended after the viewer connects; a truncated or
rotated file restarts from its top. Lines that parse as JSON delta events
(a `type` mentioning `delta` plus a string `text`/`delta`/`content` field)
render as streaming text; everything else renders as raw lines.
