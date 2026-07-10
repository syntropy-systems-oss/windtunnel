---
description: "Step-by-step guide to install Wind Tunnel, run and report scenarios, gate CI, and triage failures."
---
# Getting started

Ten minutes from clone to your first scored agent run.

## Install

```bash
git clone https://github.com/syntropy-systems-oss/windtunnel
cd windtunnel
uv sync                                    # creates .venv + installs the dev group
.venv/bin/pytest -m "not integration" -q   # sanity: the unit suite needs no infra
```

## 1. Score your first scenario (no agent platform required)

The `in_memory` runtime returns scripted responses — it exists so you can
learn the scoring model and write scenarios before wiring up a real platform.

```python
from windtunnel.api import Scenario, run_scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime

scenario = Scenario(
    name="capital_of_france",
    prompt="What is the capital of France?",
    target_facts=[["Paris"]],
)

runtime = InMemoryRuntime(scripted_responses=["The capital of France is Paris."])
result = run_scenario(scenario, runtime, mcps=[])

print(result.aggregate.verdict)            # "PASS"
print(result.runs[0].score.outcome.detail) # why the outcome layer passed
```

Change the scripted response to `"It's Lyon."` and run again — `FAIL`, and
the outcome detail tells you which fact group failed to match.

## 2. Add trajectory expectations — and watch the gate catch a guess

Facts can be guessed. To assert the agent *worked* for the answer, gate on
tool use and name the tools it must call:

```python
scenario = Scenario(
    name="lookup_before_answer",
    prompt="What is the email on file for the client Bluewing Logistics?",
    target_facts=[["ops@bluewing.example"]],
    requires_tool_use=True,            # zero tool calls → outcome FAILS, even if the fact appears
    must_call=["client_lookup"],       # trajectory layer: this tool must be called
    forbidden_calls=["delete_client"], # ...and this one must not
)

runtime = InMemoryRuntime(
    scripted_responses=["The email on file is ops@bluewing.example."]
)
result = run_scenario(scenario, runtime, mcps=[])
print(result.aggregate.verdict)            # "FAIL"
print(result.runs[0].score.outcome.detail) # requires_tool_use: trace has zero tool calls
```

Read that carefully: the scripted answer contains the **right fact**, and
the run still fails. The in-memory runtime never calls tools — it just
talks — and `requires_tool_use` exists precisely to fail an agent that
produces the right answer without doing the work. A correct answer with no
tool call is indistinguishable from a lucky guess, and luck doesn't deploy.

`must_call` entries can be alternatives: `must_call=[["client_lookup",
"client_search"]]` means *either* satisfies the requirement. Set
`order_matters=True` to require the calls as a subsequence.

## 3. Give the agent real tools (mock MCP server)

Scenarios carry their own tool environment as an MCP server. The framework
ships a FastMCP wrapper that records every call, so trajectory scoring
asserts what the agent actually did — not what it claimed:

```python
from windtunnel.mcp.fastmcp.server import LoggingFastMCP, FastMCPServer

mcp = LoggingFastMCP("crm")

@mcp.tool()
def client_lookup(query: str) -> dict:
    if "bluewing" in query.lower():
        return {"name": "Bluewing Logistics", "email": "ops@bluewing.example"}
    return {"error": "no such client"}

server = FastMCPServer(mcp_instance=mcp)
result = run_scenario(scenario, runtime, mcps=[server])
```

The runner starts the server, hands its URL to the runtime at provision
time, resets the call log between runs, and stops it afterwards.

> **Note:** the in-memory runtime ignores MCP servers — it never calls
> tools, so this scenario still fails under it (as step 2 showed). From
> here on you're authoring scenarios for a *real* runtime: one that
> registers the server's tools with your platform so the agent can
> actually call them. That's the four-method contract in
> [writing-a-runtime.md](writing-a-runtime.md).

## 4. Stress it with perturbations

A scenario that only passes in fair weather isn't reliable. Perturbations
inject adversity — corrupted history the model must resist, or a misbehaving
tool environment:

```python
from windtunnel.api.perturbations import BlankAssistantContent, ToolReturnsMalformedJson

scenario = Scenario(
    name="survives_blank_turn",
    prompt="What is the email on file for the client Bluewing Logistics?",
    target_facts=[["ops@bluewing.example"]],
    requires_tool_use=True,
    must_call=["client_lookup"],
    perturbations=[
        BlankAssistantContent(),        # pre-send: a degenerate empty turn appears in history
        ToolReturnsMalformedJson(),     # env: the mock returns broken JSON once
    ],
)
```

The robustness layer verifies each perturbation actually applied (every one
leaves a marker in the trace), so a broken injection can't masquerade as a
pass.

## 5. Run a batch and read the report

```bash
wt run --scenario lookup_before_answer --runtime in_memory --runs 5 --label baseline
wt report --runs runs/ --format html --out report.html
```

The report groups by scenario × variant, shows the aggregate verdict,
per-layer pass rates, and links each cell to its full trace — every turn,
every tool call, every latency, exactly as the model saw it.

To compare two configurations (a model swap, a prompt change, a temperature
pin):

```bash
wt run ... --label candidate
wt compare --labels baseline candidate
```

Selection scales past exact names: `--tag dim:recovery` runs a dimension,
`--pack <name>` a pack, `--owner team-ops` everything that team owns, and
`--scenario "lookup_*"` takes shell globs. Repeating a flag ORs within it;
different flags AND together.

Every sweep also appends one record per scenario to `runs/ledger.ndjsonl` —
timestamp, verdict, per-layer pass rates, run ids, git SHA — the append-only
history that the report's latest-run view doesn't keep. Pass-rate trends and
flake detection are a few lines of `jq` away, and `wt report --format json`
emits the report's own data as a standalone artifact.

## 6. Know the rest of the CLI

Wind Tunnel ships the following `wt` commands:

| Command | What it does |
|---|---|
| `wt run` | Execute scenarios against a runtime and write traces, score sidecars, ledger rows, and optional CI artifacts. |
| `wt report` | Render saved runs as HTML, Markdown, or JSON. |
| `wt compare` | Compare run labels. |
| `wt replay` | Replay a saved trace's last user turn against a runtime. |
| `wt doctor` | Run the reset-isolation canary against a live runtime. |
| `wt import` | Generate a scenario skeleton from a Contract A `*.wtin.json` trace envelope. |
| `wt validate` | Validate and lint Contract A envelopes; use `--strict` in producer CI. |
| `wt triage` | Classify failed saved runs with the shipped rule-based classifier. |

The headline import workflow starts with a trace envelope:

```bash
wt validate --strict incident.wtin.json
wt import --trace incident.wtin.json --out scenarios/imported/incident/
```

That generated scenario intentionally fails until you author its outcome gate.
See [importing a trace](importing-a-trace.md) for the full workflow, and
[CLI reference](cli-reference.md) for all options and exit codes.

If you are bringing up a runtime, run the reset canary before trusting scores:

```bash
wt doctor --runtime <your-runtime>
```

`wt doctor` requires a live model. For hermetic CI checks without a live model,
call `run_reset_canary(..., probe_recall=False, state_probe=...)` from pytest.

## 7. Gate CI on it

`wt run` already exits like `go test` (0 pass, 1 regression or error). For
CI systems that want structure, not just an exit code:

```bash
wt run --tag dim:recovery --runs 3 --format junit --out results.xml
wt run --tag dim:recovery --runs 3 --format json  --out results.json
```

JUnit output is one `<testsuite>` per pack and one `<testcase>` per
scenario, with failures carrying the per-layer details and triage category —
GitHub Actions, GitLab, and Jenkins render it natively. The JSON document
holds the same records the ledger gets, for anything that doesn't speak
JUnit.

## 8. Triage failures

```bash
wt triage --runs runs/ --classifier rule_based
```

Each failed run is classified against the
[failure taxonomy](failure-taxonomy.md) (wrong tool, guessed instead of
clarified, fabricated after silent tool failure, ...) with a confidence and a
fix vector. See [writing-a-classifier.md](writing-a-classifier.md) to add
your own rules. An LLM-backed classifier can implement the same protocol; the
repository's implementation sketch is not registered as a CLI choice.

## Next steps

- Point it at your real platform: [writing-a-runtime.md](writing-a-runtime.md)
  — implement four small Protocols and every scenario above runs against your
  production-shaped stack unchanged.
- Author a full dimension: [writing-a-scenario.md](writing-a-scenario.md).
- Import a trace-backed regression: [importing-a-trace.md](importing-a-trace.md).
- Understand the design: [architecture.md](architecture.md).
