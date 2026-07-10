---
description: "Design specification for lifecycle hooks: the windtunnel.hooks plugin SPI, per-point ordering contracts, the scoped hook context, sidecar artifacts, and the debrief reference hook."
---
# Design 0003: lifecycle hooks

**Status:** draft · **Scope:** the hook plugin SPI, the dispatch and
ordering contracts at each lifecycle point, the scoped context handed to
hooks, hook sidecar artifacts, the `debrief` reference hook that ships
with the framework, and the abstract `StateProbeHook` base for continuous
isolation checks. **Non-goals:** removing or deprecating the reset
canary's `state_probe` kwarg, per-hook config plumbing beyond environment
variables, and anything that lets a hook's output gate a verdict or
modify steering.

Wind Tunnel runs have a fixed skeleton: provision, reset, inject, score,
repeat. This document adds named observation points to that skeleton so
that code which needs to *watch* a run — not change it — can be written as
a plugin instead of a fork. A hook observes the run at a declared
lifecycle point and emits artifacts beside the trace. It never scores,
never gates, and never mutates the world being judged.

## Why hooks, and why now

This is an abstraction extracted from three concrete consumers, not a
plugin system built on spec:

1. **Debrief** — after the score is computed and while the session is
   still alive, send the verdict back to the agent under test and record
   its self-report as an artifact. The rationale: the model has
   *privileged observability of the environment*. It is the only
   component that saw every tool result, every error string, every weird
   response shape from inside the run. Self-reports are most reliable on
   exactly the failure class that dominates real triage — wiring and
   harness faults — because there the model is quoting concrete evidence
   it received, not reconstructing intent.
2. **State probes** — `run_reset_canary(..., state_probe=...)` is already
   a hook trying to be born as a bespoke kwarg on one API. As a lifecycle
   hook, isolation gets asserted on every run of a real pack instead of
   only inside `wt doctor`.
3. **Prompt-surface capture** — shipped in 0.6 as purpose-built runner
   code (`Trace.surface`). It is shaped exactly like a hook: at a
   lifecycle point, observe the run, freeze what you saw into an
   artifact. Had hooks existed first, it would have been one.

Three consumers, one shape: *at lifecycle point X, observe the run and
emit an artifact*. That clears the emergence-then-abstraction bar.

The reason this must live in the framework rather than in wrappers is
ordering. A debrief is only sound if it runs **after** the scorer has
captured fixture and world state (so the extra turn cannot perturb what
is being judged) and **before** the next state reset (so the session
still has the run in context). Only the runner can guarantee that window;
a wrapper around `run_scenario()` never sees it.

## The plugin SPI

A hook is an object — typically a class instance — that implements any
subset of the lifecycle methods:

```python
class Hook:
    """Base class for lifecycle hooks. Subclass and override the points
    you care about; unimplemented points cost nothing."""

    name: str  # stable identifier; used in artifact filenames and --hook

    def on_provisioned(self, ctx): ...
    def on_run_start(self, ctx): ...
    def on_run_scored(self, ctx): ...
    def on_run_end(self, ctx): ...
    def on_scenario_end(self, ctx): ...
    def on_pack_end(self, ctx): ...
```

One entry point registers one hook *plugin*, not one callable per
lifecycle point. Real hooks span multiple points with shared state (a
state probe wants an `on_run_start` baseline and an `on_run_end`
assertion; a debrief may want a per-pack summary), and activation should
be one user-facing name. The framework introspects which methods the
plugin actually defines, pytest-plugin style, and dispatches only those.

`name` must be a stable, filesystem-safe slug (`[a-z0-9_-]+`). It is the
artifact discriminator and the activation token; renaming it is a
breaking change for any tooling that reads the hook's artifacts.

### Registration and activation

Discovery follows the existing plugin pattern exactly. Built-in hooks
live in an internal registry (as built-in runtimes do); external hooks
register under a `windtunnel.hooks` entry-point group, the idiomatic
third leg beside `windtunnel.runtimes` and `windtunnel.scenario_packs`:

```toml
[project.entry-points."windtunnel.hooks"]
acme_state_probe = "acme_bench.hooks:AcmeStateProbeHook"
```

The registered object may be a `Hook` instance or a class; a class is
instantiated with no arguments (same contract as runtime plugins).

Activation is **explicit, per invocation**:

```
wt run --hook debrief [--hook other] ...
```

Installing a package must never silently change bench behavior. Gating
benches need byte-comparable artifacts unless a hook was deliberately
enabled, so there is no "auto-enable on install" path and no config file
that turns hooks on ambiently. No `--hook` flag → zero hooks → byte-for-
byte the behavior of a hook-less Wind Tunnel. An unknown hook name is a
hard CLI error listing the available names (built-ins plus entry points).

Hook-specific knobs are environment variables, namespaced
`WT_<HOOKNAME>_*` (the convention `WT_INJECT_URL` established). 0.7.0
deliberately ships no per-hook config-file plumbing.

## Lifecycle points and ordering contracts

The per-run sequence in `run_scenario()` / `_run_once()` today, with hook
points marked:

```
provision handle; check world preconditions
  → on_provisioned
for each run:
    reset_state()
    mint session_id; reset probes; capture surface
      → on_run_start          (post-reset, pre-first-inject)
    inject turns; collect replies
    capture MCP evidence + fixture/world observations
    build Trace (evidence frozen)
    compute Score
      → on_run_scored         (session alive; next reset has NOT happened)
      → on_run_end            (the run's slot is closing)
teardown handle
  → on_scenario_end           (AggregateResult available)
...CLI sweep loop completes all scenarios...
  → on_pack_end               (all aggregates available)
```

| Point | Fires | Guarantees |
|---|---|---|
| `on_provisioned` | once per scenario batch, after `provision()` and world-precondition checks | handle exists; no run has started; no trace/score in context |
| `on_run_start` | per run, after `reset_state()` and surface capture, before the first inject | session is fresh; `session_id` minted; nothing injected yet |
| `on_run_scored` | per run, after `Score` is computed | trace evidence and fixture/world state already captured; **the session is still alive and the next reset has not happened**; `ctx.converse()` is valid only here |
| `on_run_end` | per run, immediately after `on_run_scored` dispatch completes | the run's result is final; the next lifecycle event is the next run's reset or the scenario teardown; observation only |
| `on_scenario_end` | once per scenario, after the run loop, before `teardown()` returns the result | `ctx.aggregate` holds the scenario's `AggregateResult` |
| `on_pack_end` | once per `wt run` invocation, after the sweep loop | all completed scenario aggregates available |

`on_run_scored` is the load-bearing contract and the reason hooks are
framework machinery. Note the asymmetry with the original sketch of
"post-score, pre-reset": in the current runner there is **no post-run
reset** — the next `reset_state()` happens at the top of the next loop
iteration (`run_scenario` in `windtunnel/api/runner.py`).
The guarantee is therefore stated as *the next reset has not happened*,
which is the property a debrief actually needs, rather than a claim about
where reset code sits.

A note on naming: earlier sketches had a single `on_pack_end (aggregate
available)`. In the shipped runner the aggregate (`AggregateResult`) and
the handle teardown are **per-scenario**, while "everything finished" is
a CLI-sweep-level event; conflating them would hand hooks either a fake
aggregate or a dead handle. The design splits them: `on_scenario_end`
(runner-fired, aggregate in context) and `on_pack_end` (CLI-fired, all
aggregates). Most hooks implement neither.

Hooks fire **in activation order** (the order of `--hook` flags), and
each point's dispatch completes before the runner proceeds. Hooks are
synchronous; the framework owns the deadline (§ Failure containment).

## The scoped context

Hooks do not receive the raw `AgentHandle`. They receive a `HookContext`
that makes ordering violations structurally impossible — there is no
`reset_state()`, no `teardown()`, no raw `send()` to call out of turn.

Read-only members (populated per point; absent members are `None`):

- `ctx.scenario` — scenario identity/metadata
- `ctx.agent` — the `AgentConfig` (label, soul, model metadata)
- `ctx.run_id`, `ctx.session_id` — the current run's identifiers
- `ctx.trace` — the frozen `Trace` (from `on_run_scored` onward)
- `ctx.score` — the computed `Score` (from `on_run_scored` onward)
- `ctx.aggregate` — the `AggregateResult` (`on_scenario_end` onward)

Hooks MUST treat these as immutable. The framework passes its live
objects for cheapness; mutating them is undefined behavior and a bug in
the hook, not a supported channel. (Python cannot enforce deep
immutability without copying every trace; the contract is stated instead
of paid for.)

Capabilities:

- **`ctx.converse(text) -> str`** — send a text turn into the run's
  session and return the normalized reply. Valid **only** during
  `on_run_scored`; any other point raises. Mechanically this is
  `handle.send()` with the run's own `session_id`, so the agent answers
  with the run in context. The turn is **never appended to
  `trace.turns`** — the trace is already frozen — so trajectory scorers,
  latency stats, and replay are untouched. Hooks may call `converse()`
  more than once during `on_run_scored`; the framework records metadata
  per call, and artifact stamping accumulates timeout/error state across
  those calls (`timed_out: true` if any converse call timed out).

  Tools cannot currently be disabled per-turn: the SPI's `send()` has no
  such parameter and `SamplingConfig.tool_choice` is provision-time
  config. A converse turn may therefore trigger tool calls. This is
  sound only because the contract places converse after evidence capture
  — the world being judged is already frozen — but it must be **recorded,
  not silently allowed**: the framework stamps `tools_disabled: false`
  into any artifact whose hook used `converse`, so a reader knows the
  self-report could have had side effects in the (already-judged)
  fixture world.

- **`ctx.emit_artifact(payload, label=None)`** — record a JSON-serializable
  payload as a hook artifact. Artifacts are **buffered, not written**:
  the runner has no knowledge of artifact paths (that is CLI territory),
  so emissions attach to the run result and the CLI persists them beside
  the trace and score files it already writes. Hooks never receive
  filesystem access.

- **`ctx.warn(message)`** — append a non-fatal diagnostic to the same
  hook-warning channel used for contained hook exceptions. Run-scoped
  warnings land in `trace.worker_warnings` as `hook:<name>: <message>`;
  scenario- and pack-scoped warnings are surfaced by the CLI. A warning
  is evidence, not a gate.

## Artifacts

A run's whole story lives in one directory. The trace is
`<stem>.json` where `<stem>` is `<timestamp>_<run_id[:8]>`, and the score
sidecar is `<stem>.score.json`. Hook artifacts instantiate the same
sidecar pattern with the hook name as the discriminator:

```
<stem>.json                  # trace
<stem>.score.json            # score sidecar
<stem>.debrief.json          # debrief hook artifact
<stem>.<hook_name>.json      # the general pattern
<stem>.<hook_name>.<label>.json   # a hook that emits several per run
```

Scenario- and pack-scoped emissions (from `on_scenario_end` /
`on_pack_end`) have no single run to sit beside; they are written at the
runs-directory root. Scenario-scoped emissions include the scenario id:
`<sweep_timestamp>.<hook_name>.<scenario_id>[.<label>].pack.json`.
Pack-scoped emissions stay
`<sweep_timestamp>.<hook_name>[.<label>].pack.json`.

**The sidecar rule.** A `*.json` file whose stem contains a dot is a
sidecar, never a trace. Every walker of `runs/` — `wt report`,
`wt triage`, `wt rescore`, `load_runs()` — must apply this rule rather
than special-casing `.score.json`. (Today `wt rescore` excludes only
`.score.json`; that walker generalizes as part of this change, otherwise
a debrief artifact would be parsed as a trace.)

Never counted, never gating: hook artifacts do not participate in
scoring, `wt report` totals, JUnit output, or exit codes. They exist for
downstream analysis — clustering debriefs across a 0/5 is a `wt triage`
concern, and a later one.

## Failure containment

Hook failures are never run failures. The framework wraps every dispatch:

- An exception inside a hook is caught, logged, and appended to the
  trace's `worker_warnings` as `hook:<name>: <error>` — diagnostics
  travel with the run's story, and a hung or broken hook can never turn
  a PASS into an ERROR.
- `ctx.converse()` runs under a framework-owned deadline (default 30s,
  `WT_HOOK_CONVERSE_TIMEOUT_S` to override). On expiry the call is
  abandoned, any later reply for that call is discarded, the artifact
  records `timed_out: true` if any converse call timed out, and dispatch
  returns.
- **The orphan-turn caveat, stated honestly:** an abandoned converse is
  an in-flight request the framework cannot always cancel (an HTTP
  inject already sent will still be processed by the endpoint). Because
  the runner is synchronous, the next reset cannot *begin* until
  dispatch returns — but an endpoint may finish processing an abandoned
  turn after that reset, which is the same exposure class as any client
  timeout against a stateful endpoint. The reset canary exists to catch
  exactly this leakage; a bench that cannot tolerate it should set the
  converse deadline above its endpoint's worst-case latency, not below.

## The debrief reference hook

`debrief` ships built in, and exists for two reasons: it is the first
consumer with a real payoff, and it exercises the hardest contract in
the system (post-score, session-alive, converse, artifact emission). A
hook system shipped with zero consumers is an unproven abstraction.

Behavior: at `on_run_scored`, if any score layer failed by default
(`WT_DEBRIEF_ON=all` to include fully passing runs), send the agent one
turn carrying the run-level outcome verdict and the scorers' concrete
layer reasons (each layer's `LayerResult.detail`), and record the reply.

**Prompt discipline.** The prompt asks in this order:

1. *Environment first:* did any tool call return an error, unexpected
   shape, or missing capability? Quote it.
2. *Blockers:* was anything you needed absent from the environment?
3. *Only then:* what would you do differently?

The ordering is epistemics: self-reports are reliable when the model
quotes error strings it actually received (privileged observability of
the environment from inside the run) and confabulatory on steering
questions ("why didn't you ask a clarifying question?"). Harvest the
reliable class first, so a truncated or lazy reply still contains the
part worth having.

**Artifact schema** (`<stem>.debrief.json`), versioned so triage can
cluster across runs and schema evolution stays honest:

```json
{
  "schema_version": 2,
  "run_id": "...",
  "scenario_id": "...",
  "agent": "...",
  "model": "...",
  "verdict": "FAIL",
  "failed_layers": ["outcome"],
  "reasons": {"outcome": "...", "trajectory": "...", "constraint": "...",
               "integrity": "..."},
  "prompt": "...",
  "reply": "...",
  "tools_disabled": false,
  "timed_out": false,
  "duration_ms": 1234,
  "error": null
}
```

**Debriefs report; they never gate.** No pass/fail influence, no exit-
code influence, and nothing in the framework consumes a debrief to modify
souls, skills, or scenarios automatically. A self-report auto-editing its
own steering is the hacks-becoming-truth failure mode; the loop is
*propose, not apply* — a human (or at minimum a triage process) sits
between the model's self-diagnosis and anything that changes steering.
Debriefs are leads for the wiring → harness → steering → model triage
ladder, not verdicts.

## Rollout

Ships additive in **0.7.0**: no `--hook` flag means byte-identical
behavior to 0.6, nothing is deprecated, and the new surface is the SPI,
the CLI flag, the built-in `debrief`, and the abstract `StateProbeHook`.

`StateProbeHook` also ships in **0.7.0** as an additive base class for
consumer #2. The canary kwarg is retained and not deprecated — the doctor
seeded-nonce flow still needs `run_reset_canary(..., state_probe=...)`,
so there was no breaking change to defer. A state-probe hook establishes
the first run's post-reset state as the clean baseline and reports later
post-reset mismatches through the ordinary hook warning channel
(`trace.worker_warnings` entries like `hook:<name>: ...`) plus a bounded
violation artifact. `wt doctor` remains the hard gate. If those warnings
prove too quiet in practice, the candidate escalation is an evidence-
quality or taint state on the run; deliberately not built here.
