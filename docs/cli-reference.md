# CLI reference

The `wt` command ships eight subcommands in Wind Tunnel 0.5.0.

| Command | Purpose |
|---|---|
| `wt run` | Run scenarios against a runtime and write traces, score sidecars, ledger rows, and optional CI artifacts. |
| `wt report` | Render a `runs/` directory as HTML, Markdown, or JSON. |
| `wt compare` | Compare saved results across variant labels. |
| `wt replay` | Replay a saved trace's last user turn against a runtime and save the new trace. |
| `wt doctor` | Run the reset-isolation canary against a live runtime. |
| `wt import` | Generate a scenario skeleton from a Contract A `*.wtin.json` trace envelope. |
| `wt validate` | Validate and lint Contract A `*.wtin.json` envelopes. |
| `wt triage` | Classify failed runs from saved traces and `.score.json` sidecars. |

Use `wt <command> --help` for the parser's exact current option text.

## `wt run`

```bash
wt run [--scenario S] [--tag TAG] [--pack PACK] [--owner OWNER] \
  [--soul PATH] [--agents PATH] [--runtime RUNTIME] [--label LABEL] \
  [--runs N] [--runs-dir DIR] [--format junit|json --out FILE]
```

Examples:

```bash
wt run --runtime in_memory --scenario lookup_before_action --runs 1
wt run --runtime http_inject --tag dim:recovery --runs 3 --label candidate
wt run --tag dim:recovery --runs 3 --format junit --out results.xml
```

Selectors compose as AND across selector families and OR within repeated
values:

- `--scenario` accepts exact names and shell-style globs such as `lookup_*`.
- `--tag` matches scenario tags such as `dim:recovery`.
- `--pack` matches `ScenarioPack.name`.
- `--owner` matches `ScenarioPack.owner`.

Runtime resolution order:

1. Built-ins: `in_memory` and `http_inject`.
2. Installed entry points in the `windtunnel.runtimes` group.
3. A `module:attr` dotted path resolving to a `RuntimePlugin` instance or class.

The built-in `in_memory` runtime is a scripted learning/runtime-conformance
stub. It never calls tools, so `requires_tool_use` scenarios fail under it by
design.

The built-in `http_inject` runtime speaks Contract C. Configure it with
`WT_INJECT_URL` (default `http://127.0.0.1:8647`) and
`WT_INJECT_TIMEOUT_S` (default `120.0`). See
[writing a runtime](writing-a-runtime.md#the-paved-path-http_inject).

`wt run` writes one trace plus one `.score.json` sidecar per run, appends one
aggregate row per scenario to `<runs-dir>/ledger.ndjsonl`, and exits:

| Exit | Meaning |
|---|---|
| `0` | All gated scenario aggregates passed. |
| `1` | A regression or runtime error occurred. |
| `2` | Usage/configuration error. |

The run gate is the outcome layer only. Trajectory, constraint, and robustness
are still scored and reported, but they do not decide the per-run pass bit.

`--format` and `--out` must be provided together:

- `--format junit --out results.xml` writes one testsuite per pack and one
  testcase per scenario aggregate.
- `--format json --out results.json` writes the same records that were appended
  to the ledger for the sweep.

## `wt report`

```bash
wt report [--runs DIR] [--out FILE] [--format html|markdown|json]
```

Examples:

```bash
wt report --runs runs/ --format html --out report.html
wt report --runs runs/ --format markdown
wt report --runs runs/ --format json --out report.json
```

`html` is the default format. Without `--out`, HTML writes `report.html`,
Markdown prints to stdout, and JSON prints to stdout.

## `wt compare`

```bash
wt compare --labels BASELINE CANDIDATE [--runs DIR]
```

Labels are the `--label` values recorded by `wt run`. The command loads saved
results from the runs directory and prints a pass/fail table by scenario and
label. It exits non-zero if any compared cell is failing.

## `wt replay`

```bash
wt replay --trace PATH [--runtime RUNTIME] [--runs-dir DIR]
```

`wt replay` loads a native Wind Tunnel trace JSON, extracts the last user turn,
runs it against the selected runtime, and saves the replayed trace plus a score
sidecar. It is useful for reproducing runtime behavior from an existing run; it
does not recover the original scenario's full authored gate from the trace.

## `wt doctor`

```bash
wt doctor [--runtime RUNTIME] [--soul PATH] [--label LABEL]
```

`wt doctor` is a bring-up check for a live runtime. It resolves the runtime the
same way `wt run` does, provisions it, and runs `run_reset_canary()` in recall
mode. Recall mode seeds a random nonce, resets the runtime, then asks a fresh
session whether the nonce leaked.

Use it after standing up an endpoint:

```bash
wt doctor --runtime http_inject
```

This requires a live model behind the runtime. In CI without a live model, call
`run_reset_canary(..., probe_recall=False, state_probe=...)` from pytest instead;
that hermetic mode is a library API, not a CLI mode.

## `wt import`

```bash
wt import --trace incident.wtin.json --out scenarios/imported/incident/
```

`wt import` reads a Contract A interchange envelope and writes:

- `scenario.py`
- `scorer.py`
- `fixture.universe.json`
- `IMPORTED.md`

The generated scenario intentionally fails until a human authors the outcome
gate. See [importing a trace](importing-a-trace.md) for the full workflow.

By default, `--out` must be empty or absent. Pass `--force` only when you intend
to overwrite generated files in a non-empty directory.

## `wt validate`

```bash
wt validate [--strict] PATH [PATH ...]
```

`wt validate` parses Contract A interchange envelopes and then runs lint checks
on envelopes that parsed successfully.

Output lines:

- `OK <path>`: the envelope parsed.
- `WARN <path>: <message>`: the envelope parsed but has a suspicious shape.
- `INVALID <path>: <error>`: the envelope failed validation.

Warnings do not fail the command by default. `--strict` treats any warning as a
failure, which is the right mode for producer CI.

Exit codes:

| Exit | Meaning |
|---|---|
| `0` | Every file parsed; no strict-mode warning failure. |
| `1` | A file was invalid, or `--strict` saw at least one warning. |
| `2` | A path was missing. |

## `wt triage`

```bash
wt triage [--runs DIR] [--classifier rule_based|llm_judge]
```

`wt triage` walks saved traces and sibling `.score.json` files, skips passing
runs, and emits a Markdown report grouped by failure category.

The shipped classifier is `rule_based`. The `llm_judge` option is present as a
stub registration point, but the shipped `LLMJudgeClassifier` raises
`NotImplementedError` until an implementation is added.

`wt triage` is informational and exits `0` even when failures are present.
