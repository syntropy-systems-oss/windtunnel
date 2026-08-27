<!-- GENERATED from docs/viewing-runs.md at 0f09c7aba345 — do not edit; edit docs/viewing-runs.md. -->
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
matching primitives against the stored trace and knows *where* each
expectation was met or missed. One interaction model covers every
evidence class — **status lives on the contract, illumination lives in
the transcript, interaction bridges them**:

- the transcript is fully neutral at rest: fact/number spans,
  forbidden-assertion spans, tool-call tokens, and policy anchors are
  pre-rendered as invisible marks and nothing is lit until you interact;
- hovering any anchorable contract entry — a fact group, a `NumberFact`,
  a forbidden fact, a `must_call`, a `forbidden_calls`, an anchored
  policy — illuminates exactly that entry's spans/tokens (green for
  satisfied/found, red for violated/asserted) and, after a short
  hover-intent dwell, scrolls the transcript pane to the first lit mark;
- clicking toggles that entry's **lock** — each interactive entry carries
  a checkbox showing its lock state, and multiple entries can be locked
  at once, so you can build up a set of held highlights and scroll
  freely. Hover adds a temporary layer on top of the lock-set; unhover
  removes only that layer;
- each contract entry keeps its at-rest verdict: "said" / "never said"
  for facts, "called via `<observed name>`" / "never called" for
  `must_call`, "clean" / "called" for `forbidden_calls`, "held" /
  "violated" for recorded policies, "applied" / "not applied" for
  perturbation markers;
- matches are computed server-side by the same `tool_name_matches`
  comparisons the trajectory evaluator uses, at token precision: the
  exact canonical name is marked inside a platform-decorated call, not
  the whole block. When the evidence source is the server's own call
  log, a server-computed witnessed→transcript mapping (greedy in-order
  name walk) lights the corresponding claimed call blocks too; a
  witnessed call the transcript never claimed is labeled "no matching
  transcript call" rather than silently skipped. A miss is precision
  too: "never called" carries the definitive red state on the contract
  side — "no call matched — this is the failure" — because absence has
  nothing to highlight;
- a policy that returns an anchored verdict (`PolicyVerdict` with
  `EvidenceAnchor` references — see [writing a
  scenario](writing-a-scenario.md)) is hoverable like any call entry: its
  witnessed-call and span anchors illuminate, an observation anchor whose
  entry the server can pair with a rendered call (a recognizable tool
  name, matched by name + occurrence order) illuminates that call, and
  everything else — free-text locators, unpaired observation anchors —
  renders as "looked at: observations.tool_results[3] — …" under the
  entry. Entries with nothing to
  illuminate — plain-predicate policies, custom `TrajectoryCheck`s,
  `outcome_fn` — are visibly non-interactive and say so ("opaque policy —
  no transcript anchor"); nothing ever looks hoverable and does nothing.
  For those opaque checks, "what exactly is windtunnel expecting" is one
  click away: a "show check source" toggle renders the callable's own
  source (read-only introspection of the loaded pack; unavailable source
  — builtins, vanished files — keeps the plain opaque note);
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

## Comparing runs and recording preferences

Runs that share a `scenario_id` are **siblings** — the same task, sampled
completions. Same label = the same arm; different labels = different arms
(a cross-arm comparison). The run screen lists a run's siblings
("compare with sibling"), and `#/compare/<run_id_a>/<run_id_b>` is
deep-linkable: two transcript panes side by side beneath one shared
scenario contract whose entries illuminate **both** panes (the same
hover/lock model as the run screen; A/B dual status per entry).

```bash
wt serve --annotate --annotator reviewer-1
```

`--annotate` (off by default) adds preference capture to the compare
view: **prefer A / no preference / prefer B** — keyboard `a`/`←`,
`n`, `d`/`→` for fast labeling. Each judgment appends one NDJSON row to
`<runs-dir>/annotations.ndjsonl`:

```json
{"ts": "…", "scenario_id": "…", "label_a": "…", "run_id_a": "…",
 "label_b": "…", "run_id_b": "…", "preferred": "a" | "b" | null,
 "annotator": "reviewer-1"}
```

Append-only, beside the ledger — runs are never edited, and identity
fields come from the stored traces, not the client. Without the flag the
server keeps its read-only posture (the control is hidden and the write
endpoint refuses); recorded annotations still *display* everywhere — on
run screens and the compare view — read tolerantly like the ledger.

`#/queue` is the fast-labeling loop: the server picks the next sibling
pair this annotator hasn't judged (within-label pairs, cross-label pairs,
or both — a selector on the view), shows progress (n labeled / n
available), and advances automatically after each judgment, so you can
sit and label continuously — building a preference dataset straight from
bench artifacts.

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
