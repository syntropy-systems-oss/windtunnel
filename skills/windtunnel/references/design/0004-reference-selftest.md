<!-- GENERATED from docs/design/0004-reference-selftest.md at 215449d4ac9e — do not edit; edit docs/design/0004-reference-selftest.md. -->
---
description: "Design specification for live golden/poison scenario self-tests, the optional runtime inference-substitution capability, isolation, probe timing, and CI verdicts."
---
# 0004: Reference self-tests

## Status

Accepted for 0.10.0.

## Problem

A scenario can fail because the agent regressed, but it can also fail because
the harness is miswired or the gate is wrong. Ordinary agent runs cannot prove
that a declared gate recognizes a known-correct trajectory or rejects a known
defect. Wind Tunnel therefore needs tests for the test itself.

A reference self-test has two complementary cases:

- a **golden** case scripts known-correct model decisions and must pass the
  scenario's declared gate;
- a **poison** case scripts a named known-bad decision path and must fail that
  gate.

The script replaces model inference only. The runtime's real agent loop, tool
mounting, fixture mutations, MCP evidence, state probes, preconditions, trace
construction, and scoring remain live. A fabricated final trace would test a
scorer in isolation; it would not certify the plumbing that produces the
evidence the scorer consumes.

## Public authoring contract

Reference cases belong to `Scenario`, alongside the gate they certify:

```python
from windtunnel.api import (
    ReferenceCase,
    ReferenceDecision,
    ReferenceToolCall,
    Scenario,
)

scenario = Scenario(
    name="artifact_guard",
    prompt="Create the safe artifact.",
    target_facts=[["artifact complete"]],
    requires_tool_use=True,
    reference_cases=[
        ReferenceCase(
            name="known-good",
            kind="golden",
            decisions=(
                ReferenceDecision(tool_calls=(
                    ReferenceToolCall("write_artifact", {"safe": True}),
                )),
                ReferenceDecision(content="artifact complete"),
            ),
        ),
        ReferenceCase(
            name="unsafe-write",
            kind="poison",
            decisions=(
                ReferenceDecision(tool_calls=(
                    ReferenceToolCall("write_artifact", {"safe": False}),
                )),
                ReferenceDecision(content="artifact complete"),
            ),
        ),
    ],
)
```

`decisions` are successive model responses inside one agent turn. Every
non-final decision has at least one tool call. The final decision has non-empty
content and no tool calls. Tool arguments must be JSON-serializable. Case names
must be unique within a scenario.

Reference data is source-controlled test data. Use fictional fixtures and
never embed production prompts, credentials, customer records, or private
incident payloads.

## Optional runtime capability

An ordinary `AgentRuntime` remains valid. A runtime opts into self-testing by
also satisfying `ReferenceCapableAgentRuntime`:

```python
from windtunnel.spi import AgentConfig, ReferenceCase

class MyRuntime:
    def provision(self, config: AgentConfig, mcps=None):
        ...

    def provision_reference(self, config: AgentConfig, case: ReferenceCase, mcps=None):
        self.inference_substitute.install(case.decisions)
        return self._provision_normal_agent(config, mcps)
```

The capability is declared once per runtime service, not configured per
scenario. The driver owns the location and mechanism of its inference seam;
Wind Tunnel does not model URLs, proxies, provider protocols, or service
topology. `provision_reference()` must return an otherwise normal
`AgentHandle`, and `handle.teardown()` must restore or remove the substitution.

The built-in `in_memory` runtime intentionally does **not** implement this
capability. It shortcuts the real agent/tool loop, so reporting it as a
reference-capable runtime would certify a different system.

## Execution and isolation

`wt selftest` uses normal runtime and pack discovery, then executes this order:

1. Resolve the runtime plugin and build the runtime once.
2. Discover and select scenario packs with the same `--scenario`, `--tag`,
   `--pack`, and `--owner` semantics as `wt run`.
3. Verify that the runtime exposes `provision_reference()`. An unsupported
   runtime produces explicit `UNSUPPORTED` case records and does not prepare
   fixtures or call pack factories.
4. Call the plugin's optional `pre_run(runtime, scenarios, runtime_name)` once.
5. For each reference case, read the selected scenario's **owning pack** and
   create fresh MCP/probe wiring.
6. Call `provision_reference()` once for that case, then run the ordinary
   `run_scenario()` path with one run.
7. Tear down the handle before starting the next case.

Fresh provisioning and fresh pack factories are required per case. A golden
case must not seed state that makes a poison case pass or fail, and one poison
must not contaminate another.

### State-probe visibility timing

`StateProbeAvailable()` inspects `PreconditionContext.state_probe`; it does not
discover probes hidden inside plugins, scorers, or runtime internals. In the
CLI path, that context is populated only by this sequence:

1. `RuntimePlugin.pre_run()` may install or replace the selected owning pack's
   `state_probe_factory` after its live fixture is ready.
2. `wt selftest` calls that factory for the current case.
3. The returned probe is passed to `run_reference_case(..., state_probe=...)`.
4. The ordinary runner provisions the handle and evaluates preconditions.

A scorer-level "probe missing" guard can still protect scoring, but it does
not populate `PreconditionContext.state_probe`. Declare
`StateProbeAvailable()` only where the owning pack factory or a direct library
call guarantees that the probe exists before preconditions run.

## Verdicts and exit codes

Self-test verdicts are deliberately separate from ordinary scenario scores:

| Verdict | Meaning |
|---|---|
| `PASS` | Golden passed its gate, or poison failed its gate. |
| `GOLDEN_FAILED` | Known-correct behavior did not pass the gate. |
| `POISON_PASSED` | Known-bad behavior escaped the gate. |
| `UNSUPPORTED` | Runtime does not expose the optional capability. |
| `ERROR` | Execution was invalid: precondition, integrity, setup, runtime, probe-capture, or MCP-evidence failure. |

A rejected poison has an ordinary failed `Score` and a self-test verdict of
`PASS`; collapsing those values would invert the result. JSON and JUnit output
therefore carry the self-test verdict explicitly while retaining the ordinary
score as evidence.

- exit `0`: every selected reference case is `PASS`;
- exit `1`: any case is `GOLDEN_FAILED`, `POISON_PASSED`, or `ERROR`;
- exit `2`: unsupported runtime, invalid CLI usage, or no selected references.

Every executed case writes the normal trace and score sidecar under
`runs/selftest` by default. It does not append to the ordinary run ledger or
change normal pass-rate aggregates.

```bash
wt selftest --runtime <reference-capable-runtime>
wt selftest --runtime <runtime> --pack synthetic \
  --format junit --out selftest.xml
```

## Non-goals

- Reference cases are not a substitute for stochastic real-model runs.
- Wind Tunnel does not prescribe an inference provider or transport.
- Core does not execute tool calls from the script; the live agent loop does.
- Self-test results do not rewrite scenario gates or bless API stability.
