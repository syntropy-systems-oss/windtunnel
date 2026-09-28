<!-- GENERATED from docs/design/0005-experiment-ladder.md at ffd652f08197 — do not edit; edit docs/design/0005-experiment-ladder.md. -->
---
description: "Design specification for the experiment ladder: derived run tiers, wall-clock budgets, declared questions, artifact fingerprints, the regression evidence gate, prefix replay probes, tier-ordered runtime queueing, per-tier model policy, recorded experiment cost and value, cache-aware pricing, and the prompt-cache-miss check."
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
- `model` — the runtime's model label (see
  [Model tag and per-tier model policy](#model-tag-and-per-tier-model-policy));
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

### Model tag and per-tier model policy

A pass on a small model says little about the model the agent ships with.
The model is therefore part of what is under test.

A runtime plugin reports the model it will answer with through an optional
hook, `model_label(runtime_name) -> str | None` (the built-in `terminus`
runtime reports `WT_TERMINUS_MODEL`). The label is an opaque string, compared
exactly. `wt run` puts it on the agent config, so every trace records it, and
into the fingerprint's `model` part, so a focused pass on one model never
earns or satisfies a regression on another; the refusal names `model` as
what changed. When the hook is absent the label is null, and a trace records
the model the runtime's responses name (an OpenAI-style `model` field), or
`unknown`. There is no `--model` flag: a label the caller can set is a label
the caller can launder a pass through.

A repository may restrict which models each tier runs on:

```toml
[tool.windtunnel.ladder.models]
probe = ["small-model", "target-model"]
focused = ["target-model"]
regression = ["target-model"]
```

A tier that is not listed may use any model. A sweep whose label is not
listed for its tier, or whose runtime reports no label while its tier is
listed, exits `2` before it takes the runtime lock. An invalid table (an
unknown tier, an empty or non-string list) is refused, not ignored.

### Cost and value

Every sweep records what it cost and, later, what it was worth, so a team
can see which tiers pay for themselves.

**Cost.** Each ledger row's `experiment.cost` holds the scenario's wall
seconds (its job, provisioning included) and the model tokens its runs
consumed: `input_tokens` (the TOTAL prompt tokens, including any served from
a prompt cache), `cached_tokens`, and `output_tokens`. Tokens come from the
`usage` object on the runtime's responses (`input_tokens`/`output_tokens`, or
`prompt_tokens`/`completion_tokens`), summed per run into the trace's
`usage`. A run with any send that reported no usage records `usage: null`,
and any null makes the row's token counts null: a partial sum would
understate the cost, so tokens are never guessed.

When a runtime reports usage per model call (not just a per-run total), the
trace also carries `model_calls` — see
[Per-call usage and the prompt-cache-miss check](#per-call-usage-and-the-prompt-cache-miss-check)
— and `input_tokens`/`cached_tokens`/`output_tokens` are the sums over every
call instead: a call missing its prompt or completion count makes the whole
run's tokens unknown, and a call missing only its cache split makes
`cached_tokens` unknown on its own (the totals still stand).

**The plan.** `--if-pass TEXT` and `--if-fail TEXT` optionally say, up front,
what the result will change. They are recorded, and `wt run` repeats the one
that applies when the sweep ends.

**The outcome.** When a sweep ends, `wt run` appends one record to
`<runs>/experiments.ndjsonl` with the sweep's tier, model, question,
expectation, plan, outcome (`pass` when it exits `0`), whether the prediction
held, and its cost: wall seconds from acquiring the runtime, and the sum of
its rows' tokens (null if any row, or any scenario that errored before
producing one, did not report them).

**The review.** Afterwards, `wt review SWEEP_ID --decision "what it changed"`
or `wt review SWEEP_ID --no-change` records whether the sweep changed a
decision; a later review of the same sweep replaces an earlier one.

`wt results --ladder` (and `--json`) summarizes the file per tier: sweeps,
wall seconds, tokens (with how many sweeps reported them), predictions held,
and how many reviewed sweeps changed a decision. A tier whose sweeps are
expensive and rarely change anything is one to climb past faster; one whose
predictions keep missing is where the agent is guessing.

### Pricing: turning cost into dollars

Wall seconds and tokens say what a sweep spent; a team also wants to know
what it cost. `[tool.windtunnel.ladder.pricing]` is optional and holds no
built-in prices — rates are config, an operator's own numbers, never code.
Three token price classes, per model label: an uncached-input rate, an
optional cache-read rate (cheaper, since a cache hit skips most of the
prefill), and an output rate.

```toml
[tool.windtunnel.ladder.pricing]
time_per_hour = 60.0             # $ per wall-clock hour (example rate)

[tool.windtunnel.ladder.pricing.models]
# $ per million tokens — example rates, not real prices.
"target-model" = { input_per_m = 1.00, cache_read_per_m = 0.10, output_per_m = 4.00 }
default = { input_per_m = 1.00, cache_read_per_m = 0.10, output_per_m = 4.00 }
```

Model labels are opaque strings, matched exactly against the `model` the
ledger already records (see
[Model tag and per-tier model policy](#model-tag-and-per-tier-model-policy)).
A label with no entry of its own falls back to `models.default` when the
table gives one. `cache_read_per_m` is optional and falls back to that same
label's `input_per_m` — a runtime that never reports a cache split still
prices correctly, every input token at the one rate. `time_per_hour` and
every rate must be a non-negative number; an invalid table is refused (exit
`2`), not silently ignored.

When pricing is configured, every ledger row's `experiment.cost_usd` and
every sweep record's `cost_usd` in `experiments.ndjsonl` hold:

```json
{
  "uncached_input": 0.0090, "cache_read": 0.0004, "output": 0.0400,
  "time": 0.0010, "total": 0.0504,
  "tokens_known": true, "cache_split_known": true
}
```

`uncached_input` prices `input_tokens - cached_tokens` at `input_per_m`;
`cache_read` prices `cached_tokens` at `cache_read_per_m`. Every component is
null and `tokens_known` is false when the row's token usage was not
reported, or its model has no price and no `models.default` — `total` then
covers `time` alone, same as before three price classes existed. When tokens
are known but the run's `cached_tokens` is null (the runtime never reported a
per-call cache split), every input token prices at `input_per_m`,
`cache_split_known` is false, and `cache_read` stays null rather than
guessing the split. With no `[tool.windtunnel.ladder.pricing]` table,
`cost_usd` is absent entirely (not null), so a runs directory recorded before
pricing was configured, or by a repository that never configures it, stays
exactly as before.

`wt run` prints a cost block when each sweep ends — the question, tokens in
(cached)/out (or "unknown"), wall time, and, when pricing is configured, the
`$ uncached + cache read + output + time = total` breakdown, flagging "cache
split unknown" when `cached_tokens` was never reported and "tokens unknown —
time cost only" when token usage was not reported at all. `wt results
--ladder` adds the same $ breakdown per tier (how many of its sweeps were
priced, and how many of those had unknown tokens) and a cumulative total
across tiers, shown only once at least one sweep in the file was priced.

### Per-call usage and the prompt-cache-miss check

A runtime that reports usage per model call, not just a per-run total, lets
`wt` price a cache split and check that a multi-turn conversation is actually
using its prompt cache.

**The shape a runtime emits.** When an `AgentHandle.send()` response carries a
`usage` object, the runner normalizes it and appends one entry to
`Trace.model_calls`, in turn order:

```json
{"conversation": "3f2a9c1b-…", "prompt_tokens": 1500, "cached_tokens": 1300, "completion_tokens": 30}
```

`conversation` is that run's `session_id` — every call in one trace shares
it; pooling several runs' (or several scenarios') `model_calls` into one list
(as the ladder does for cost and the cache check) uses it to tell one run's
calls from another's, so "the first call in a conversation" and "call k" stay
relative to one run's own turn order however many conversations are pooled.
`prompt_tokens` is the same TOTAL-including-cached figure as `usage.input_tokens`.
A field is `null`, never guessed, when that one call didn't report it — unlike
`usage`, one call's gap does not blank the run's other totals. `model_calls`
is `null` (not an empty list) when the runtime never reported usage at all.

One `send()` can itself cover several model calls (an agent loop's tool
call, tool result, call again, all inside one turn) — reporting only that
turn's aggregate would dilute a miss on one of those calls into the turn's
overall ratio. A runtime instead nests a `"calls"` list of per-inference-call
usage dicts under `usage`, and each entry becomes its own `model_calls`
entry (in order) instead of the one aggregated entry; an entry may carry its
own `"conversation"` string, overriding the run's `session_id` default, for
a call that starts its own side conversation. See
[Writing a runtime](../writing-a-runtime.md) for the wire spellings accepted
when normalizing a call's raw usage dict, and the exact `usage.calls` shape.

**The check.** `[tool.windtunnel.cache]` is optional and off by default:

```toml
[tool.windtunnel.cache]
fail_on_miss = true       # a miss (or an unknown split) fails the sweep; default false
min_cached_ratio = 0.5    # cached_tokens / prompt_tokens floor; default 0.5
```

For every model call after the first in a conversation — the first turn has
nothing to have cached yet, so it is never checked — `cached_tokens /
prompt_tokens` must be at least `min_cached_ratio`, or it is a miss:
`prompt cache miss at call k: cached M of P prompt tokens`. The check never
derives an expected prefix or compares prompts between calls; it only reads
the reported numbers and this ratio. When a checked call's cache split was
not reported, the result is `unknown` — never `pass` — and said so in the
output. With `fail_on_miss = true`, a `fail` or `unknown` result makes the
row's `counts_as_failure` true regardless of the scenario's own verdict.

The result — `{"result": "pass" | "fail" | "unknown", "reason": ..., "calls":
[...]}` — is recorded as `experiment.cache_check` on the ledger row (and
`cache_check` on the sweep's `experiments.ndjsonl` record) whenever
`[tool.windtunnel.cache]` is configured, and `wt run`'s end-of-sweep cost
block prints the verdict plus one compact `call k: cached M/P` line per
checked call:

```text
wt run: cost — 'does the multi-turn prompt still hit the cache?'
wt run:   tokens: 3700 in (1400 cached) / 120 out
wt run:   wall: 4.2s
wt run:   $ uncached 0.0023 + cache read 0.0001 + output 0.0005 + time 0.0700 = total 0.0729
wt run:   cache check: fail — prompt cache miss at call 2: cached 100 of 1200 prompt tokens
wt run:   call 2: cached 100/1200
wt run:   call 3: cached 1300/1500
```

When the runtime never reports per-call usage (or a checked call's cache
split goes unreported), the block instead reads e.g. `cache check: unknown —
cache split not reported for one or more calls`, with `call k: cache split
unknown` in place of the counts — never a silent pass.

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
    "if_pass": "run the pack",
    "if_fail": "probe turn 2",
    "cost": {"wall_s": 41.2, "input_tokens": 18230, "cached_tokens": 15000, "output_tokens": 1504},
    "cost_usd": {
      "uncached_input": 0.0032, "cache_read": 0.0015, "output": 0.0060, "time": 0.6867,
      "total": 0.6974, "tokens_known": true, "cache_split_known": true
    },
    "cache_check": {"result": "pass", "reason": null, "calls": ["call 2: cached 15000/18230"]},
    "budget_s": 900,
    "budget_exhausted": false,
    "fingerprint": "sha256:…",
    "fingerprint_parts": {
      "git_tree": "4b825dc…", "runtime": "my_runtime", "target": "my_runtime",
      "model": "target-model", "soul": "sha256:…", "agents": null, "plugin": null, "wt_version": "0.12.0"
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
the exit code) — a prompt-cache miss with `fail_on_miss = true` can make it
true even when the scenario itself passed. `evidence_for` on a regression row
names the regression sweeps whose failures were satisfied by focused
evidence, and `earned_by` the focused sweep that earned it. Traces gain
`usage` and, when a runtime reports per-call usage, `model_calls`. `cost_usd`
is present only when `[tool.windtunnel.ladder.pricing]` is configured, and
`cache_check` only when `[tool.windtunnel.cache]` is. The changes are
additive; readers ignore unknown fields, so `windtunnel_ledger` stays at
version 1.

## Out of scope

- **Preempting a running sweep.** Yielding the runtime between jobs would
  need every plugin to tolerate a rebuild mid-sweep.
- **Probes over Contract C.** An additive `seed_history` inject field,
  behind a capability route so an older endpoint cannot silently ignore it,
  would let production-path runtimes replay from a point.
- **Mid-turn replay.** The trace records one assistant turn per user turn;
  resuming between tool calls inside a turn needs a runtime seam that accepts
  a history ending in a tool result.
