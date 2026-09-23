---
description: "Tight iteration loop for people and coding agents: follow sweeps live, tabulate results and metrics per label, compare labels, and rescore saved traces."
---
# Iterating on an agent

A bench is most useful when a change can be measured in one command. This
page is the loop an operator — or a coding agent driving `wt` from a shell —
repeats: change something, run it under a new label, read the results, compare
against the baseline, and re-score old traces when only the scorer changed.

Every command here reads the files `wt run` writes (`<trace>.json`, its
`.score.json` sidecar, `ledger.ndjsonl`, and `events.ndjsonl`) and never
provisions a runtime.
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

`wt run` writes each run's trace and `.score.json` sidecar the moment that run
is scored, and each scenario's ledger row the moment its last run finishes, so
a long sweep leaves its evidence behind as it goes and a killed sweep keeps
everything it finished. The file layout is the same as it has always been.

## 2. Follow a sweep: `wt watch`

Every sweep also appends progress events to `<runs-dir>/events.ndjsonl`, and
`wt watch` turns them into one line per event:

```bash
wt run --pack my_pack --runs 3 --label candidate &     # prints: wt run: sweep 3f2a9c1b7e4d — ...
wt watch --runs runs/ --label candidate
```

```text
14:02:10 sweep 3f2a9c1b7e4d started: label candidate, runtime my_runtime, 2 scenario(s) x 3 run(s)
14:02:10 lookup_order run 1/3 started
14:02:31 lookup_order run 1/3 PASS (20.8s) outcome.final_correct=True outcome.revisions=6
14:02:31 lookup_order run 2/3 started
14:02:55 lookup_order run 2/3 FAIL (23.9s) failed: outcome outcome.final_correct=False outcome.revisions=11
...
14:03:40 lookup_order FAIL 2/3 pass
14:04:52 sweep 3f2a9c1b7e4d finished: exit 1 (completed; 2/2 scenario(s), 0 error(s))
```

It exits when the sweep ends, **with the sweep's own exit code**, so an agent
can background `wt run` and block on `wt watch` without losing the verdict.

- `--label L` follows the newest sweep with that label that is still running
  or started in the last few seconds, and otherwise waits for the next one to
  start. `--sweep ID` (the id `wt run` prints on stderr) follows exactly one
  sweep and replays it if it has already finished — the race-free choice.
- A sweep whose process dies without finishing (killed, crashed) ends the
  watch with exit `1`; its completed runs are already on disk. `--timeout S`
  gives up after S seconds with exit `124`.
- `--json` prints the raw event lines. Events are `sweep_started`,
  `run_started`, `run_finished` (verdict, per-layer pass/fail, metrics, trace
  path, duration), `scenario_finished` (aggregate verdict and pass counts),
  `scenario_error`, and `sweep_finished` (exit code and status: `completed`,
  `aborted` by the circuit breaker, or `error`). Every event carries
  `windtunnel_event: 1`, a timestamp, the `sweep_id`, and the `label`.

The event stream is progress, not record: nothing reads it to decide a
verdict, and a failure to write it only warns.

### Running scenarios concurrently

A sweep is a list of **jobs**, one per selected scenario: a job provisions a
handle, drives that scenario's `--runs` runs on it, and records the results. A
scheduler decides how the jobs execute:

```bash
wt run --pack my_pack --runs 5 --label candidate                                   # sequential (default)
wt run --pack my_pack --runs 5 --label candidate --scheduler concurrent --max-concurrency 4
wt run --pack my_pack --label candidate --scheduler my_pkg.schedulers:Priority     # your own
```

- `sequential` runs one job at a time, exactly as sweeps always have.
- `concurrent` runs up to `--max-concurrency` jobs at once on a thread pool.
  It never exceeds the runtime plugin's declared `max_concurrency` — which is
  1 unless the plugin says otherwise, because many runtimes bind fixed ports
  or share one backend — and it prints one line whenever it clamps a request.
  Lifecycle hooks force one job at a time, since hooks may keep state that
  assumes sequential runs. Summary lines print as jobs finish; `--format`
  output and the report keep selection order.
- `package.module:Class` or `path/to/file.py:Class` loads a subclass of
  `windtunnel.spi.Scheduler`: implement `execute(jobs, stop)` — run each
  `RunJob` at most once, start none after `stop` is set, never more than
  `self.max_concurrency` at once, and re-raise the first exception a job
  raises. The CLI still enforces the runtime's limit around every job.

The circuit breaker is unchanged: three consecutive scenario errors stop the
sweep from starting new jobs.

### Sharing a runtime: the run lock

Two sweeps against one runtime — two shells, two agents, a CI step and a
developer — would reset each other's sessions and fight over its ports. So a
sweep takes a machine-wide lock for its runtime before building it and holds
it until `post_run()` returns. A second sweep waits, saying who it waits for:

```text
wt run: runtime 'my_runtime' is in use (held by pid 4242 on bench-host, since 2026-01-02T03:04:05Z, running `wt run --pack my_pack --label baseline`); waiting for it to finish — pass --no-wait to exit instead
```

`--no-wait` exits `75` at once instead, for callers that would rather retry
than queue. The lock is an OS file lock, released by the kernel even when its
holder is killed, so it never goes stale. It covers every runtime whose plugin
declares a finite `max_concurrency` (the default); runtimes that declare no
limit, such as `in_memory`, are never locked. The lock is keyed by runtime
name, or by the plugin's `lock_key(runtime_name)` when one name can reach
different backends (`http_inject` keys by its endpoint URL). Lock files live in
`$WT_LOCK_DIR`, else a per-user directory under the system temp dir.

### Queue rounds: `wt batch`

To queue several rounds with one command, write one `wt run` spec per line —
the same options `wt run` takes, shell-quoted, with `#` comments and an
optional leading `wt run` — and hand the file to `wt batch`:

```text
# rounds.txt
--pack my_pack --runs 5 --label baseline
--pack my_pack --runs 5 --label candidate --agents notes/candidate.md
wt run --pack my_pack --runs 5 --label candidate-t0 --soul prompts/strict.md
```

```bash
wt batch rounds.txt --runs-dir runs/ --scheduler concurrent
printf -- '--pack my_pack --label again\n' | wt batch -     # specs from stdin
```

Every line is parsed before the first spec runs, so a typo on line 9 fails the
batch (exit `2`, naming the line) without spending rounds 1–8. Specs then run
in file order, each as its own sweep — own sweep id and events, own ledger
rows, own runtime lock — and a failing spec never stops the next. The batch's
`--runs-dir`, `--scheduler`, `--max-concurrency`, and `--no-wait` are defaults
for every spec; a spec's own value wins. The batch exits with the highest exit
code any spec returned and prints one summary line per spec on stderr. Follow
any round with `wt watch --label <its label>`.

## 3. Tabulate a label: `wt results`

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

## 4. Compare against the baseline: `wt compare`

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

## 5. Re-score instead of re-running: `wt rescore`

When only the scorer changed, the saved traces already hold the evidence:

```bash
wt rescore --runs runs/ --label candidate --json
```

The JSON lists, per trace, the old and new verdict of every layer, the old and
new headline verdict, and old and new metrics, so the effect of an outcome
function edit is visible in one command. Add `--write` to update the sidecars
(traces are never modified); `wt results` and `wt compare` then read the new
scores. See [fast scorer iteration](writing-a-scenario.md) for the details.
