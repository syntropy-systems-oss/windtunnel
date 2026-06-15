# Writing a Scenario

A `Scenario` is the single authoring surface for everything a bench run is graded
on. It is backend-agnostic — **never import a runtime or mock type from a
scenario** (enforced by `tests/test_import_invariants.py`).

This is the authoring reference. For how scoring works conceptually, see
[`architecture.md`](architecture.md#3-the-four-layer-scoring-model).

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

`name`, `prompt`, and `target_facts` are required; everything else defaults to "no
expectation."

---

## The `Scenario` schema (`api/scenario.py`)

Fields are grouped by the scoring layer each feeds.

### Identity
| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Scenario identity (used as `--scenario` selector + trace `scenario_id`). |
| `prompt` | `str` | The user prompt that drives the run. |

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

Scenario(name="...", prompt="...", target_facts=[], outcome_fn=_graded, requires_tool_use=True)
```

Like `policies`/`trajectory_checks`, `outcome_fn` is a callable, so it isn't
serialized — it's reconstructed when the scenario's pack is re-imported (offline
re-scoring needs the pack importable).

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

### Trajectory layer (recorded, not the gate)
| Field | Type | Default | Meaning |
|---|---|---|---|
| `must_call` | `list[str]` | `[]` | Tools that must all appear. Use the CANONICAL bare tool name (e.g. `client_lookup`) — the evaluator matches platform-decorated variants (`mcp_acme_ops_client_lookup`, `ops.client_lookup`) by suffix-at-word-boundary. |
| `forbidden_calls` | `list[str]` | `[]` | Tools that must never appear (e.g. forbid `invoice_send` to test "clarify, don't act"). |
| `order_matters` | `bool` | `False` | If `True`, `must_call` must appear as an in-order subsequence. |
| `trajectory_checks` | `list[TrajectoryCheck]` | `[]` | Custom verifiers over the observed call path; ANDed with the sugar fields above (see below). |

> `must_call=['clarify']` will fail trajectory ~100% (models clarify in prose, not
> via a literal `clarify` tool). Trajectory isn't the gate, so this doesn't change
> the pass count — but prefer `forbidden_calls` to encode "should have clarified."

**Custom `TrajectoryCheck`** — the trajectory layer's counterpart to `Policy`
(constraint) and `Perturbation` (robustness): a verifier over the path the agent
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

A `Policy` is the **constraint** layer — recorded, but it does **not** drive the
headline pass/fail (only the outcome layer does). If the external state *is* the
success criterion (the gate), read `trace.observations` from an **`outcome_fn`**
instead (see the Outcome layer above); use a policy when it's an *additional*
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

### Robustness layer
| Field | Type | Default | Meaning |
|---|---|---|---|
| `perturbations` | `list[Perturbation]` | `[]` | Adversarial stressors applied to the run (see below). |

### Multi-turn
| Field | Type | Default | Meaning |
|---|---|---|---|
| `user_turns` | `list[str]` | `[]` | When **non-empty**, this IS the full ordered user-turn sequence: the runner sends each entry under one `session_id` (accumulating history) and **ignores `prompt`**. The LAST entry is the scored turn (evaluators always score the final assistant turn). Convention: set `prompt` to a copy of that last entry so prompt-reading surfaces (triage, the LLM judge) show the scored question. Empty = single-turn (`prompt` is sent). |

### Metadata
| Field | Type | Default | Meaning |
|---|---|---|---|
| `failure_cost` | `FailureCost` | safest profile | `severity`/`customer_visible`/`reversible`/`side_effect_performed` — for weighted aggregation. |
| `variance_allowed` | `bool` | `False` | If `True`, the deploy gate accepts sub-100% and reports `pass_rate ± stddev` (sampler-sensitivity dim). |
| `tags` | `list[str]` | `[]` | Convention: `"dim:<name>"` groups regressions by dimension. |

---

## Perturbations

A perturbation adversarially stresses one run. Every perturbation declares a
`marker`; the runner ensures it lands in `trace.worker_warnings`, and
`evaluate_robustness` verifies that contract. Two families:

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
4. Tag it `dim:<name>`; the dim's `PACK` (a `ScenarioPack` in the dim's
   `__init__.py`) binds that tag to the dim's `MCPServer` factory so the runner
   provisions the right mock. New dim? Build a `PACK` and add it to
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
    name="invoice_hygiene",          # scenarios carry tags=["dim:invoice_hygiene"]
    scenarios=[Scenario(...), ...],
    mcp_factory=None,                # or Callable[[Scenario], MCPServer], see below
    state_probe_factory=None,        # or Callable[[Scenario], StateProbe | None]
    transport_only=False,
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
  first, entry-point packs after); `--scenario <name>` filters across all
  packs, and omitting it runs everything.
- **Mock tools.** If your dim needs canned upstream tools, set
  `mcp_factory` to a callable that takes the selected `Scenario` and returns
  a fresh, **not-yet-started** `MCPServer` (the runner owns start/stop per
  batch). It's matched to your scenarios by the `dim:<name>` tag, and only
  invoked for plugin runtimes — the built-in `in_memory` runtime is scripted
  and ignores mocks. Most factories ignore the scenario argument; take it
  when the mock must specialize per scenario (the built-in `silent_failure`
  pack derives its failure mode from the scenario's perturbation).
- **External-state evidence.** If your dim verifies world state (see
  "Verifying external state" above), set `state_probe_factory` to a callable
  that takes the selected `Scenario` and returns a `StateProbe` (or `None`
  for scenarios it doesn't observe). Same `dim:<name>`-tag dispatch and
  plugin-runtime-only invocation as `mcp_factory`. When the probe's fixture
  is started by your runtime plugin's `pre_run()` (the usual driver shape),
  ship the pack with `state_probe_factory=None` and have `pre_run()` set it
  on your module-level `PACK` once the fixture is up — `pre_run` fires before
  any scenario, and the CLI reads the factory per scenario, not at discovery.
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
bug), or the scenario design — the four-layer trace usually tells you which.
