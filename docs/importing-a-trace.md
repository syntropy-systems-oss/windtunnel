# Importing a trace

Wind Tunnel's headline workflow is turning a real agent trace into a hermetic
regression test:

```text
production trace
  -> Contract A *.wtin.json envelope
  -> wt validate
  -> wt import
  -> authored Scenario + universe fixture
  -> wt run
```

The import path is intentionally split between producer work and scenario-author
work. A trace shows what happened. A regression test still needs a human to say
what should count as correct.

## 1. Export a Contract A envelope

Wind Tunnel imports `*.wtin.json` files, not platform-native logs. The envelope
is a neutral interchange shape aligned with OpenTelemetry GenAI messages:

- `messages`: ordered user, assistant, and tool parts.
- `session`: model and sampling metadata.
- `tool_definitions`: optional schemas for tools offered to the agent.
- `witnessed_calls`: optional call/result evidence from the tool boundary.
- `source`: optional provenance such as an incident id.

The exact contract is recorded in
[design 0001](design/0001-trace-reseeding.md#contract-a-the-trace-interchange-format).
Producers must redact sensitive values before the envelope leaves their system;
Wind Tunnel replays the content it is given.

## 2. Validate before importing

Run validation on every envelope before generating a scenario:

```bash
wt validate --strict incident-412.wtin.json
```

`wt validate` uses the same parser as `wt import`. It also lints schema-valid
envelopes for suspicious shapes, such as truncated tool values or a
`tool_call_response` without a matching `tool_call`.

Without `--strict`, lint warnings print as `WARN` lines but do not fail the
command. With `--strict`, warnings exit `1`, which is the recommended producer
CI mode.

## 3. Generate the skeleton

```bash
wt import --trace incident-412.wtin.json --out scenarios/imported/incident_412/
```

The output directory contains four files:

| File | Purpose |
|---|---|
| `fixture.universe.json` | A recorded tool-universe fixture built from `witnessed_calls` when available, otherwise reconstructed transcript pairs. |
| `scenario.py` | A `Scenario` skeleton with prompt/user turns, `requires_tool_use`, provenance tags, and commented-out `must_call` suggestions. |
| `scorer.py` | An `outcome_fn` stub plus suggested fact candidates copied from the final assistant text. |
| `IMPORTED.md` | A review note describing inferred evidence, provenance, stubbed schemas, and remaining TODOs. |

`wt import` refuses to write into a non-empty directory unless `--force` is
passed.

## 4. Author the gate

The generated scenario fails on purpose. Its placeholder `target_facts` cannot
match, and `scorer.py` returns a failing `LayerResult` until you replace it.

Review the generated files:

- Decide the outcome criterion. Use reviewed `target_facts` for simple final
  answers, or wire `outcome_fn` from `scorer.py` for artifact/state/provenance
  checks.
- Review `SUGGESTED_TARGET_FACTS`. They are copied from what the agent said, not
  from ground truth.
- Uncomment `must_call` only when that trajectory is part of correctness.
- Replace stubbed tool schemas in `fixture.universe.json` when the envelope did
  not include `tool_definitions`.
- Keep or edit the `origin:<ref>` tag. It flows into `ledger.ndjsonl`, making a
  later red row traceable to the imported incident.

Useful scorer helpers are covered in
[writing a scenario](writing-a-scenario.md#the-scenario-schema-apiscenariopy):
`all_of`, `observation`, `substantiated_by_tools`, `llm_judge`, and
`no_divergence`.

## 5. Make it discoverable

`wt import` writes a self-contained directory, but the CLI only discovers
installed `ScenarioPack`s. Before running it through `wt run`, either:

- add the generated `scenario` to an existing pack in your repo, or
- create a new pack that imports `SCENARIOS` from `scenario.py`, binds
  `fixture.universe.json` with `RecordedMCPServer`, and registers the pack under
  the `windtunnel.scenario_packs` entry-point group.

The pack shape is documented in
[writing a scenario](writing-a-scenario.md#shipping-a-scenario-pack). During
authoring, you can also call `run_scenario()` directly from a small local script.

## 6. Run the regression

Once the scenario is authored and packaged:

```bash
wt run --scenario incident_412 --runtime <your-runtime> --runs 3 --label regression
wt report --runs runs/ --format html --out report.html
```

If the generated fixture is used as the scenario's mock MCP server, reruns are
hermetic: the agent sees the recorded tool world instead of live upstreams. Any
call outside the recording is captured as universe divergence evidence, and
`no_divergence()` can make that a constraint-layer signal.

## Related pages

- [Recording a tool universe](recording-a-universe.md): the fixture format that
  `wt import` emits.
- [CLI reference](cli-reference.md): command options for `wt import` and
  `wt validate`.
- [Design 0001](design/0001-trace-reseeding.md): the normative import and
  universe contracts.
