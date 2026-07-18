---
description: "Design spine for trace re-seeding, Contract A interchange, Contract B universes, import, scorer, ledger, and CI ergonomics."
---
# Design spine: trace re-seeding

**Status:** accepted · **Scope:** five upstream changes and the two format
contracts they share.

> Historical note: this document records the 0.8 outcome-only gate. Wind
> Tunnel 0.9 supersedes that scoring policy with inferred declared gates,
> experiment integrity, and robustness-under-perturbation semantics; see
> [Migrating to 0.9](../migrating-to-0.9.md).

This document is the design spine for making Wind Tunnel's core loop —
*turn any production trace into a regression test* — a first-class,
end-to-end path. It specifies the two format contracts that are expensive
to get wrong and cheap to implement against:

- **Contract A — the trace interchange format**: a neutral, importable
  trace schema aligned with the OpenTelemetry GenAI semantic conventions.
- **Contract B — the recorded tool-universe fixture**: a queryable fake
  upstream built from recorded tool-call/result pairs, with an explicit
  divergence policy.

Around those contracts, five changes, ranked by how much of the vision
each unblocks:

| # | Change | Depends on |
|---|---|---|
| 1 | Recorded tool-universe fixture (`RecordedMCPServer`) | Contract B |
| 2 | Outcome scorer library (`windtunnel.api.scorers`) | `Scenario.outcome_fn` (shipped) |
| 3 | Pack ownership + machine-readable run ledger | — |
| 4 | Trace import (`wt import`) | Contracts A + B |
| 5 | CI ergonomics (JUnit/JSON output, tag filtering) | — |

## The re-seeding model

A production trace already contains almost everything a regression
scenario needs: the user's ask, the tools that were available, the
answers those tools gave, and the outcome the agent produced. Re-seeding
turns that into a hermetic test:

```
production trace
   │  wt import
   ▼
scenario skeleton  +  universe fixture  +  scorer stub
   │  wt run (hermetic — recorded universe, no live upstream)
   ▼
trace + score  →  results ledger  →  CI gate / report / triage
```

The model only works if two things hold:

1. **The fake upstream is queryable, not scripted.** A rerun agent will
   not reproduce the recorded tool sequence — it will phrase the same
   lookup differently, retry, or take another route to the same answer.
   The fixture must answer *queries*, and must have a deliberate policy
   for queries the recording doesn't contain (§ Contract B).
2. **Scoring judges outcomes, not trajectories.** Exact tool sequences
   diverge immediately on rerun; the final answer, the end state, and
   the provenance of claims do not have to. Wind Tunnel's per-run gate
   is already outcome-only ([architecture](../architecture.md)); this
   spine extends that stance with a library of reusable outcome scorers
   (§ Change 2).

Trajectory, constraint, and robustness stay exactly what they are today:
recorded on every run, surfaced in reports, never the deploy gate.

---

## Contract B — the recorded tool-universe fixture

*The keystone. Specified first because Contract A's importer emits it.*

### What it is

A **universe file** is a JSON artifact holding recorded tool-call/result
pairs plus the tool schemas they came from. A `RecordedMCPServer` loads
one and implements the existing SPI — `MCPServer.start() → MCPHandle`
(`windtunnel/spi/mcp_server.py`) — so it slots into
`ScenarioPack.mcp_factory` unchanged and every existing runner/scoring
path (server-witnessed `call_log()`, `mcp_calls` on the trace,
`configure_failure_mode`) works without modification. It is also the
general-purpose hermetic fake-data layer: nothing about it requires that
the recordings came from production; they can be authored by hand or
synthesized.

### File format (`*.universe.json`)

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
    "arg_keys": { "client_lookup": ["query"] },
    "per_tool_on_miss": { "audit_log_append": "empty" }
  }
}
```

Field notes:

- `windtunnel_universe` — schema version, integer, required. Same
  forward-tolerance discipline as `Trace._from_dict`.
- `tools[*].input_schema` / `result_schema` — JSON Schema. `input_schema`
  is what the agent is offered (it becomes the served MCP tool schema);
  `result_schema` is optional and only consulted by the `empty` and
  `synthesize` miss policies.
- `recordings[*]` — deliberately the same shape as `MCPCall`
  (`tool_name`, `args`, `result`) minus the runtime-only fields
  (`timestamp_ms`, `extra`). A recording is a *witnessed call at rest*;
  `wt import` produces them by draining witnessed calls out of a trace,
  and a live run's `call_log()` can be re-frozen into a new universe file
  (record mode) with zero shape conversion.
- `args` are stored in the flat shape (`{"query": ...}`), never the
  OpenAI wire shape — the recorder normalizes on write, because lookup
  keys must be canonical even though live traffic arrives in either
  shape (the same dual-shape tolerance `MCPCall` documents).

!!! warning "Producers must redact"
    Recordings hold verbatim tool results — scrub PII, credentials, and
    anything else you wouldn't commit *before* a universe file enters
    version control; Wind Tunnel replays what you froze, it never
    sanitizes it.

### Matching: how a live call finds a recording

Lookup is **stateless** by default: the same query always returns the
same recording, however many times it is asked. (Agents retry; a
consume-once queue would turn every retry into a divergence. Sequential
consume-once semantics are a per-tool opt-in, `"mode": "sequence"`, for
genuinely stateful tools — a counter, a paginated cursor.)

Match resolution, in order:

1. **Exact** — same `tool_name`, canonically-equal `args` (keys sorted,
   values compared after JSON canonicalization).
2. **Keyed** — same `tool_name`, equal values for the tool's declared
   `arg_keys` subset, ignoring the rest. This is the workhorse: it makes
   `client_lookup(query="Bluewing Logistics", verbose=true)` hit a
   recording made without `verbose`. No `arg_keys` entry → skip this level.
3. **Miss** — apply the divergence policy.

Deliberately *not* in core: fuzzy/embedding similarity. Matching must be
deterministic and explainable in a diff; anything cleverer belongs in a
`synthesize` hook (below).

### The divergence policy

The central design decision. A rerun agent **will** ask something the
recording doesn't contain, and each policy trades hermeticity against
run completion differently:

| `on_miss` | The agent sees | Use when |
|---|---|---|
| `fail_call` *(default)* | A structured tool error: `{"error": "no_recorded_result", "tool": ..., "args": ...}` | You want hermetic and loud. Doubles as a free `silent_failure`-style probe: does the agent notice, or fabricate? |
| `empty` | A schema-shaped empty result (`[]` / `{}` per `result_schema`, `""` fallback) | The tool is a search/list where "no results" is a valid, safe answer. |
| `nearest` | The recording with the highest count of exactly-matching arg key/value pairs for that tool (deterministic tie-break: first by recording order) | Best-effort completion matters more than strictness — exploratory reruns, not CI gates. |
| `synthesize` | Whatever a user-supplied hook returns: `Callable[[str, dict, Universe], Any]` | You have a generator (an LLM, a faker, a domain model) that can extrapolate from `result_schema` + existing recordings. Core ships the *hook*, never a generator — the pure-stdlib rule holds. |

`on_miss` sets the universe-wide default; `per_tool_on_miss` overrides
per tool (a read-only search can be `nearest` while `payment_submit`
stays `fail_call`).

**Every divergence is first-class evidence, whatever the policy.** On a
miss the server:

- appends the call to `call_log()` with
  `extra={"divergence": {"policy": ..., "matched": null | <recording index>}}`,
  so it lands in `trace.mcp_calls` like any witnessed call, and
- emits a `universe_divergence: tool=<name> policy=<policy>` worker
  warning, the same marker discipline perturbations use.

That makes divergence *scorable* rather than merely logged: the scorer
library ships a `no_divergence()` policy predicate (constraint layer)
for scenarios that must stay fully inside the recording, and reports can
show divergence counts per scenario without any new plumbing.

### Non-goals

- No record-mode proxy against live upstreams in the first cut —
  `call_log()` + a `freeze_universe()` helper covers "record what my
  mock served"; recording against real production tools is a downstream
  exporter concern.
- No statefulness beyond `sequence` mode. A universe file is not a
  database simulator; if a dim needs mutable state it keeps writing a
  `synthetic_db` mock, which remains fully supported.

---

## Contract A — the trace interchange format

### Why not just `Trace` JSON?

`save_trace()` output is Wind Tunnel's *native* schema — right for the
bench's own runs, wrong as an import surface: nobody else emits it, and
freezing it as the public contract would couple external producers to
internal evolution. The interchange format is the neutral boundary:
external systems export *to* it, `wt import` reads *from* it, and the
mapping to native `Trace` stays private to the importer.

### Alignment with OpenTelemetry GenAI

The OTel GenAI semantic conventions (now in their own
[semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
repository, status **Development**) already define the two things an
interchange trace needs, and we adopt both verbatim rather than inventing
a dialect:

- **The message/parts model** — `gen_ai.input.messages` /
  `gen_ai.output.messages`: an ordered array of
  `{role, parts: [...]}` where parts are typed `text`,
  `tool_call` (`{id, name, arguments}`), or
  `tool_call_response` (`{id, response}`).
- **The attribute vocabulary** — `gen_ai.request.model`,
  `gen_ai.request.temperature` / `top_p`, `gen_ai.provider.name`,
  `gen_ai.tool.name` / `gen_ai.tool.call.id` /
  `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` (from
  `execute_tool` spans).

Because the semconv is Development-status, the envelope pins the mapping
(`"otel_genai_mapping"`) so a future attribute rename is a versioned
migration, not silent drift.

### Envelope (`*.wtin.json`)

```json
{
  "windtunnel_interchange": 1,
  "otel_genai_mapping": "semantic-conventions-genai@development-2026-06",
  "source": {
    "ref": "incident-2026-06-30-412",
    "system": "acme-observability",
    "captured_at": "2026-06-30T12:01:00Z"
  },
  "session": {
    "model": "gpt-example-1",
    "provider": "openai",
    "sampler": { "temperature": 0.7, "top_p": 0.95 },
    "started_at": "2026-06-30T12:00:00Z"
  },
  "messages": [
    { "role": "user", "parts": [ { "type": "text", "content": "Email on file for Bluewing?" } ] },
    { "role": "assistant", "parts": [ { "type": "tool_call", "id": "call_1", "name": "client_lookup", "arguments": { "query": "Bluewing" } } ] },
    { "role": "tool", "parts": [ { "type": "tool_call_response", "id": "call_1", "response": { "email": "ops@bluewing.example" } } ] },
    { "role": "assistant", "parts": [ { "type": "text", "content": "ops@bluewing.example" } ] }
  ],
  "tool_definitions": [
    { "name": "client_lookup", "description": "…", "input_schema": { "type": "object" } }
  ],
  "witnessed_calls": [
    { "tool_name": "client_lookup", "args": { "query": "Bluewing" }, "result": { "email": "ops@bluewing.example" } }
  ]
}
```

- `messages` is exactly the OTel parts model — an exporter sitting on
  OTel spans copies `gen_ai.input.messages` + `gen_ai.output.messages`
  through; an exporter sitting on any other logging pipeline writes the
  same shape directly. Only `session.model` and `messages` are required.
- `witnessed_calls` is optional server-side evidence (populated from
  `execute_tool` spans when the producer has them). When absent, the
  importer reconstructs call/result pairs from `tool_call` /
  `tool_call_response` parts — the agent's own account, which is exactly
  the evidence-quality distinction `Trace.mcp_calls` vs
  `turns[*].tool_calls` already draws, and the skeleton records which
  source it used.
- `tool_definitions` is optional; without it the universe file's
  `input_schema`s are stubbed and flagged for hand-editing.
- `source` is optional, opaque provenance in the producer's own
  vocabulary — an incident id, a ticket, a span/trace id. Wind Tunnel
  never interprets it; `wt import` stamps `source.ref` onto the scenario
  skeleton as an `origin:<ref>` tag and records the full object in
  `IMPORTED.md`, and the ledger carries it (§ Change 3), so a red row in
  CI traces back to the incident that seeded the scenario.
- The envelope carries message and tool content **verbatim** — producers
  must redact PII, credentials, and anything else that shouldn't leave
  their boundary before the file does; nothing downstream sanitizes it.

### `wt import`

```bash
wt import --trace prod_incident_412.wtin.json --out scenarios/imported/lookup_bluewing/
```

Emits a self-contained scenario directory:

- **`scenario.py`** — a skeleton `Scenario`: `prompt` (or `user_turns`
  for multi-turn) from the user messages; `must_call` pre-filled from
  the tools observed *and commented out* — trajectory expectations are
  an opt-in tightening, not a default, per the outcome-first stance;
  `requires_tool_use=True` when any tool was called.
- **`fixture.universe.json`** — Contract B, recordings drained from
  `witnessed_calls` (preferred) or reconstructed pairs, `on_miss:
  fail_call`.
- **`scorer.py`** — an `outcome_fn` stub: `target_facts` seeded from the
  final assistant text as a *suggestion block*, plus a TODO pointing at
  the scorer library. The importer never guesses what "correct" means;
  it hands the author the evidence and the vocabulary.
- **`IMPORTED.md`** — what was inferred, from which evidence source,
  and what needs human judgment.

The importer is a *skeleton generator*, deliberately: a trace shows what
the agent did, not what it should have done. Everything mechanical is
filled in; everything judgmental is a marked TODO.

---

## Change 2 — the outcome scorer library

`Scenario.outcome_fn: Callable[[Trace], LayerResult]` (shipped) is the
seam; what's missing is the library that makes reaching for it cheap.
New module `windtunnel/api/scorers.py`, pure stdlib, everything
returning or consuming the existing `LayerResult`:

- **Combinators** — `all_of(*fns)`, `any_of(*fns)`, each producing a
  joined diagnostic `detail` on failure (mirroring how
  `evaluate_trajectory` joins check failures).
- **State assertion** — `observation(source, path, predicate, label)`:
  reads `trace.observations[source]`, walks a dotted/indexed path,
  applies a predicate. This is the artifact-grading pattern
  `outcome_fn` was built for, packaged.
- **Rubric LLM-judge** — `llm_judge(rubric, generate_fn)`: BYO
  `GenerateFn` (the same bring-your-own-model contract `replay` uses);
  core ships the harness (prompt assembly from rubric + final answer +
  evidence, strict PASS/FAIL parse, parse-failure = layer failure with
  the raw response in `detail`), never a vendor client. The
  `LLMJudgeClassifier` stub in `triage/` shares this generate-fn seam.
- **Provenance checker** — `substantiated_by_tools(facts=None)`: the
  claims in the final answer must appear in some server-witnessed tool
  result (`trace.mcp_calls[*].result`, falling back to transcript
  `tool_results` with the evidence source named in `detail`, same
  fallback discipline as `evaluate_trajectory`). Rule-based first cut:
  reuse the existing fact/number matching machinery
  (`target_facts`-style substring groups, `NumberFact` word-boundary
  matching) pointed at tool results instead of scenario constants —
  "does the answer cite what the tools actually returned, or numbers
  from nowhere?" An LLM-judge variant composes on top via `llm_judge`.
- **Divergence predicate** — `no_divergence()` as a `Policy` factory
  (constraint layer, see Contract B), because "stayed inside the
  recording" is a property of the path, not the answer.

`outcome_fn` composition keeps the existing semantics: structural gates
(missing assistant turn, `requires_tool_use`) still run first; a raise
inside any scorer is caught and scored as failure.

## Change 3 — suite ownership and the run ledger

Two additions, both mechanism-only — Wind Tunnel ships the ledger
format, never the gating policy a consumer builds over it:

**Ownership.** `ScenarioPack` gains `owner: str | None = None` (free-form
— a team name, a GitHub handle, a codeowners path) and
`metadata: dict[str, str]` for anything else. Owner flows into the
ledger record and into report grouping. No semantics attached upstream.

**The ledger.** `wt run` appends one line per scenario-aggregate to
`<runs-dir>/ledger.ndjsonl` at the end of a sweep:

```json
{"ts": "2026-06-30T12:03:41Z", "scenario_id": "lookup_before_action",
 "pack": "tool_affordance", "owner": "team-ops", "label": "candidate",
 "model": "…", "quant": "…", "verdict": "PASS",
 "runs": 3, "layer_pass_rates": {"outcome": 1.0, "trajectory": 1.0, "constraint": 1.0, "robustness": 1.0},
 "run_ids": ["…"], "origin": "incident-2026-06-30-412",
 "git_sha": "3efbf94", "wt_version": "0.2.0"}
```

Append-only NDJSON, one record per (scenario, sweep): trivially
greppable, trivially windowed — pass-rate trends, flake detection, "how
long has this scenario been green" are each a few lines of `jq`, and by
design that analysis lives in the consumer, not here. `git_sha` is best-effort from the environment, null
outside a repo; `origin` is best-effort from the scenario's
`origin:<ref>` tag (stamped by `wt import`, see Contract A), null when
the scenario wasn't seeded from a trace. This fixes the current history gap where
`report.load_runs()` keeps only the latest run per cell: the per-run
trace+score files remain the deep evidence; the ledger is the queryable
index over time.

`wt report --format json` additionally emits the already-computed report
data structure (currently embedded as a JSON island in the HTML) as a
standalone artifact.

## Change 5 — CI ergonomics

Exit codes are already right (0 pass / 1 regression-or-error / 2 usage,
with the transport-only exemption and the consecutive-error circuit
breaker) — unchanged. Added:

- **`wt run --format junit --out results.xml`** — one `<testsuite>` per
  pack, one `<testcase>` per scenario-aggregate; failures carry the
  layer details and triage category when available. This is the "gate a
  PR like `go test`" surface.
- **`wt run --format json --out results.json`** — the same records the
  ledger gets, as a single sweep document, for anything that doesn't
  speak JUnit.
- **Selection** — beyond today's exact `--scenario` names: `--tag
  dim:recovery` (matches the existing tag convention), `--pack
  <name>`, `--owner <owner>`, and glob support in `--scenario`
  (`--scenario "lookup_*"`). Selection composes as AND across flags,
  OR within a repeated flag.

## Sequencing

Each lands as its own PR, in dependency order; 2, 3, 5 are independent
of each other and can go in any order once 1 is in review:

1. **Universe fixture** (Contract B): format module + `RecordedMCPServer`
   + divergence evidence + `freeze_universe()` helper + docs page.
2. **Scorer library**: `api/scorers.py` (builds on `outcome_fn`).
3. **Ownership + ledger**: `ScenarioPack.owner`, `ledger.ndjsonl`,
   `report --format json`.
4. **Interchange + `wt import`** (Contract A): needs 1 (emits universe
   files) and wants 2 (scorer stubs reference the library).
5. **CI output + selection**: junit/json writers, `--tag/--pack/--owner`.

## Explicit non-goals (the downstream boundary)

Wind Tunnel defines **formats and mechanisms**; anything that encodes a
particular organization's judgment stays downstream:

- Exporters from specific event logs, observability stacks, or products
  (they target Contract A; that's the whole point of it).
- Scenario content and fake-data corpora beyond the built-in dims'
  synthetic examples.
- Gating and deployment policy — what a green ledger history *means* is
  a consumer decision; upstream ships the ledger.
- LLM clients of any kind — judges and synthesizers are BYO callables,
  keeping the pure-stdlib core intact.

## Open questions

- **Canonical arg equality**: JSON canonicalization is defined here as
  sorted-keys + exact scalar equality. Float tolerance and
  case-insensitive strings are plausible per-tool knobs — deferred until
  a real universe file needs them.
- **`sequence` mode interaction with retries**: a retried call consumes
  the next recording. Acceptable for v1 (sequence mode is opt-in and
  rare); revisit if it bites.
- **Interchange multi-session traces**: v1 assumes one session per file.
  Multi-session bundles (`sessions: [...]`) are a compatible extension.
- **Semconv drift**: `otel_genai_mapping` pins the vocabulary; when the
  semconv stabilizes, cut interchange v2 with a migration note rather
  than mutating v1.
