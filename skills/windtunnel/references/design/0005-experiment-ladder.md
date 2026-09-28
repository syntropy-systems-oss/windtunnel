<!-- GENERATED from docs/design/0005-experiment-ladder.md at ab2defdcf654 — do not edit; edit docs/design/0005-experiment-ladder.md. -->
---
description: "Design specification for the experiment ladder: derived run tiers, wall-clock budgets, declared questions, artifact fingerprints, the regression evidence gate, prefix replay probes, and tier-ordered runtime queueing."
---
# 0005: The experiment ladder

## Status

Accepted for 0.12.0.

## Problem

A coding agent iterating on an agent with Wind Tunnel tends to answer every
question with the most expensive experiment available. It sees one scenario
fail, changes an error message, and re-runs the whole pack for an hour to
learn whether that one change fixed that one scenario. Then it does it again.

Two different questions are being conflated:

- **Did this change resolve the issue?** One scenario, maybe one turn of it,
  answers this in minutes.
- **Does everything still work?** The full pack answers this, and is worth
  running once there is a reason to believe the fix works.

You don't build the full airframe to learn whether a wing profile stalls.
You test a small section, iterate quickly, scale up, and only put the whole
plane in the tunnel once the parts have earned it.

Writing this down in the agent docs does not work. Agents read guidance and
then do the expensive thing anyway, with a justification. An escape hatch
fails the same way: every run would take it, each with a reason. The ladder
is therefore enforced by `wt run` itself and has no bypass flag.

## Design

### Tiers are derived, not declared

A sweep's tier comes from what it runs, so it cannot be mislabelled:

| Tier | What selects it | Answers |
|---|---|---|
| `probe` | `wt run --from-trace T` — one scenario replayed on top of a recorded history prefix | "Does the step that broke behave now?" |
| `focused` | a selection that resolves to exactly one scenario | "Does this scenario pass on this artifact?" |
| `regression` | a selection of more than one scenario | "Does everything still work?" |

### Wall-clock budgets

Every sweep has a budget in seconds. Probe and focused sweeps are capped;
regression sweeps are uncapped unless `--budget` is given.

| Tier | Default cap |
|---|---|
| `probe` | 300 s |
| `focused` | 900 s |

A repository sets its own caps in `pyproject.toml`, because what counts as
short depends on the bench (a local 4B model and a hosted frontier model
differ by an order of magnitude):

```toml
[tool.windtunnel.ladder]
probe_budget_s = 180
focused_budget_s = 1200
```

`--budget SECONDS` may lower a sweep's budget but never raise it above its
tier cap. The clock starts when the runtime lock is acquired, so time spent
queued behind another sweep is not charged. The budget is checked before each
run starts: once it is spent, no further run or scenario job starts, the
sweep finishes with the runs it completed, and the ledger records
`budget_exhausted: true`. A run already in flight is never interrupted,
because a half-finished run is not evidence of anything.

A sweep cut short answered a smaller question than it asked, so it exits
non-zero whatever its completed runs scored, and its rows never count as
focused evidence. The fix for a sweep that keeps running out of budget is a
smaller experiment, not a larger budget.

### Declared question and expectation

Once a runs directory has any ledger history, every `wt run` into it must
declare what it is for:

```bash
wt run --scenario lookup_order --runs 3 \
  --question "does returning the schema error to the model stop the fabricated table?" \
  --expect pass
```

`--question` is free text. `--expect` is `pass` or `fail`. Both are written
into every ledger row of the sweep, and at the end `wt run` prints whether
the prediction held. The first sweep into an empty runs directory (a fresh
baseline, or a CI job on a clean checkout) needs neither.

The record makes the experiment's intent reviewable after the fact, and it
makes the agent's predictions scoreable: an agent whose `--expect` is wrong
half the time is guessing, not debugging.

### Artifact fingerprint

Evidence is only evidence about the artifact it ran against. Each sweep
computes a fingerprint from:

- `git_tree` — the git tree of the working directory's repository including
  uncommitted and untracked (non-ignored) changes, and excluding what Wind
  Tunnel itself writes: the runs directory, every `--out` file any `wt run`
  or `wt report` has written for that runs directory (recorded in
  `<runs>/.wt-outputs`), and compiled Python (`*.pyc`, `*.pyo`), which
  importing the agent under test produces. It is taken with a copy of the index and a scratch object
  directory, so the real index, working tree, and object store are never
  written to; Git LFS's clean filter is disabled for the snapshot so it does
  not write into `.git/lfs` either;
- `runtime` and `target` — the runtime name as given, and the backend it
  resolves to independent of spelling: the plugin's class (an entry-point
  name and a `module:Class` path to the same plugin agree) plus its lock key
  when it has one (for `http_inject`, the endpoint URL, with `localhost` and
  `127.0.0.1`, letter case, default ports, and a trailing slash normalized);
- `soul` and `agents` — content hashes of the `--soul` and `--agents` files;
- `plugin` — the runtime plugin's optional
  `artifact_fingerprint(runtime_name) -> str | None`, for drivers whose
  agent under test lives outside the working tree (an image digest, a
  deployed commit);
- `wt_version` — the installed Wind Tunnel version.

The fingerprint is a hash over those parts; the parts are recorded alongside
it, and the snapshot's per-file listing is kept in `<runs>/.fingerprints/`,
so when a focused pass has gone stale the refusal names what changed —
which parts, and which files. Editing any tracked or untracked file,
including the scenario or the ladder config, changes the fingerprint and
makes earlier evidence stale. A file the runtime or other tooling writes into
the tree (a log, a cache) does too; the refusal names it, and the remedy is
to gitignore it, since it is not part of the artifact.

Outside a git repository `git_tree` is recorded as absent and the fingerprint
covers the remaining parts. That is weaker, and `wt run` says so. If the
snapshot fails inside a repository (git times out, say), `wt run` says that
instead, rather than reporting it as "not a repository". Uncommitted changes
inside a submodule or a nested repository are not seen; drivers whose agent
lives there should implement `artifact_fingerprint()`.

The fingerprint is taken before the sweep queues for the runtime. A focused
sweep, whose rows are the only ones that count as evidence, takes it again
when it records its result; if the artifact changed while the sweep waited or
ran, the row is recorded with the fingerprint `changed-during-sweep` and is
evidence for nothing.

### The evidence gate: a full run is earned

You do not build the whole airframe to find out what flies. A regression is
the full plane, and `wt run` refuses one that smaller tests have not earned.

The gate reads the ledger in append order (timestamps have one-second
resolution and are not used to order rows), and only rows against the same
runtime `target`, so a smoke run on `in_memory` never gates a driver. A
"focused pass" below is a passing `focused` row on the current fingerprint
from a sweep that finished within its budget; "passing" uses the same rule
that sets `wt run`'s exit code, so a transport-only verdict is not a failure.

1. **Earning.** Once the target has any ladder history, a regression needs at
   least one focused pass written after the most recent regression. That is
   the proof, on this exact artifact, that the thing you changed works; the
   full run then answers the different question of whether anything else
   broke.
2. **Failures.** For each selected scenario (identified by pack and name)
   whose most recent regression row failed, a focused pass of that scenario
   written after that row.

If either is missing, `wt run` exits `2` before taking the runtime lock. For
missing failure evidence it names the scenarios and prints the focused command
for each. For an unearned run it points at `wt results` for "what is working
now" (answered from runs already on disk, in seconds) and at a focused run of
the scenario the change targets. Both carry the flags that decide what is
under test (`--runtime`, `--runs-dir`, `--pack-source`, `--pack`,
`--all-packs`, `--soul`, `--agents`), and both name what made an earlier
focused pass stale. The regression's ledger rows record the focused sweep that
earned them (`earned_by`).

A scenario that errored before producing an aggregate has no ledger row;
that is an execution failure rather than an agent verdict, and the ledger
(not the progress event stream) is the record the gate reads.

Consequences, all deliberate:

- The first sweep against a target, whatever its size, is free: that is the
  baseline, measuring the plane you already have. CI on a clean checkout has
  no ledger and is never gated.
- "Run everything to see what is working" is refused. The answer is already
  on disk (`wt results`), and the next useful experiment is a focused one.
- Re-running a regression on unchanged code is not earned. Flakiness is a
  focused question: run the flaky scenario with more `--runs`.
- Every regression must be earned afresh. A clean regression does not earn
  the next one; the change you make after it has to pass a focused run first.
- Probe passes do not count. A probe replays one step on a recorded history;
  it is how you find the fix, not how you certify it.
- A scenario that failed can be left out of the selection. That is an
  explicit decision not to test it, not a bypass: the sweep records it under
  `excluded_failing` (failures from the packs the sweep selects from) and
  `wt run` prints it. Because the failure rule works per scenario, leaving a
  failure out does not launder it; it stays gated for every later regression
  that includes it, and so does a failure a budget-truncated regression never
  reached.
- Any change after the focused pass, however small, invalidates it. The
  agent climbs the ladder again from the rung that is now stale.

### Probe: replay from a recorded point

```bash
wt run --from-trace runs/lookup_order/.../20260102T030405Z_abcd.json \
  --from-turn 3 --question "..." --expect pass
```

The probe resolves the trace's scenario (the same discovery and
`--pack-source` rules as any `wt run`), freezes the trace's turns before user
turn `--from-turn` (1-based; default: the last, scored user turn) as history,
and runs the remaining user turns live. The frozen turns are sent exactly as
the runner builds history for a live multi-turn run (user and assistant
content), are recorded at the start of the new trace, and are marked with a
`replay_prefix:` worker warning naming the source run and the number of
frozen turns.

Scoring uses the scenario's own gates over the live turns only. The frozen
turns are the original run's behavior, so their tool calls neither satisfy
`must_call`/`requires_tool_use` nor trip `forbidden_calls`, and policies and
post-hoc perturbations do not see them either. `wt rescore` applies the same
view, so live and offline scores agree. Choose a `--from-turn` after which
the behavior under question happens.

A probe needs a runtime whose handles consume the full message history.
Contract C (`http_inject`) and `terminus` transmit only the newest user turn.
The runner refuses the probe at the first send, the same way history-shaped
perturbations are refused rather than silently scored as if applied, and the
run is recorded as an execution error, not an agent verdict.

### Tier-ordered queueing

The runtime lock serves waiting sweeps by tier, then arrival: probes, then
focused, then regression. Each waiter holds a lock on its own ticket file in
the lock directory, so a crashed waiter's ticket is recognized as dead and
removed by the next waiter that looks. A new sweep never jumps ahead of a
live waiter, including with `--no-wait`. A sweep that already holds the
runtime is never preempted; a probe queued behind a running regression sweep
starts as soon as that sweep finishes, ahead of any other regression waiting.

Ordering has a cost the old lock did not: a waiter at the head of the queue
that is alive but stopped (suspended with SIGSTOP, say) holds up everyone
behind it even while the runtime is idle, because a live ticket is never
reaped.

## Ledger record

Every ledger row written by `wt run` gains two fields:

```json
{
  "sweep_id": "3f2a9c1b7e4d",
  "experiment": {
    "counts_as_failure": false,
    "tier": "focused",
    "question": "does returning the schema error stop the fabricated table?",
    "expect": "pass",
    "budget_s": 900,
    "budget_exhausted": false,
    "fingerprint": "sha256:…",
    "fingerprint_parts": {
      "git_tree": "4b825dc…", "runtime": "my_runtime", "target": "my_runtime",
      "soul": "sha256:…", "agents": null, "plugin": null, "wt_version": "0.12.0"
    },
    "source_trace": null,
    "from_turn": null,
    "evidence_for": [],
    "earned_by": null,
    "excluded_failing": []
  }
}
```

`counts_as_failure` is the sweep's own judgement of the row (the rule behind
the exit code). `evidence_for` on a regression row names the regression
sweeps whose failures were satisfied by focused evidence, and `earned_by` the
focused sweep that earned it. The changes are additive; readers ignore unknown
fields, so `windtunnel_ledger` stays at version 1.

## Out of scope

- **Preempting a running sweep.** Yielding the runtime between jobs would
  need every plugin to tolerate a rebuild mid-sweep.
- **Probes over Contract C.** An additive `seed_history` inject field,
  behind a capability route so an older endpoint cannot silently ignore it,
  would let production-path runtimes replay from a point.
- **Mid-turn replay.** The trace records one assistant turn per user turn;
  resuming between tool calls inside a turn needs a runtime seam that accepts
  a history ending in a tool result.
