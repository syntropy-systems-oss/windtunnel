<!-- GENERATED from docs/cli-reference.md at 1f429a054ab8 — do not edit; edit docs/cli-reference.md. -->
---
description: Generated reference for wt CLI subcommands, usage, options, and exit-code
  semantics.
---
<!-- GENERATED from windtunnel.cli argparse at e3460b7ffe83 — do not edit; edit windtunnel/cli.py. -->
# CLI reference

The `wt` command ships 11 subcommands. This page is generated from `windtunnel.cli`'s argparse tree.

| Command | Purpose |
|---|---|
| `wt report` | Generate a report from a runs/ directory. |
| `wt compare` | Compare results across variant labels. |
| `wt run` | Run scenarios against a runtime. |
| `wt rescore` | Re-score saved traces against current scenario definitions. |
| `wt replay` | Replay a captured trace against a runtime. |
| `wt doctor` | Bring-up check: run the reset-isolation canary against a live runtime. |
| `wt surface` | Record or compare the agent's prompt-surface golden (surface diff ⇒ bench run before merge). |
| `wt import` | Generate a scenario skeleton from a Contract A *.wtin.json trace. |
| `wt validate` | Validate Contract A *.wtin.json interchange envelope(s). |
| `wt triage` | Classify failed runs and emit a markdown report grouped by failure category. |
| `wt skill` | Print or install the packaged Wind Tunnel agent skill. |

Exit code conventions: `0` means success, `1` means a runtime failure, regression, world mismatch, or newly-scored outcome failure, and `2` means usage or configuration error.

## `wt report`

Generate a report from a runs/ directory.

Usage:

```bash
wt report [-h] [--runs DIR] [--out FILE] [--format {html,markdown,json}]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runs` | no | runs | Path to the runs/ directory (default: ./runs) |
| `--out` | no |  | Output path for file formats (HTML default: report.html). |
| `--format` | no | html | Output format: html (default), markdown, or json. Choices: html, markdown, json. |

## `wt compare`

Compare results across variant labels.

Usage:

```bash
wt compare [-h] [--labels LABEL [LABEL ...]] [--runs DIR]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--labels` | no | [] | Variant labels to compare (space-separated). |
| `--runs` | no | runs | Path to the runs/ directory (default: ./runs) |

## `wt run`

Run scenarios against a runtime.

Usage:

```bash
wt run [-h] [--scenario S] [--tag TAG] [--pack PACK] [--pack-source SOURCE] [--owner OWNER] [--soul PATH] [--agents PATH] [--runtime RUNTIME] [--hook HOOK] [--label LABEL] [--runs N] [--runs-dir DIR] [--format {junit,json}] [--out FILE]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--scenario` | no |  | Scenario name(s) to run. Repeat for multiple. Omit to run all registered scenarios (the built-in dims plus any pack installed under the 'windtunnel.scenario_packs' entry-point group). Shell-style globs such as 'lookup_*' are supported. |
| `--tag` | no |  | Run scenarios carrying TAG. Repeat for OR matching within tags; composes with other selectors by AND. |
| `--pack` | no |  | Run scenarios from pack PACK. Repeat for OR matching within packs; composes with other selectors by AND. |
| `--pack-source` | no |  | Load an additional local scenario pack from module:attr or path/to/file.py:attr. Repeat for multiple sources; use --pack to select it by name. |
| `--owner` | no |  | Run scenarios from packs whose owner matches OWNER. Repeat for OR matching within owners; composes with other selectors by AND. |
| `--soul` | no |  | Path to SOUL.md / persona doc to inject. |
| `--agents` | no |  | Path to an AGENTS.md operating-notes doc to inject (routed to set-docs --agents; does not touch agent code). |
| `--runtime` | no | in_memory | Runtime to use (default: in_memory). Either the built-in 'in_memory' (zero-infrastructure scripted runtime — no network; useful for learning the scoring model and testing scenario definitions in CI), the name of an installed runtime plugin (discovered via the 'windtunnel.runtimes' entry-point group — e.g. 'acme' from a platform driver package), or a 'module:attr' dotted path to a RuntimePlugin instance or class. |
| `--hook` | no |  | Lifecycle hook to activate for this run. Repeat for multiple hooks; built-ins include 'debrief'. |
| `--label` | no |  | Variant label for this run (recorded in traces). |
| `--runs` | no | 1 | Number of runs per scenario (default: 1). |
| `--runs-dir` | no | runs | Directory to write trace files (default: ./runs). |
| `--format` | no |  | Machine-readable run output format. Must be paired with --out. Choices: junit, json. |
| `--out` | no |  | Path for --format junit/json output. Must be paired with --format. |

## `wt rescore`

Re-score saved traces against current scenario definitions.

Usage:

```bash
wt rescore [-h] (--runs DIR | --trace PATH [PATH ...]) [--write] [--scenario S] [--tag TAG] [--pack PACK] [--pack-source SOURCE] [--owner OWNER]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runs` | no |  | Walk a runs/ directory and re-score every saved trace. |
| `--trace` | no |  | Explicit trace JSON path(s) to re-score. |
| `--write` | no | false | Update .score.json sidecars. Trace files are never modified. |
| `--scenario` | no |  | Only re-score traces whose scenario_id matches S. Repeat for multiple; shell-style globs such as 'lookup_*' are supported. |
| `--tag` | no |  | Restrict scenario definitions to packs/scenarios carrying TAG. |
| `--pack` | no |  | Restrict scenario definitions to pack PACK. |
| `--pack-source` | no |  | Load an additional local scenario pack from module:attr or path/to/file.py:attr before resolving traces. |
| `--owner` | no |  | Restrict scenario definitions to packs whose owner matches OWNER. |

## `wt replay`

Replay a captured trace against a runtime.

Usage:

```bash
wt replay [-h] --trace PATH [--runtime RUNTIME] [--runs-dir DIR]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--trace` | yes |  | Path to the trace JSON file to replay. |
| `--runtime` | no | in_memory | Runtime to replay against: built-in 'in_memory', an installed plugin name (entry-point group 'windtunnel.runtimes'), or a 'module:attr' dotted path to a RuntimePlugin. |
| `--runs-dir` | no | runs | Directory to write replayed traces (default: ./runs). |

## `wt doctor`

Bring-up check: run the reset-isolation canary against a live runtime.

Usage:

```bash
wt doctor [-h] [--runtime RUNTIME] [--soul PATH] [--label LABEL]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runtime` | no | in_memory | Runtime to check (default: in_memory). Resolved exactly like `wt run --runtime`: built-in 'in_memory', an installed plugin name (entry-point group 'windtunnel.runtimes'), or a 'module:attr' dotted path to a RuntimePlugin. Runs the canary in RECALL mode, which requires a live model behind the runtime — doctor is a bring-up tool, not a CI check. For CI runners without a live model, call run_reset_canary(..., probe_recall=False, state_probe=...) directly from pytest instead. |
| `--soul` | no |  | Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`). |
| `--label` | no |  | Variant label recorded for this check (default: wt_doctor). |

## `wt surface`

Record or compare the agent's prompt-surface golden (surface diff ⇒ bench run before merge).

Usage:

```bash
wt surface [-h] {record,diff,check} ...
```

Subcommands:

| Command | Purpose |
|---|---|
| `wt surface record` | Probe the runtime's surface and write the golden (per-segment hashes; no prompt text unless --store-text). |
| `wt surface diff` | Show per-segment changes vs the golden. Informative: exits 0 even when the surface changed. |
| `wt surface check` | CI gate: exit 1 on ANY surface change (or an invalid/absent surface where the golden promises one). A change means: bench before merge. An unchanged surface proves nothing — never use a passing check to skip runs. |

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| _(none)_ | no |  |  |

### `wt surface record`

Probe the runtime's surface and write the golden (per-segment hashes; no prompt text unless --store-text).

Usage:

```bash
wt surface record [-h] [--runtime RUNTIME] [--soul PATH] [--label LABEL] [--golden PATH] [--store-text]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runtime` | no | in_memory | Runtime to probe (default: in_memory). Resolved exactly like `wt run --runtime`. The probe provisions, resets, asks describe_surface(), and tears down — no scenarios run, no model calls. |
| `--soul` | no |  | Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`). |
| `--label` | no |  | Variant label for the probe (default: wt_surface). |
| `--golden` | no | surface.golden.json | Golden file path (default: surface.golden.json). |
| `--store-text` | no | false | ALSO store the full segment text in the golden. The text is a human-facing sidecar — comparison only ever reads hashes — and it embeds the complete prompt surface: treat the file as sensitively as the system prompt itself. |

### `wt surface diff`

Show per-segment changes vs the golden. Informative: exits 0 even when the surface changed.

Usage:

```bash
wt surface diff [-h] [--runtime RUNTIME] [--soul PATH] [--label LABEL] [--golden PATH]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runtime` | no | in_memory | Runtime to probe (default: in_memory). Resolved exactly like `wt run --runtime`. The probe provisions, resets, asks describe_surface(), and tears down — no scenarios run, no model calls. |
| `--soul` | no |  | Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`). |
| `--label` | no |  | Variant label for the probe (default: wt_surface). |
| `--golden` | no | surface.golden.json | Golden file path (default: surface.golden.json). |

### `wt surface check`

CI gate: exit 1 on ANY surface change (or an invalid/absent surface where the golden promises one). A change means: bench before merge. An unchanged surface proves nothing — never use a passing check to skip runs.

Usage:

```bash
wt surface check [-h] [--runtime RUNTIME] [--soul PATH] [--label LABEL] [--golden PATH]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runtime` | no | in_memory | Runtime to probe (default: in_memory). Resolved exactly like `wt run --runtime`. The probe provisions, resets, asks describe_surface(), and tears down — no scenarios run, no model calls. |
| `--soul` | no |  | Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`). |
| `--label` | no |  | Variant label for the probe (default: wt_surface). |
| `--golden` | no | surface.golden.json | Golden file path (default: surface.golden.json). |

## `wt import`

Generate a scenario skeleton from a Contract A *.wtin.json trace.

Usage:

```bash
wt import [-h] --trace PATH --out DIR [--force]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--trace` | yes |  | Path to the Contract A *.wtin.json trace envelope. |
| `--out` | yes |  | Directory to write scenario.py, scorer.py, fixture.universe.json, and IMPORTED.md. |
| `--force` | no | false | Allow writing into an existing non-empty directory. |

## `wt validate`

Validate Contract A *.wtin.json interchange envelope(s).

Usage:

```bash
wt validate [-h] [--strict] PATH [PATH ...]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `paths` | yes |  | Path(s) to *.wtin.json envelope file(s) to validate. |
| `--strict` | no | false | Exit 1 if any file produces a lint warning (e.g. truncated/redacted values, unpaired tool_call_response ids), not only on schema errors. |

## `wt triage`

Classify failed runs and emit a markdown report grouped by failure category.

Usage:

```bash
wt triage [-h] [--runs DIR] [--classifier {rule_based,llm_judge}]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--runs` | no | runs | Path to the runs/ directory (default: ./runs). Each trace must have a sibling .score.json file. |
| `--classifier` | no | rule_based | Classifier to use: rule_based (default, deterministic) or llm_judge (stub — raises NotImplementedError until implemented). Choices: rule_based, llm_judge. |

## `wt skill`

Print or install the packaged Wind Tunnel agent skill.

Usage:

```bash
wt skill [-h] {path,install} ...
```

Subcommands:

| Command | Purpose |
|---|---|
| `wt skill path` | Print the absolute path of the installed Wind Tunnel skill directory. |
| `wt skill install` | Install the Wind Tunnel skill into an agent skills directory. |

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| _(none)_ | no |  |  |

### `wt skill path`

Print the absolute path of the installed Wind Tunnel skill directory.

Usage:

```bash
wt skill path [-h]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| _(none)_ | no |  |  |

### `wt skill install`

Install the Wind Tunnel skill into an agent skills directory.

Usage:

```bash
wt skill install [-h] [--dest DIR] [--copy]
```

Arguments and options:

| Name | Required | Default | Help |
|---|---:|---|---|
| `--dest` | no | .agents/skills | Directory that will receive a windtunnel skill entry (default: .agents/skills). |
| `--copy` | no | false | Copy instead of symlinking. The copy survives package uninstall but may go stale. |
