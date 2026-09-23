---
description: "Tight iteration loop for people and coding agents: rescore saved traces, tabulate results and metrics per label, and compare labels."
---
# Iterating on an agent

A bench is most useful when a change can be measured in one command. This
page is the loop an operator — or a coding agent driving `wt` from a shell —
repeats: change something, run it under a new label, read the results, compare
against the baseline, and re-score old traces when only the scorer changed.

Every command here reads the same files `wt run` writes (`<trace>.json`, its
`.score.json` sidecar, and `ledger.ndjsonl`) and never provisions a runtime.
Each one has a `--json` mode whose document is stable and versioned, so an
agent never has to parse the human table.

## 1. Label every round

`--label` is the unit everything else groups by. Use a new label per change:

```bash
wt run --pack my_pack --runs 5 --label baseline
# ...edit the prompt, the agent, or the model config...
wt run --pack my_pack --runs 5 --label candidate
```

Re-using a label is allowed. Reports, `wt compare`, and `wt results` then read
the label's latest sweep as recorded in the ledger (or every saved run with the
label when the ledger has no row for it, e.g. a directory of copied traces).

## 2. Tabulate a label: `wt results`

```bash
wt results --runs runs/ --label candidate
```

```text
label candidate: 2 scenario(s), 10 run(s) under runs
  lookup_order                             FAIL               4/5 pass (80%)
    outcome.final_correct  80% true (4/5)
    outcome.revisions      mean 8.2  min 6  max 11  (n=5)
  refund_flow                              PASS               5/5 pass (100%)
```

Per scenario it shows the headline verdict, pass counts, and every
[metric](writing-a-scenario.md#metrics-measure-alongside-the-verdict) the
scorers reported, aggregated by type: a rate for booleans, mean/min/max for
numbers, value counts for strings. Omit `--label` to summarize every label;
repeat it for several; narrow with `--scenario 'lookup_*'`.

`--json` adds each run's trace path, run id, verdict, and raw metrics, so the
next step can open the exact failing trace:

```json
{
  "windtunnel_results": 1,
  "runs_dir": "runs",
  "labels": ["candidate"],
  "results": [
    {
      "scenario_id": "lookup_order",
      "label": "candidate",
      "verdict": "FAIL",
      "runs": 5, "passed": 4, "failed": 1, "invalid": 0, "pass_rate": 0.8,
      "selection": "latest_aggregate",
      "metrics": {
        "outcome.final_correct": {"kind": "bool", "count": 5, "true_count": 4, "rate": 0.8},
        "outcome.revisions": {"kind": "number", "count": 5, "mean": 8.2, "min": 6, "max": 11}
      },
      "traces": [
        {"path": "runs/lookup_order/.../20260102T030405000000Z_abcd1234.json",
         "run_id": "abcd1234-...", "started_at": "2026-01-02T03:04:05+00:00",
         "verdict": "FAIL", "metrics": {"outcome.final_correct": false, "outcome.revisions": 11}}
      ]
    }
  ]
}
```

`selection` says which runs were counted: `latest_aggregate` (the runs of the
label's newest ledger row) or `all_traces` (no ledger row: every saved run with
the label). `wt results` exits `2` when the runs directory is missing or no
requested label exists, naming the labels that do.

## 3. Compare against the baseline: `wt compare`

```bash
wt compare --labels baseline candidate
```

The first label is the baseline. After the verdict table and the risk-ranked
verdict changes, `wt compare` prints each shared scenario's metric deltas:

```text
Metric deltas (vs baseline):
  candidate       lookup_order                             outcome.final_correct  rate 60% -> 80% (+20pp)
  candidate       lookup_order                             outcome.revisions      mean 9.4 -> 8.2 (-1.2)
```

Numbers compare means, booleans compare rates (in percentage points), and
strings compare per-value counts. A metric reported under only one label shows
`absent` on the other side. The exit code is `1` only for a verdict regression:
metric movement is information, never a gate. `--json` emits the verdicts,
changes, and `metric_deltas` as one document.

## 4. Re-score instead of re-running: `wt rescore`

When only the scorer changed, the saved traces already hold the evidence:

```bash
wt rescore --runs runs/ --label candidate --json
```

The JSON lists, per trace, the old and new verdict of every layer, the old and
new headline verdict, and old and new metrics, so the effect of an outcome
function edit is visible in one command. Add `--write` to update the sidecars
(traces are never modified); `wt results` and `wt compare` then read the new
scores. See [fast scorer iteration](writing-a-scenario.md) for the details.
