# Recording a tool universe

A **universe file** (`*.universe.json`) is a hermetic fake upstream built from
recorded tool-call/result pairs. `RecordedMCPServer` loads one and serves it as
an ordinary MCP server, so a scenario can rerun against *exactly the world a
production incident happened in* — same tools, same data, no live upstream.

This is the general-purpose fake-data layer: recordings can come from a
production trace, from a live run's call log (record mode, below), or be
written by hand. For the design rationale — why queryable-not-scripted, why
these matching rules — see the
[trace re-seeding design spine](design/0001-trace-reseeding.md).

---

## The file format

```json
{
  "windtunnel_universe": 1,
  "tools": [
    {
      "name": "client_lookup",
      "description": "Look up a client by name or account id.",
      "input_schema": { "type": "object", "properties": { "query": { "type": "string" } } },
      "result_schema": { "type": "object" }
    }
  ],
  "recordings": [
    {
      "tool_name": "client_lookup",
      "args": { "query": "Bluewing Logistics" },
      "result": { "name": "Bluewing Logistics", "email": "ops@bluewing.example" }
    }
  ],
  "matching": {
    "on_miss": "fail_call",
    "arg_keys": { "client_lookup": ["query"] }
  }
}
```

- `tools[*].input_schema` is what the agent is offered; `result_schema` is
  optional and only consulted by the `empty` miss policy.
- `recordings[*]` are deliberately the same shape as `MCPCall`
  (`tool_name`, `args`, `result`) — a recording is a witnessed call at rest.
  `args` are stored flat (`{"query": ...}`), never in a wire wrapper; the
  loader and recorder normalize.

!!! warning "Producers must redact"
    Recordings hold verbatim tool results — scrub PII, credentials, and
    anything else you wouldn't commit *before* a universe file enters version
    control. Wind Tunnel replays what you froze; it never sanitizes it.

---

## Serving it

`RecordedMCPServer` implements the standard `MCPServer` SPI, so it drops in
anywhere a mock server goes — `run_scenario(mcps=[...])` directly, or a
pack's `mcp_factory`:

```python
from windtunnel.api import ScenarioPack, load_universe
from windtunnel.mcp import RecordedMCPServer

PACK = ScenarioPack(
    name="imported_incidents",
    scenarios=[...],
    mcp_factory=lambda scenario: RecordedMCPServer("fixtures/lookup_bluewing.universe.json"),
)
```

The runner starts it, hands the agent its URL, drains `call_log()` into
`trace.mcp_calls`, and stops it — the same lifecycle as any mock. The handle's
`call_tool()` is also public, so in-process tests can exercise replay without
HTTP.

## How a live call finds a recording

A rerun agent will not reproduce the recorded call sequence — it rephrases,
retries, takes another route. So lookup is **queryable and stateless**: the
same query always returns the same recording (retries are not divergences),
resolved in order:

1. **Exact** — same tool, canonically-equal args (sorted keys, JSON-canonical
   values).
2. **Keyed** — equal values for the tool's `arg_keys` subset, ignoring the
   rest. This is the workhorse: `client_lookup(query="Bluewing Logistics",
   verbose=true)` still hits a recording made without `verbose`.
3. **Miss** — apply the divergence policy.

Matching is deterministic and explainable in a diff — no fuzzy matching in
core. Genuinely stateful tools (a counter, a paginated cursor) can opt into
consume-once semantics with per-tool `"mode": "sequence"`.

## Divergence policies

`matching.on_miss` sets the default; `per_tool_on_miss` overrides per tool
(a read-only search can be `nearest` while `payment_submit` stays
`fail_call`).

| `on_miss` | The agent sees | Use when |
|---|---|---|
| `fail_call` *(default)* | a structured tool error: `{"error": "no_recorded_result", ...}` | you want hermetic and loud — a miss doubles as a probe: does the agent notice, or fabricate? |
| `empty` | a schema-shaped empty result (`[]` / `{}` / `""`) | "no results" is a valid, safe answer for this tool |
| `nearest` | the recording sharing the most arg key/values (deterministic tie-break) | exploratory reruns where completion beats strictness |
| `synthesize` | whatever your hook returns: `Callable[[str, dict, Universe], Any]` | you bring a generator (a faker, an LLM); core ships only the hook |

**Every divergence is evidence, whatever the policy.** A miss lands in
`call_log()` with `extra={"divergence": {...}}` and emits a
`universe_divergence:` worker warning — so it's *scorable*, not merely logged.
The scorer library's `no_divergence()` returns a constraint-layer `Policy`
for scenarios that must stay fully inside the recording:

```python
from windtunnel.api import no_divergence

Scenario(..., policies=[no_divergence()])
```

## Record mode: freezing a live run

Any handle's `call_log()` can be re-frozen into a universe file with zero
shape conversion — run once against a rich hand-built mock, freeze, and the
replay fixture is free:

```python
from windtunnel.api import UniverseTool, freeze_universe

freeze_universe(
    handle.call_log(),
    tools=[UniverseTool(name="client_lookup", input_schema={...})],
    matching={"arg_keys": {"client_lookup": ["query"]}},
    path="fixtures/lookup_bluewing.universe.json",
)
```

Recording against live production tools is deliberately out of scope — that
is an exporter's job, upstream of the
[interchange format](design/0001-trace-reseeding.md#contract-a-the-trace-interchange-format).
