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

## Run drill-down

Clicking a ledger row loads each run's saved trace plus its `.score.json`
sidecar — the same artifacts `wt report`, `wt triage`, and `wt rescore`
consume. The drill-down puts the *why* front and center: every failing
layer's `detail` string is surfaced first, followed by all four layer
verdicts (outcome, trajectory, constraint, integrity), the conversation
turns with their tool calls, and — when a logging mock was in play — the
tool calls witnessed at the tool server itself (`mcp_calls`), independent of
what the transcript claims.

## Scenario browser

Scenario packs are discovered exactly like `wt run`: the built-in dims, the
`windtunnel.scenario_packs` entry-point group, and any `--pack-source`. For
each scenario the browser shows the authored expectations — user turns,
target facts, `must_call` / `forbidden_calls`, declared perturbations, the
resolved gate layers, and the `FailureCost` risk weight — plus the tool
surface the pack declares. The tool listing is best-effort and honest:
`wt serve` never starts a mock server, so a server that only knows its tools
once started reports that instead of a fabricated listing.

## Live watch

`--live-glob <pattern>` tails JSONL files matching a glob and streams newly
appended complete lines to the Live tab over SSE. It is deliberately
generic — point it at whatever JSONL your runtime writes; the viewer knows
nothing about any particular runtime's log naming. Files that already exist
stream only lines appended after the viewer connects; a truncated or
rotated file restarts from its top. Lines that parse as JSON delta events
(a `type` mentioning `delta` plus a string `text`/`delta`/`content` field)
render as streaming text; everything else renders as raw lines.
