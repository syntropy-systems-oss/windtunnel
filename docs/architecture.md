---
description: "Architecture overview of Wind Tunnel's API/SPI split, runner data path, behavior gates, experiment integrity, perturbations, and CLI surfaces."
---
# Wind Tunnel — Architecture

How Wind Tunnel is put together: the two-surface design, the data path of a
scored run, behavior gates, experiment integrity, perturbations, and the
dimension catalog.

---

## 1. Two surfaces: API and SPI

- **API** (`windtunnel/api/`) — what scenario authors import: `Scenario`,
  `Trace`, `Score`, `evaluators`, `perturbations`, `run_scenario()`.
  Backend-agnostic.
- **SPI** (`windtunnel/spi/`) — what runtime implementers fill in:
  `AgentRuntime`, `AgentHandle`, `MCPServer`, `MCPHandle` Protocols. Each
  agent platform implements the SPI.

**Hard invariant:** a scenario must NEVER import a platform-specific type. If
you can't write a scenario without importing `windtunnel.runtimes.*`, the SPI
has leaked — fix the contract, not the scenario. Enforced by
`tests/test_import_invariants.py`.

The payoff: a scenario is written once and runs unchanged against an
in-memory stub, a local docker stack, or your full production-shaped
platform. When a scenario passes in-memory but fails on your platform, the
difference *is* your platform — that's the signal the bench exists to
produce.

```
   scenario authors                     runtime implementers
        │                                       │
        ▼                                       ▼
  ┌───────────┐    run_scenario()      ┌─────────────────┐
  │  api/     │ ─────────────────────► │  spi/ Protocols  │
  │ Scenario  │                        │ AgentRuntime     │
  │ Trace     │ ◄───────────────────── │ AgentHandle      │
  │ Score     │      Trace, Turn       │ MCPServer/Handle │
  └───────────┘                        └────────┬────────┘
                                                │ implemented by
                                       ┌────────▼────────┐
                                       │ runtimes/<yours> │
                                       └─────────────────┘
```

## Internal implementation boundaries

The stable modules above are also compatibility facades. Their public import
paths stay fixed while focused private packages own the implementation:

| Stable surface | Private implementation | Responsibility |
|---|---|---|
| `windtunnel.api.runner` | `windtunnel.api._runner` | Message shaping, evidence capture, world preconditions, and hook dispatch. |
| `windtunnel.api.evaluators` | `windtunnel.api._matching` | Shared fact, number, last-turn, and canonical tool-name matching. |
| `windtunnel.cli` | `windtunnel._cli` | Runtime/scenario discovery, selection, hooks, sweep storage, and machine-readable output. |
| `windtunnel.report` | `windtunnel._report` | Run loading, report modeling/diffs, and text/JSON rendering. |

Private implementation modules may depend on API/SPI contracts, but never
import back from their facade. That one-way dependency keeps orchestration
testable without creating circular imports. Code outside Wind Tunnel should
continue importing the stable surface; underscore-prefixed packages are free
to evolve between releases.

SPI concepts have one canonical definition. In particular, `MCPSpec` is
defined in `spi.agent_runtime` and re-exported elsewhere, and a
`RuntimePlugin` structurally requires only `build()`. Optional lifecycle hooks
such as `pre_run()` are discovered by capability rather than made part of the
minimum plugin contract.

## 2. Anatomy of a scored run

`run_scenario(scenario, runtime, mcps)` orchestrates one batch:

1. **Start MCP servers** — the runner starts every server in the `mcps`
   argument and collects handles. Two ways servers get there: pass them
   yourself when calling `run_scenario()` directly, or let the CLI build one
   from the scenario's pack (`ScenarioPack.mcp_factory`, matched by the
   `dim:<name>` tag) and pass it into `mcps` for you. Either way the runner
   owns start/stop — `mcps=[]` means the agent genuinely has no tools, and
   any `requires_tool_use` scenario will (correctly) fail.
2. **Provision** — `runtime.provision(config, mcps)` returns an
   `AgentHandle`: a live agent wired to those tools. Expensive, once per
   batch.
3. **Per run** (N runs per scenario):
   - `handle.reset_state()` — wipe cross-run state (sessions, memory,
     tool-call logs). Cheap, every run. State contamination between runs is
     the classic source of false passes, so a failed reset is fatal.
   - **Pre-send perturbations** shape the outgoing messages (corrupted
     history the live model must handle).
   - `handle.send(messages, session_id)` — drive the turn(s). Multi-turn
     scenarios thread the same `session_id` across turns.
   - Record a `Trace`: every `Turn` with role, content, tool calls/results
     (preserved in the wire shape they arrived in), latency, and — when the
     runtime can supply it — the exact rendered prompt with its hash.
   - **Score** agent behavior and verify experiment integrity (below).
4. **Aggregate** N scores into a verdict; `handle.teardown()` at batch end.

The `Trace` is the unit of record: JSON-serializable, diff-able, replayable.
Reports, comparisons, and triage all consume saved traces — you never need to
re-run a model to re-analyze a run (re-scoring saved traces is supported and
cheap).

Persisted boundaries are explicitly versioned: native traces use
`windtunnel_trace`, score sidecars use `windtunnel_score`, and ledger rows use
`windtunnel_ledger`. Readers migrate unversioned 0.8 artifacts in memory and
reject unknown future versions. Contract A interchange and Contract B universe
files tolerate additive fields within their supported version but likewise
reject unknown version numbers.

## 3. Behavior scores, gates, and experiment integrity

A `Scenario` declares expectations across three independent agent-behavior
layers. Each run also produces an integrity result about the test setup.

| Layer | Checks | Evaluator |
|---|---|---|
| **outcome** | the user-visible answer is right | `evaluate_outcome` |
| **trajectory** | right tools called, right order, none forbidden | `evaluate_trajectory` |
| **constraint** | named policy predicates over the trace hold | `evaluate_constraint` |
| **integrity** | declared perturbations were actually applied | `evaluate_integrity` |

The scenario's gate is inferred as `outcome` plus every trajectory or
constraint layer for which the author declared an expectation. A required
tool call or custom trajectory check therefore fails the scenario when it
fails; so does a declared policy. Set `gate_layers` explicitly when a layer
is intentionally diagnostic, for example `gate_layers=["outcome"]` while
exploring a new trajectory assertion.

Integrity is outside that configurable gate. A missing perturbation marker
means the intended experiment did not happen, so the aggregate is `INVALID`
rather than `PASS` or `FAIL`. This prevents both false confidence and false
blame.

`evaluate_outcome` encodes several hard-won rules:

1. **Last-turn semantics** — scores the *actual* last assistant turn, even if
   its content is `""`. An agent that stops after a tool call without
   answering has answered with nothing: fail. Never backfill from
   intermediate turns.
2. **`requires_tool_use` gate** — if set and the trace has zero tool calls,
   fail even if the right facts appear. This closes the "guessed correctly
   from training data" false positive.
3. **AND-of-OR `target_facts`** — `[["A","a"],["B"]]` means *(A or a) AND
   (B)*. Case-insensitive substring match.
4. **Typed `target_numbers`** — word-boundary regex (`\b3\b` doesn't match
   `B003`), optional unit-proximity check.
5. **Negation-aware `forbidden_facts`** — "the SKU is B003" fails, "there is
   no SKU B003" doesn't.

**Aggregate verdict:** `PASS` iff **all N** runs satisfy the declared gate;
otherwise `FAIL` — unless the scenario sets `variance_allowed=True`
(for sampler-sensitivity work), which yields `PASS_WITH_VARIANCE` with
`pass_rate ± stddev`. No runs, or any integrity failure, yields `INVALID`.

Every scenario carries a `FailureCost` (severity / customer_visible /
reversible / side_effect_performed). Wind Tunnel converts it to a stable risk
weight and reports `failure_risk = risk_weight × (1 - pass_rate)`. Comparisons
rank regressions by this operational risk while every gated regression still
fails.

**Robustness** is not the integrity flag. It is gate performance on scenarios
that declare perturbations, after integrity has established that those
conditions were actually present. A suite with no perturbation scenarios has
no robustness rate (`N/A`), not a synthetic 100%.

## 4. Trajectory truth: the MCP call log

Agents misreport their own tool use — they narrate calls they never made and
omit ones they did. Wind Tunnel doesn't trust the transcript: the
`MCPHandle.call_log()` primitive records every call that actually reached
the tool server (name, args, result, timestamp), the runner drains it onto
the trace as `Trace.mcp_calls`, and `evaluate_trajectory` scores against
*that* whenever it is non-empty — the transcript's self-reported
`tool_calls` are used only as a fallback when no logging mock was in play
(e.g. the in-memory runtime). The `LayerResult` detail names which evidence
was used (`server-witnessed` vs `transcript`). The log is reset between runs
and readable even when the mock runs as a subprocess.

A side benefit: perturbation-injected history (fake prior tool calls shaped
into the messages) never reaches the tool server, so the server-witnessed
path is naturally free of calls the live model never made — no filtering
needed.

## 5. Perturbations (two families)

A perturbation adversarially stresses one run. Two families, by *where* the
corruption lands:

- **Pre-send / history-shaping** (subclasses of `PreSendPerturbation`,
  abstract `shape_messages()`) —
  inject corrupted prior turns into the messages the **live model actually
  runs on**: a blank assistant turn, a literal tool-call rendered as text, a
  wrong prior tool call, a truncated pagination, a stale memory line. The
  model either resists the poison or succumbs — the eval is real either way.
- **Environment-shaping** (mock failure modes via
  `FailureInjectableMCPHandle.configure_failure_mode()`) — the tool server misbehaves live:
  malformed JSON, timeout, unexpectedly empty result.

Every perturbation declares a `marker`; the runner ensures it lands in the
trace, and `evaluate_integrity` verifies the contract was honoured. A
perturbation that silently failed to apply cannot produce a false pass.
Pre-send perturbations are injected before send and skip the post-hoc
`apply()` — no double application.

## 6. The dimension catalog

Each dimension is a package under `windtunnel/scenarios/dim_*` with its own
`scenarios.py`, a mock MCP server, and a synthetic database, exposed to the
CLI as a `ScenarioPack` (the dim's `PACK`). Group by the `dim:<name>` tag.
Third-party dimensions ship as packs under the `windtunnel.scenario_packs`
entry-point group — the scenario-side twin of `windtunnel.runtimes`.

| Dim | Reliability property tested |
|---|---|
| `tool_affordance` | Builds the right mental model of each tool's contract (strict-id vs lenient-name, in/out of scope, chained lookups). |
| `clarify_vs_guess` | Under genuine ambiguity, clarify or list candidates rather than silently guess. |
| `memory_conflict` | Trust the live tool over stale/seeded memory; surface conflicts. |
| `multi_turn_drift` | Preserve context across a multi-turn session; scored on the final turn. |
| `policy_pressure` | Hold a policy under explicit user pressure to skip it. |
| `recovery` | Recover from a bad intermediate state injected into history. |
| `sampler_sensitivity` | Stability of correct behavior across the temp/top_p matrix (`variance_allowed=True`). |
| `side_effect_safety` | Respect per-effect-class autonomy ceilings (read < draft < internal update < external send < destructive). |
| `silent_failure` | Detect the *environment* misbehaving and report instead of fabricating. |
| `icl_poisoning` | Policy survives a corrupted prior turn; don't copy the broken shape. |

Some dims add a per-dim verdict overlay (e.g. clarify_vs_guess buckets runs
into acted / clarified / wrongly_guessed / refused_unnecessarily).

## 7. CLI surfaces

The `wt` CLI is the packaged workflow surface:

- `wt run` executes scenarios, writes traces and score sidecars, appends the
  ledger, and can emit JUnit/JSON for CI.
- `wt report` renders saved runs as HTML, Markdown, or JSON.
- `wt compare` compares labeled run sets (model swap, prompt change,
  temperature pin).
- `wt replay` replays a saved trace's last user turn against a runtime.
- `wt doctor` runs the reset-isolation canary against a live runtime.
- `wt import` generates a scenario skeleton from a Contract A trace envelope.
- `wt validate` validates and lints Contract A envelopes.
- `wt triage` classifies saved failures against the
  [failure taxonomy](failure-taxonomy.md). The shipped classifier is
  rule-based. An LLM-backed classifier can implement the same protocol; the
  repository's `LLMJudgeClassifier` remains an unregistered implementation
  sketch rather than a selectable CLI feature.

See [CLI reference](cli-reference.md) for options and exit codes.

## 8. Fidelity: the design stance

Wind Tunnel's founding bet is that **agent reliability bugs live in the
seams** — chat templates, tool-schema sanitizers, message-history plumbing,
session state — not in the model alone. So a runtime should reproduce your
production path as faithfully as possible: same images, same proxies, same
tool-prefixing and grants, with only the minimum hermetic divergences
(fake identity provider, isolated state, canned upstream tools). The SPI is
deliberately small so that wiring the *real* stack in is easier than building
a lookalike.

See [writing-a-runtime.md](writing-a-runtime.md) for the contract.
