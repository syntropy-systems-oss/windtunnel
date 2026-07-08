<!-- GENERATED from docs/using-hooks.md at 0eb99ac62286 — do not edit; edit docs/using-hooks.md. -->
---
description: "Task guide for enabling Wind Tunnel lifecycle hooks, reading debrief artifacts, and registering custom hooks."
---
# Using Hooks

Hooks let code observe a Wind Tunnel run at fixed lifecycle points and emit
sidecar artifacts. They are deliberately not a second scoring system. A hook can
watch, ask one post-score follow-up turn, and write JSON through the runner; it
cannot change the trace, reset the agent, edit steering, or decide whether the
run passed.

## Quickstart: debrief failed runs

Turn on the built-in debrief hook with `--hook debrief`:

```bash
uv run wt run --runtime <your-runtime> --scenario <scenario-name> --runs 1 --hook debrief
```

`wt run` still writes the normal trace and score sidecar:

```text
runs/.../<stem>.json
runs/.../<stem>.score.json
```

When `debrief` fires, it writes one more sidecar beside them:

```text
runs/.../<stem>.debrief.json
```

The debrief artifact contains the run identity, scenario id, agent/model labels,
headline verdict, failed score layers, each layer's reason string, the exact
diagnostic prompt sent back to the agent, the agent's reply, timeout/error
metadata, and `tools_disabled: false`. That last field is intentional: hook
conversation uses the same session and runtime `send()` path, and the current SPI
cannot disable tools for that extra turn. The scored trace is already frozen
before the debrief happens, so this cannot change the verdict, but the artifact
should still say what happened.

By default, debriefs are emitted only for runs with at least one failed score
layer. To include passing runs too:

```bash
WT_DEBRIEF_ON=all uv run wt run --runtime <your-runtime> --runs 1 --hook debrief
```

The follow-up turn has a framework-owned deadline. The default is 30 seconds;
override it when your runtime has a slower worst-case response path:

```bash
WT_HOOK_CONVERSE_TIMEOUT_S=90 uv run wt run --runtime <your-runtime> --runs 1 --hook debrief
```

## How to read a debrief

Read a debrief as a lead, not a verdict.

It is strongest when the model quotes concrete tool or environment evidence it
actually received: schema errors, missing capabilities, malformed results,
timeouts, empty tool responses, or surprising response shapes. The model had
privileged observability of those strings from inside the run; the scorer may
only see their effect.

It is weakest when it explains steering decisions after the fact. Questions like
"why did you not clarify?" invite confabulation. That is why the debrief prompt
asks for environment and tool errors first, blockers second, and only then asks
what the agent would do differently. If the reply is short, lazy, or truncated,
the first section is the part most likely to be useful.

Debriefs never gate. They do not change `Score`, JUnit output, report totals, or
the `wt run` exit code. Wind Tunnel also never consumes a debrief to auto-edit a
system prompt, skill, scenario, or runtime. Use it in the triage ladder: wiring,
harness, steering, model. Do not let a model's self-report rewrite the bench
that judged it.

## Writing your own hook

Subclass `Hook` and override only the lifecycle points you need:

```python
from windtunnel.spi.hooks import Hook, HookContext


class RunNotesHook(Hook):
    name = "run_notes"

    def on_run_scored(self, ctx: HookContext) -> None:
        ctx.emit_artifact({
            "schema_version": 1,
            "run_id": ctx.run_id,
            "scenario_id": getattr(ctx.scenario, "name", ""),
            "outcome_passed": bool(ctx.score and ctx.score.outcome.passed),
        })
```

Hook names and artifact labels must be filesystem-safe slugs matching
`[a-z0-9_-]+`. The name is both the `--hook` activation token and the artifact
filename discriminator.

| Lifecycle point | When it fires | What you can rely on |
|---|---|---|
| `on_provisioned` | Once per scenario batch, after `provision()` and world preconditions | The handle exists; no run has started; no trace or score exists yet. |
| `on_run_start` | Per run, after `reset_state()` and surface capture, before the first inject | The session is fresh and `session_id` is minted; nothing has been sent. |
| `on_run_scored` | Per run, after `Score` is computed | The trace, MCP evidence, observations, and score are frozen; the session is still alive; `ctx.converse()` is valid. |
| `on_run_end` | Per run, immediately after `on_run_scored` hooks finish | The run result is final; the next event is another reset or scenario teardown. |
| `on_scenario_end` | Once per scenario, after all runs | `ctx.aggregate` holds the scenario's `AggregateResult`. |
| `on_pack_end` | Once per `wt run` invocation, after the sweep loop | `ctx.aggregate` holds the completed scenario aggregates. |

`HookContext` is intentionally scoped. It exposes read-only run metadata such as
`ctx.scenario`, `ctx.agent`, `ctx.run_id`, `ctx.session_id`, `ctx.trace`,
`ctx.score`, and `ctx.aggregate`, depending on the lifecycle point. Treat those
objects as immutable. The framework passes live objects for cost reasons, but
mutation is undefined behavior.

The hook capabilities are:

| API | Where it works | Meaning |
|---|---|---|
| `ctx.converse(text) -> str` | Only in `on_run_scored` | Sends one diagnostic user turn into the same run session and returns normalized assistant text. The turn is never appended to `trace.turns`. |
| `ctx.emit_artifact(payload, label=None)` | Any hook point | Buffers JSON-serializable payloads for the CLI to persist. Run artifacts are written beside the trace; scenario and pack artifacts are written at the runs-directory root. |
| `ctx.warn(message)` | Any hook point | Records a non-fatal hook warning through the same `hook:<name>: ...` channel used for contained hook exceptions. |

Hooks never receive filesystem paths. `emit_artifact()` is buffered because only
the CLI knows where the trace and score sidecars are being written.

Failure containment is part of the contract. A broken hook never fails a run. If
a hook raises, Wind Tunnel records a `hook:<name>: ...` warning with the run
story and continues. If `ctx.converse()` times out, the hook sees an error and
the artifact can record `timed_out: true`; the score remains whatever it already
was.

Register external hooks with the same entry-point pattern used for runtimes and
scenario packs:

```toml
[project.entry-points."windtunnel.hooks"]
run_notes = "my_windtunnel_hooks.hooks:RunNotesHook"
```

The entry point may load a `Hook` instance or a `Hook` class; classes are
instantiated with no arguments. After changing entry points, reinstall the
package before trusting discovery.

Activation is explicit:

```bash
uv run wt run --runtime <your-runtime> --hook run_notes
```

Installing a hook package never changes bench behavior by itself. That rule is
not ceremony: gating benches need comparable artifacts across installs and CI
images. No `--hook` flag means no hooks run, even if hook packages are installed.

## Continuous state probes

Subclass `StateProbeHook` when `reset_state()` should return a fixture to the
same external state before every run. The first post-reset snapshot becomes the
baseline; later post-reset mismatches report that the previous run may have
leaked state past reset.

```python
from pathlib import Path

from windtunnel.hooks.state_probe import StateProbeHook


class TempStateDirHook(StateProbeHook):
    name = "tmp_state"

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def capture_state(self) -> dict:
        return {
            "files": sorted(
                str(path.relative_to(self.root))
                for path in self.root.rglob("*")
                if path.is_file()
            )
        }
```

A violation on run 2 appears on that run's trace warning channel:

```text
hook:tmp_state: post-reset state differs from the first-run baseline; the previous run may have leaked past reset_state(): changed keys files
```

It also emits a bounded sidecar, for example
`<stem>.tmp_state.violation.json`:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "baseline_run_id": "...",
  "violation": "post_reset_state_mismatch",
  "difference": {"summary": "changed keys files"}
}
```

`StateProbeHook` reuses the same `StateProbe.capture()` snapshot shape used by
`run_reset_canary(..., state_probe=...)`, but it does not replace the canary.
The canary remains the `wt doctor` hard gate; continuous probes report through
warnings and artifacts only, never through scores or exit codes.

For the complete contracts, ordering guarantees, artifact naming rules, and the
debrief schema, see [Design 0003: lifecycle hooks](design/0003-hook-system.md).
