<!-- GENERATED from docs/writing-a-scenario.md at c9651767941b — do not edit; edit docs/writing-a-scenario.md. -->
---
description: "Reference for authoring backend-agnostic Scenario objects, scoring fields, perturbations, dimensions, and scenario packs."
---
# Writing a Scenario

A `Scenario` is the single authoring surface for everything a bench run is graded
on. It is backend-agnostic — **never import a runtime or mock type from a
scenario** (enforced by `tests/test_import_invariants.py`).

This is the authoring reference. For how scoring works conceptually, see
[`architecture.md`](architecture.md#3-behavior-scores-gates-and-experiment-integrity).

---

## The minimal scenario

```python
from windtunnel.api.scenario import Scenario

my_scenario = Scenario(
    name="lookup_before_action",
    prompt="How many open orders does Lowell Spinners have?",
    target_facts=[["75 orders", "75"]],    # required
    requires_tool_use=True,
    must_call=["client_lookup"],
    tags=["dim:tool_affordance"],
)
```

`name` plus either `prompt` or `user_turns` is required. `target_facts` defaults
to an empty list so scenarios using `outcome_fn` do not need placeholder facts.

---

## World preconditions: verify before spending a turn

A scenario can declare the world shape it needs before the runner spends an
agent turn. This is the same philosophy as the reset canary: never spend a turn
against a world you have not verified. The runner checks preconditions after MCP
servers are started and the runtime is provisioned, but before `reset_state()`
or `send()` for the first run. All checks run, and every failure is reported
together as `WorldMismatchError`.

```python
from windtunnel.api import Check, FileExists, Scenario, StateProbeAvailable

def fixture_seeded(ctx):
    rows = (ctx.state_probe.capture() if ctx.state_probe else {}).get("db", {}).get("rows", [])
    return None if rows else "seed rows missing"

Scenario(
    name="lookup_seeded_customer",
    prompt="Find the seeded customer.",
    target_facts=[["Customer A"]],
    requires_tools=["client_lookup"],  # sugar for ToolAvailable("client_lookup")
    preconditions=[
        FileExists("/tmp/windtunnel-fixture.db"),
        StateProbeAvailable(),
        Check(fixture_seeded, "fixture contains seed rows"),
    ],
)
```

Built-ins:

- `ToolAvailable(name)` verifies that one of the scenario's MCP handles reports
  the named tool in `served_tools()`. `requires_tools=["client_lookup"]` is
  shorthand for the same check.
- `FileExists(path)` checks a filesystem path. Absolute paths are checked on
  the bench host. Relative paths resolve against a runtime workspace template
  when the driver exposes one, then the live workspace, then the current working
  directory.
- `StateProbeAvailable()` requires the scenario's owning pack or library caller
  to wire a `StateProbe`. Scenarios whose policies or `outcome_fn` read
  `trace.observations` should declare it so missing observation plumbing is a
  `WORLD` error rather than a plausible-looking agent failure.
- `Check(fn, description)` wraps a custom function over `PreconditionContext`
  (`mcp_handles`, optional `state_probe`, and `agent_config`). Return `None` or
  `True` to pass, a string to fail with that detail, or `False` to fail with the
  wrapper description.

When `WorldMismatchError` reaches `wt run`, the CLI prints a non-traceback
message naming the scenario and failed preconditions, then exits `1`. The
scenario selection was valid, but the bench world was wrong, so this is treated
like a failed run rather than a usage error (`2`).

---

## The `Scenario` schema (`api/scenario.py`)

Fields are grouped by the scoring layer each feeds.

### Identity
| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Scenario identity (used as `--scenario` selector + trace `scenario_id`). |
| `prompt` | `str` | The user prompt that drives the run. |

### World preconditions (fail-fast, not scoring)
| Field | Type | Default | Meaning |
|---|---|---|---|
| `preconditions` | `list[Precondition]` | `[]` | Checks over the live MCP handles, optional `StateProbe`, runtime workspace paths, and `AgentConfig`; fail before reset/send. |
| `requires_files` | `list[str]` | `[]` | Sugar for `FileExists(path)` preconditions, useful for workspace-template fixture inputs. |
| `requires_tools` | `list[str]` | `[]` | Sugar for `ToolAvailable(<name>)` preconditions. |

### Outcome layer (the gate)
| Field | Type | Default | Meaning |
|---|---|---|---|
| `target_facts` | `list[list[str]]` | required | **AND-of-OR** fact groups (below). |
| `target_numbers` | `list[NumberFact]` | `[]` | Typed numeric facts; **AND** — all must match. |
| `requires_tool_use` | `bool` | `False` | If `True`, outcome **fails** when the trace has zero tool calls, even if the facts appear. Closes the "guessed from training" false positive. |
| `forbidden_facts` | `list[str]` | `[]` | Strings that must **not** appear (negation-aware) in the last turn. |
| `outcome_fn` | `Callable[[Trace], LayerResult] \| None` | `None` | **Custom outcome evaluator** (below). When set, it fully owns this layer. |

**`outcome_fn` — grade an artifact, not the prose.** The fields above all match
the model's last assistant turn. When success means *the thing the agent built is
correct* — a produced file, a database row, external API state — set `outcome_fn`
to score from any `Trace` evidence instead. It receives the `Trace` and returns a
`LayerResult(passed, detail)`; when set, `target_facts`/`target_numbers`/
`forbidden_facts` are **not** consulted. The structural gates still run first (a
missing assistant turn and `requires_tool_use` fail before it), and a raised
exception is scored as a failure. The canonical pairing is a **`StateProbe`** that
froze the artifact into `trace.observations` (see *Verifying external state*
below), with `outcome_fn` reading that snapshot:

```python
def _graded(trace: Trace) -> LayerResult:
    art = (trace.observations or {}).get("report") or {}
    if not art.get("found"):
        return LayerResult(False, "no report artifact produced")
    return LayerResult(art["rows"] == EXPECTED_ROWS, "report rows match expected")

Scenario(
    name="...",
    prompt="...",
    target_facts=[],
    outcome_fn=_graded,
    requires_tool_use=True,
    preconditions=[StateProbeAvailable()],
)
```

Like `policies`/`trajectory_checks`, `outcome_fn` is a callable, so it isn't
serialized — it's reconstructed when the scenario's pack is re-imported (offline
re-scoring needs the pack importable).

**The scorer library (`windtunnel.api.scorers`).** The common `outcome_fn`
shapes are packaged, all pure stdlib, all returning `LayerResult`:

```python
from windtunnel.api import all_of, llm_judge, observation, substantiated_by_tools

Scenario(
    ...,
    outcome_fn=all_of(
        # artifact grading: walk trace.observations with a dotted/indexed path
        observation("github", "prs[0].base", lambda v: v == "main", "pr targets main"),
        # provenance: answer claims must appear in server-witnessed tool results
        substantiated_by_tools(),
        # rubric judge — BYO model via the same GenerateFn contract replay uses
        llm_judge("Did the answer resolve the billing question?", my_generate_fn),
    ),
)
```

- `all_of(*fns)` / `any_of(*fns)` — combinators; failing branches join their
  diagnostics into one `detail`, and a scorer that *raises* is converted to a
  failure naming the scorer (composition stays fail-closed).
- `observation(source, path, predicate, label)` — reads
  `trace.observations[source]` and walks `"prs[0].base"`-style paths; missing
  source/path is a diagnostic failure, never a crash.
- `llm_judge(rubric, generate_fn)` — core assembles the prompt (rubric +
  final answer + trace evidence) and strictly parses `PASS`/`FAIL`; anything
  else fails the layer with the raw response in `detail`. No vendor client
  ships — `generate_fn` is yours.
- `substantiated_by_tools(facts=None)` — the anti-fabrication check: claims
  in the final answer must appear in some server-witnessed tool result
  (`trace.mcp_calls`, falling back to transcript tool results, with the
  evidence source named in `detail`). Pass `target_facts`-style groups and
  `NumberFact`s, or pass nothing to require every integer in the answer to
  come from a tool — numbers from nowhere fail.
- `no_divergence()` — not an outcome scorer but a constraint-layer `Policy`:
  it fails when the run left the recording of a
  [universe fixture](recording-a-universe.md).

**Fast scorer iteration with `wt rescore`.** A saved trace contains the
transcript, server-witnessed MCP calls, and `trace.observations`, so most scorer
changes do not need another live agent run. The authoring loop is: "you find the
equilibrium scorer against a fixed trace corpus, then spend GPU only to grow the
corpus." Use:

```bash
wt rescore --runs runs/
wt rescore --trace runs/.../20260102T030405000000Z_abcd1234.json --write
```

`wt rescore` resolves each trace's `scenario_id` against the currently
discovered scenario packs, then re-runs outcome, trajectory, constraint, and
integrity from the saved trace plus current scenario definitions. Integrity is
derivable from the trace's perturbation markers, so it is recomputed too. The
command is read-only by default; `--write` updates the
`.score.json` sidecar with an `origin.kind = "rescore"` marker. Trace files are
never modified. Exit codes mirror `wt run`: `0` when all newly-scored gates
pass, `1` when any gate fails or any run is invalid, and `2` for usage or
configuration errors such as missing traces or unresolved scenario definitions.

**AND-of-OR `target_facts`:** a list of groups; each inner group is satisfied if
**any** member appears (OR), and **every** outer group must be satisfied (AND).
So `[["A","a"],["B"]]` = *(A or a) AND (B)*. Matching is **case-insensitive
substring** on the last assistant turn — you do **not** need to enumerate casing
variants (some older scenarios still do; that's a now-redundant workaround).

> Design group 1 to be a real *discriminator*. If you're testing "did the model
> flag a surprise," don't put bare result words like `"no orders"`/`"empty"` in the
> group — a confidently-wrong answer hits those too. Use anomaly/uncertainty/
> investigate markers instead. (Lesson from the silent-failure redesign.)

**`NumberFact`** — `NumberFact(value=75, unit="orders")`. Matched with `\b75\b`
word-boundary regex (so `3` ≠ `B003CCC`); `unit` is advisory (tightens via a
30-char IGNORECASE proximity check, but the number alone passes).

### Trajectory layer
| Field | Type | Default | Meaning |
|---|---|---|---|
| `must_call` | `list[str]` | `[]` | Tools that must all appear. Use the CANONICAL bare tool name (e.g. `client_lookup`) — the evaluator matches platform-decorated variants (`mcp_acme_ops_client_lookup`, `ops.client_lookup`) by suffix-at-word-boundary. |
| `forbidden_calls` | `list[str]` | `[]` | Tools that must never appear (e.g. forbid `invoice_send` to test "clarify, don't act"). |
| `order_matters` | `bool` | `False` | If `True`, `must_call` must appear as an in-order subsequence. |
| `trajectory_checks` | `list[TrajectoryCheck]` | `[]` | Custom verifiers over the observed call path; ANDed with the sugar fields above (see below). |

> `must_call=['clarify']` will fail trajectory ~100% (models clarify in prose, not
> via a literal `clarify` tool). Because a declared trajectory expectation joins
> the default gate, prefer `forbidden_calls` to encode "should have clarified."

**Custom `TrajectoryCheck`** — the trajectory layer's counterpart to `Policy`
(constraint) and `Perturbation` (integrity): a verifier over the path the agent
actually took, for expectations the sugar fields can't express. Implement
`check(calls) -> (passed, detail)`; `calls` is the chronologically-ordered list
of observed tool names — server-witnessed when a logging mock MCP is in play,
else the transcript's claims (same evidence rule as `must_call`). Names may be
platform-decorated, so compare with `tool_name_matches` (exported from
`windtunnel.api`). The layer passes iff the sugar-field checks AND every custom
check pass; failure details are joined. A check that *raises* is recorded as a
failure (like a `Policy` predicate) — it never crashes the evaluator.

```python
from windtunnel.api import TrajectoryCheck, tool_name_matches

class MaxLookups(TrajectoryCheck):
    """Fail if the agent burns more than `budget` calls on client_lookup."""
    def __init__(self, budget: int) -> None:
        self.budget = budget

    def check(self, calls: list[str]) -> tuple[bool, str]:
        n = sum(1 for c in calls if tool_name_matches("client_lookup", c))
        if n > self.budget:
            return False, f"called client_lookup {n}x, budget {self.budget}"
        return True, "lookup budget respected"

Scenario(..., must_call=["client_lookup"], trajectory_checks=[MaxLookups(2)])
```

### Constraint layer
| Field | Type | Default | Meaning |
|---|---|---|---|
| `policies` | `list[Policy]` | `[]` | Named predicates over the `Trace`; all must hold. |

```python
from windtunnel.api.scenario import Policy
Policy(name="no_bypass_approval",
       predicate=lambda trace: not _approval_was_bypassed(trace),
       effect_class="external_send")   # optional, for side-effect grouping
```
A predicate returns `True` = satisfied. A predicate that *raises* is recorded as a
failure (with the exception text) — it never crashes the evaluator.

#### Verifying external state: `trace.observations`

Some scenarios succeed or fail in the *world*, not the transcript: the agent
was supposed to open a PR against `main`, insert a row, write a file. Don't
write a policy that queries the live fixture (a fake GitHub, a seeded
database) — that verdict dies with the fixture, and the saved trace can never
be re-scored. Instead, wire a **`StateProbe`**
(`windtunnel/spi/state_probe.py`): the runner calls `probe.reset()` before
each run, and `probe.capture()` after the final turn, freezing the snapshot
into `trace.observations` *before* scoring. Your policy then reads plain
data:

```python
Policy(name="pr_opened_against_main",
       predicate=lambda t: any(pr["base"] == "main"
                               for pr in t.observations["github"]["prs"]))
```

A `Policy` is a **constraint** expectation and therefore joins the inferred gate.
If the external state is the primary success criterion, read
`trace.observations` from an `outcome_fn`; use a policy when it is an additional
guardrail alongside a separate outcome.

This completes the trace's evidence triad: `turns[*].tool_calls` is the
agent's own account, `mcp_calls` is what reached the mock tool server, and
`observations` is the world the agent left behind. All three are data on the
trace, so verdicts survive offline re-scoring (`wt compare`, triage,
sidecars). A `capture()` that raises records a `probe_error: ...` worker
warning instead of crashing the run, so a dead fixture is distinguishable
from a violated policy. Probes are wired per pack via
`ScenarioPack.state_probe_factory` (see "Shipping a scenario pack" below) or
passed directly as `run_scenario(..., state_probe=probe)`.

### Experiment integrity and robustness
| Field | Type | Default | Meaning |
|---|---|---|---|
| `perturbations` | `list[Perturbation]` | `[]` | Adversarial conditions applied to the run and verified by the integrity check (see below). |

Integrity asks whether the declared test condition actually happened. A failed
integrity check makes the aggregate `INVALID`; it never counts as agent
success or failure. Robustness is the scenario gate's pass rate under valid
perturbed conditions and is reported only for scenarios with perturbations.

### Multi-turn
| Field | Type | Default | Meaning |
|---|---|---|---|
| `user_turns` | `list[str]` | `[]` | When **non-empty**, this is the full ordered user-turn sequence: the runner sends each entry under one `session_id` and ignores `prompt`. The last entry is the scored turn and is also used by prompt-reading surfaces, so duplicating it in `prompt` is unnecessary. Empty means single-turn (`prompt` is sent). |

### Metadata
| Field | Type | Default | Meaning |
|---|---|---|---|
| `gate_layers` | `list[GateLayer]` | `None` | Infers outcome plus every declared trajectory/constraint expectation. Set explicitly to make selected layers diagnostic; integrity remains mandatory. |
| `failure_cost` | `FailureCost` | safest profile | Stable operational risk metadata. Reports calculate a deterministic `risk_weight` and rank failing aggregates by weighted `failure_risk`; it never excuses a gated regression. |
| `variance_allowed` | `bool` | `False` | If `True`, the deploy gate accepts sub-100% and reports `pass_rate ± stddev` (sampler-sensitivity dim). |
| `tags` | `list[str]` | `[]` | Convention: `"dim:<name>"` groups regressions by dimension. |

---

## Perturbations

A perturbation adversarially stresses one run. Every perturbation declares a
`marker`; the runner ensures it lands in `trace.worker_warnings`, and
`evaluate_integrity` verifies that contract. Two families:

### Family 1 — pre-send / history-shaping (subclasses of `PreSendPerturbation`)
Inject corrupted prior turns into the `messages` the **live model runs on** (via
the `/v1/runs` input, which the platform splits into `conversation_history = messages[:-1]`
+ new user turn `messages[-1]`). These subclass `PreSendPerturbation` (exported
from `windtunnel.api`; abstract `shape_messages(messages, scenario)`); the runner
dispatches on the class, injects **before** `handle.send()`, and **skips** the
post-hoc `apply()` (no double-application) — it only records the marker.

| Class | Injects | Simulates |
|---|---|---|
| `BlankAssistantContent` | a blank synthesis turn after a tool round-trip | an empty-turn ICL bug (colon-stop) |
| `FallbackRenderLeak` | a prior assistant turn whose content is literal `tool: {...}` text | a fallback-render leak |
| `MalformedToolCall` | a prior call with wrong arg field names (`x_query`) + empty result | model copying its own bad keys |
| `InjectWrongPriorToolCall` | a plausible-but-wrong tool call + plausible-wrong result | wrong tool choice → self-correct |
| `InjectSchemaRejectedCall` | a valid-shape call with invalid arg *values* + schema error | retry with valid args |
| `InjectPaginationTruncation` | a truncated result with `hasMore=true` | notice + paginate |
| `InjectStaleMemory` | a stale line into the system context ("Relevant saved memory:") | memory-vs-live-tool conflict (hybrid) |

### Family 2 — env-shaping (mock-MCP failure modes)
Don't touch history; configure the mock to misbehave. The runner sets
`MOCK_MCP_FAILURE_MODE` before starting the mock; the mock's `synthetic_db.py`
reads it and injects the failure on the live tool path.

| Class | Mode | Simulates |
|---|---|---|
| `ToolReturnsMalformedJson` | `malformed_json` | broken (non-JSON) tool output |
| `ToolTimeoutPerScenario` | `timeout` | tool hang exceeding `connect_timeout` |
| `ToolReturnsEmptyUnexpected` | `empty_unexpected` | 0 results when data is implied |

> When authoring an env-shaping scenario, scope the failure to the tool the
> perturbation *targets*. (The silent-failure scenario originally over-poisoned
> `find_clients` so the model never reached the tool meant to be empty — it
> tested "broken directory," not silent failure.)

---

## The 10 dimensions

Each dim is a package: `dim_<name>/scenarios.py` (+ `mock_mcp/server.py` +
`synthetic_db.py`, except `icl_poisoning` which reuses the tool_affordance mock).

| Dim | Scenarios | Perturbations / failure modes |
|---|---|---|
| `tool_affordance` | `lookup_before_action`, `field_scope_inference`, `wrong_tool_correction`, `investigate_before_export`, `export_customer_products` | none (trajectory: must_call/order/forbidden) |
| `clarify_vs_guess` | `ambiguous_entity`, `missing_required_param`, `multiple_plausible_actions` | none (forbidden_calls encode "guessed"); 4-bucket verdict |
| `memory_conflict` | `stale_vs_current`, `memory_overrides_user`, `two_conflicting_memories` | `InjectStaleMemory` (+ a `Policy` on the overrides one) |
| `multi_turn_drift` | `constraint_change_mid_flow`, `pronoun_resolution`, `topic_switch_and_return` | none (drift via the `user_turns` field) |
| `policy_pressure` | `skip_approval_pressure`, `conflicting_param_override`, `urgency_override` | none (pressure in prompt); `Policy` predicates + forbidden_calls; 3-bucket |
| `recovery` | `wrong_tool_then_correct`, `bad_arg_then_retry`, `empty_result_then_alternate_lookup`, `partial_result_then_clarify` | `InjectWrongPriorToolCall` / `InjectSchemaRejectedCall` / `InjectPaginationTruncation` |
| `sampler_sensitivity` | `typo_recovery`, `comparison_which_has_more`, `multi_step_followup` | none — the perturbation IS the temp/top_p sweep; `variance_allowed=True` |
| `side_effect_safety` | `safe_draft_only`, `destructive_clarify`, `silent_internal_update` | none; `Policy` predicates per effect-class; 5-bucket; severity=critical |
| `silent_failure` | `tool_returns_malformed_json`, `tool_timeout`, `tool_returns_empty_unexpected` | `ToolReturnsMalformedJson` / `ToolTimeoutPerScenario` / `ToolReturnsEmptyUnexpected` (all target `order_query`) |
| `icl_poisoning` | `empty_prior_assistant_turn`, `primitive_fallback_leak`, `bad_prior_call_pattern` | `BlankAssistantContent` / `FallbackRenderLeak` / `MalformedToolCall` (+ `Policy` predicates) |

The shared `synthetic_db.py` fact base across dims: clients `ACC-LSPN-001` (Lowell
Spinners), `ACC-PORT-001` (Portland Pickles), `ACC-CHIC-001` (Chicago Cubs); SKU
`B001AAA`; etc. Keep new scenarios consistent with it.

---

## Adding a new scenario

1. Pick the dim it belongs to (or create a new `dim_<name>/` package).
2. Add the `Scenario(...)` to that dim's `scenarios.py` and append it to the dim's
   exported list (e.g. `SILENT_FAILURE_SCENARIOS`).
3. If it needs tools, define them in the dim's `mock_mcp/server.py` (FastMCP,
   **bare** tool names — the platform adds its integration prefix, e.g. `ops.`) backed by `synthetic_db.py`.
4. Tag it `dim:<name>` for selection and reporting; the dim's owning `PACK` (a
   `ScenarioPack` in the dim's `__init__.py`) directly supplies its runtime
   wiring. Discovery validates the tag against that pack, so a stale rename
   fails loudly. New dim? Build a `PACK` and add it to
   `windtunnel/scenarios/__init__.py`'s `builtin_packs()` list.
5. Run it: `uv run wt run --runtime in_memory --scenario <name> --runs 1`
   (or with your platform runtime).

---

## Shipping a scenario pack

A dimension doesn't have to live in this repo. Bundle it into a
`ScenarioPack` and register it under the `windtunnel.scenario_packs`
entry-point group — the scenario-side twin of the `windtunnel.runtimes`
group that runtime drivers use — and `wt run` discovers it without any
change to the framework:

```python
# my_pack/pack.py
from windtunnel.api import Scenario, ScenarioPack

PACK = ScenarioPack(
    name="invoice_hygiene",          # owning pack and --pack identity
    scenarios=[Scenario(...), ...],
    mcp_factory=None,                # or Callable[[Scenario], MCPServer], see below
    state_probe_factory=None,        # or Callable[[Scenario], StateProbe | None]
    transport_only=False,
    owner="team-billing",            # free-form: a team, a handle, a CODEOWNERS path
    metadata={},                     # free-form annotations; core never interprets it
)
```

In your package's `pyproject.toml`:

```toml
[project.entry-points."windtunnel.scenario_packs"]
invoice_hygiene = "my_pack.pack:PACK"   # a ScenarioPack instance, or a
                                        # zero-arg callable returning one
```

What `wt run` does with it:

- **Selection pool.** Your `scenarios` join the built-in ones (built-ins
  first, entry-point packs after); `--scenario` (exact or glob), `--tag`,
  `--pack`, and `--owner` filter across all packs, and omitting them runs
  everything.
- **Dimension metadata.** `dim:<name>` tags remain available to `--tag`
  filters, but they do not control runtime wiring. At discovery, any scenario
  that declares `dim:` tags must include `dim:<owning-pack-name>`, and every
  named dimension must be a registered pack. Tagless packs remain valid.
- **Ownership.** `owner` is carried into every ledger record and drives
  `wt run --owner <owner>` selection. Wind Tunnel attaches no other
  semantics — what ownership *means* (routing, gating, paging) is yours.
- **Mock tools.** If your dim needs canned upstream tools, set
  `mcp_factory` to a callable that takes the selected `Scenario` and returns
  a fresh, **not-yet-started** `MCPServer` (the runner owns start/stop per
  batch). It is read directly from the selected scenario's owning pack, never
  inferred from tags, and is only invoked for runtimes that accept
  runner-managed MCP servers — the built-in `in_memory` runtime is scripted
  and ignores mocks. Most factories ignore the scenario argument; take it when
  the mock must specialize per scenario (the built-in `silent_failure` pack
  derives its failure mode from the scenario's perturbation).
- **External-state evidence.** If your dim verifies world state (see
  "Verifying external state" above), set `state_probe_factory` to a callable
  that takes the selected `Scenario` and returns a `StateProbe` (or `None`
  for scenarios it doesn't observe). It is also read directly from the owning
  pack and is independent of whether the runtime mounts runner-managed MCPs.
  When the probe's fixture is started by your runtime plugin's `pre_run()` (the
  usual driver shape), ship the pack with `state_probe_factory=None` and have
  `pre_run()` set it on your module-level `PACK` once the fixture is up —
  `pre_run` fires before any scenario, and the CLI reads the factory afterward.
  Add `StateProbeAvailable()` to every scenario that requires observations.
- **`transport_only=True`** marks a dim whose history-shaping perturbation is
  applied post-hoc to the trace (the live model never saw it): the scenario
  still runs and reports, but its model verdict doesn't flip the exit code.
  Leave it `False` unless you know your perturbation is counterfactual — see
  `windtunnel/api/pack.py` for the full semantics.

Like runtime plugins, entry points refresh only on reinstall: after touching
your `pyproject.toml`, reinstall (`uv sync` / `uv pip install -e .`) before
trusting discovery.

---

## A note on model behavior the bench has surfaced

Authoring scenarios is also *interpreting* failures. Some failures are real model
limits a scenario can't paper over — e.g. a small model may phantom-call a `web_search`
(or a `create-csv` skill) to find a client's email rather than use the granted
`client_lookup`, regardless of tool description or operator steering. When a
scenario "fails," check whether it's the model, the harness (an evaluator/mock
bug), or the scenario design — the structured trace usually tells you which.
