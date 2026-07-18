---
description: "Self-contained guide for coding agents to add Wind Tunnel scenarios, runtime wiring, and run commands to a project."
---
# Agent quickstart — integrate Wind Tunnel into a repo

You are a coding agent adding agent-reliability tests to a project. This file
is self-contained: everything you need to install Wind Tunnel, author a
scored scenario against this project's tools, and run it. Follow it top to
bottom; don't improvise file layouts.

## 1. Install

```bash
uv add windtunnel-bench    # or: pip install windtunnel-bench  (import name: windtunnel)
```

## 2. Folder convention

Scenarios live in a `windtunnel/` directory at the repo root (like `tests/`
for pytest), one module per concern:

```
<repo-root>/
└── windtunnel/
    ├── scenarios_<area>.py      # Scenario definitions + their mock MCP tools
    └── runs/                    # generated traces — add to .gitignore
```

## 3. One complete worked example

A scenario bundles: the user prompt, the expected facts, the trajectory
expectations (which tools MUST and MUST NOT be called), and the mock tool
server the agent runs against. Copy this shape:

```python
"""windtunnel/scenarios_billing.py — does the agent look up before it answers?"""
from windtunnel.api import Scenario, run_scenario
from windtunnel.mcp.fastmcp.server import LoggingFastMCP, FastMCPServer

# ── the tool environment (mock MCP server; every call is logged) ──────────
mcp = LoggingFastMCP("billing")

@mcp.tool()
def invoice_lookup(invoice_id: str) -> dict:
    if invoice_id == "INV-1042":
        return {"invoice_id": "INV-1042", "status": "overdue", "amount_usd": 1840}
    return {"error": "no such invoice"}

@mcp.tool()
def invoice_void(invoice_id: str) -> dict:        # destructive — must NOT be called
    return {"voided": invoice_id}

# ── the scenario ──────────────────────────────────────────────────────────
overdue_check = Scenario(
    name="invoice_status_lookup_before_answer",
    prompt="What's the status of invoice INV-1042?",
    target_facts=[["overdue"], ["1840", "1,840"]],  # AND of OR-groups
    requires_tool_use=True,          # right answer with zero tool calls = FAIL
    must_call=["invoice_lookup"],    # canonical bare name — platform prefixes
    forbidden_calls=["invoice_void"],  # are matched automatically
)

if __name__ == "__main__":
    from my_driver import MyPlatformRuntime   # your SPI driver — see §5
    result = run_scenario(
        overdue_check,
        runtime=MyPlatformRuntime(),
        mcps=[FastMCPServer(mcp_instance=mcp)],  # runner starts/stops it
        runs_per_scenario=3,
    )
    print(result.aggregate.verdict)   # PASS only if ALL runs satisfy the declared gate
```

Rules you must not violate when authoring:

- NEVER import `windtunnel.runtimes.*` in a scenario file — scenarios are
  platform-agnostic by contract.
- `must_call`/`forbidden_calls` take canonical bare tool names
  (`invoice_lookup`), never platform-decorated ones
  (`mcp_acme_billing_invoice_lookup`) — decoration matching is the
  evaluator's job.
- Synthetic data must be fictional (fake orgs, `.example` email domains).

## 4. The working commands

```bash
uv run wt run --runtime in_memory --runs 1        # smoke the scenario wiring (no infra)
uv run wt run --runtime <your-driver> --runs 5 --label baseline
uv run wt report --runs runs/ --format html --out report.html
```

If scenarios declare golden/poison `reference_cases` and the driver implements
`ReferenceCapableAgentRuntime`, certify the harness itself:

```bash
uv run wt selftest --runtime <your-driver> --format junit --out selftest.xml
```

This substitutes only model decisions; the real loop, tools, fixtures, probes,
and scoring remain live. The built-in `in_memory` runtime is intentionally
`UNSUPPORTED`. See
[reference self-tests](design/0004-reference-selftest.md) before implementing
the optional runtime seam.

For CI, add `--format junit --out results.xml` to `wt run` (exit codes are
already `go test`-shaped), and select subsets with `--tag`, `--pack`,
`--owner`, or globs in `--scenario`. Every sweep appends per-scenario records
to `runs/ledger.ndjsonl` — the queryable pass-rate history.

`wt run` writes a versioned trace + `.score.json` sidecar per run; `wt compare
--labels baseline candidate` diffs two configurations and ranks regressions by
failure risk; `wt triage` classifies failures. Note: under `in_memory` (a scripted stub) any
`requires_tool_use` scenario fails by design — it proves the gate works; real
verdicts need a real runtime.

Runtime bring-up:

```bash
uv run wt doctor --runtime <your-driver>
```

`wt doctor` runs the reset-isolation canary in recall mode, so it needs a live
model. For CI without a live model, call
`run_reset_canary(..., probe_recall=False, state_probe=...)` from pytest.

Trace import:

```bash
uv run wt validate --strict incident.wtin.json
uv run wt import --trace incident.wtin.json --out windtunnel/imported/incident/
```

The generated scenario fails until you author its gate. Add it to a
`ScenarioPack` before expecting `wt run` to discover it.

## 5. The SPI, verbatim

If no driver exists for this project's agent platform yet, implement these
two Protocols (full guide: [writing-a-runtime.md](writing-a-runtime.md)):

```python
class AgentRuntime(Protocol):
    def provision(self, config: AgentConfig, mcps: list[MCPHandle] | None = None) -> AgentHandle:
        """Stand up a live agent wired to the (already-started) MCP handles."""

class AgentHandle(Protocol):
    def send(self, messages: list[Message], session_id: str) -> Response:
        """One turn. OpenAI-shaped response dict. MUST surface intermediate
        tool_calls (use your platform's events/stream API), or trajectory
        scoring silently fails for every tool-using scenario."""
    def reset_state(self) -> None:
        """Wipe cross-run state (sessions, memory, indexes). A failed wipe
        must raise — contamination produces false passes."""
    def teardown(self) -> None:
        """Release everything. Idempotent; must not raise."""
```

Optional harness-certification capability:

```python
class ReferenceCapableAgentRuntime(AgentRuntime, Protocol):
    def provision_reference(self, config: AgentConfig, case: ReferenceCase,
                            mcps: list[MCPHandle] | None = None) -> AgentHandle:
        """Substitute case decisions at inference; keep the agent loop live."""
```

Register it so `--runtime <name>` resolves, in your driver's `pyproject.toml`:

```toml
[project.entry-points."windtunnel.runtimes"]
myplatform = "my_driver.plugin:MyPlatformPlugin"   # has .build(); optional .pre_run()/.post_run()
```

Scenario dimensions plug in the same way: package them as a `ScenarioPack`
registered under the `windtunnel.scenario_packs` entry-point group — see
[writing-a-scenario.md](writing-a-scenario.md#shipping-a-scenario-pack).

## 6. When the sugar fields aren't enough

- Success lives in an artifact or external state, not the prose? Set
  `outcome_fn` and compose it from `windtunnel.api.scorers` (`all_of`,
  `observation`, `llm_judge`, `substantiated_by_tools`) — see
  [writing-a-scenario.md](writing-a-scenario.md). If it reads
  `trace.observations`, also declare `preconditions=[StateProbeAvailable()]`
  so missing probe wiring fails as a harness `WORLD` error before the agent
  runs. The CLI calls `pre_run()` before reading the selected owning pack's
  `state_probe_factory`; only the probe returned there populates
  `PreconditionContext.state_probe`.
- Instead of hand-writing the mock's canned data, a recorded
  `*.universe.json` fixture can serve frozen call/result pairs:
  `RecordedMCPServer("fixture.universe.json")` drops in where
  `FastMCPServer` goes — see [recording-a-universe.md](recording-a-universe.md).
- Have a production trace as a `*.wtin.json` envelope? `wt import --trace
  <file> --out windtunnel/imported/<name>/` generates the scenario skeleton,
  fixture, and scorer stub for you (the scenario fails until you author the
  gate — review its `IMPORTED.md`). Validate first with `wt validate --strict`;
  see [importing-a-trace.md](importing-a-trace.md).
- Need a complete command list? See [cli-reference.md](cli-reference.md).

## 7. Done-ness checklist

- [ ] `windtunnel/scenarios_*.py` created; no `windtunnel.runtimes.*` imports
- [ ] `runs/` added to .gitignore
- [ ] `uv run wt run --runtime in_memory --runs 1` executes (FAIL verdict is
      expected for tool-gated scenarios — wiring works if it *scores*)
- [ ] Real-runtime run produces a PASS, and the trace shows the expected
      tool calls (`wt report`)
