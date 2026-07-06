---
name: windtunnel
description: Bench tool-using LLM agents with the wt CLI, scenario packs, trace import/interchange,
  Contract C inject endpoints, reset isolation, and recorded tool universes.
---
<!-- GENERATED from agents/skill-template.md + docs/ at fcdb4c76f791 — do not edit; edit docs/ or agents/skill-template.md. -->
# Wind Tunnel

Wind Tunnel is unittest for agents: a reliability bench for tool-using LLM
agents that scores outcomes, trajectories, constraints, and robustness from
diffable traces.

Use this skill when adding or debugging Wind Tunnel in a repo, authoring
scenarios, wiring runtimes, importing traces, validating interchange envelopes,
serving recorded tool universes, or bringing up Contract C inject endpoints.

## Operating Rules

- Treat `docs/` as the source of truth. The files in `references/` are generated
  copies for agent execution context.
- Prefer Contract C (`http_inject`) when an agent process can expose
  `/wt/inject` and `/wt/reset`.
- Validate imported traces before import: `uv run wt validate --strict <file.wtin.json>`.
- Smoke scenario wiring with `uv run wt run --runtime in_memory --scenario <name> --runs 1`.
- Prove runtime reset isolation with `uv run wt doctor --runtime <runtime>`.
- Run the unit suite before changing bench semantics: `uv run pytest -q`.
- Read `references/agents/anti-patterns.md` before building an importer,
  endpoint, or runtime driver.

## Generated Reference Index

<!-- BEGIN GENERATED REFERENCE INDEX -->
- `references/agent-quickstart.md` - Self-contained guide for coding agents to add Wind Tunnel scenarios, runtime wiring, and run commands to a project.
- `references/agents/anti-patterns.md` - Agent-only list of Wind Tunnel integration mistakes that produce misleading benches or hard validation failures.
- `references/agents/integration-checklist.md` - Agent-only shortest path for getting a project benched by Wind Tunnel through Contract C and one authored scenario.
- `references/architecture.md` - Architecture overview of Wind Tunnel's API/SPI split, runner data path, scoring layers, perturbations, and CLI surfaces.
- `references/cli-reference.md` - Generated reference for wt CLI subcommands, usage, options, and exit-code semantics.
- `references/design/0001-trace-reseeding.md` - Design spine for trace re-seeding, Contract A interchange, Contract B universes, import, scorer, ledger, and CI ergonomics.
- `references/design/0002-inject-protocol.md` - Design specification for Contract C inject protocol, its reset route, error handling, built-in runtime, and canary.
- `references/failure-taxonomy.md` - Catalog of Wind Tunnel failure categories, distinguishing signals, and fix vectors for triage.
- `references/getting-started.md` - Step-by-step guide to install Wind Tunnel, run and report scenarios, gate CI, and triage failures.
- `references/importing-a-trace.md` - Workflow for validating a Contract A trace, importing a failing scenario skeleton, and authoring the regression gate.
- `references/index.md` - Overview of Wind Tunnel's four-layer agent reliability bench, import workflow, CLI, and starting points.
- `references/recording-a-universe.md` - Reference for recorded tool-universe fixtures, matching rules, divergence policies, and RecordedMCPServer usage.
- `references/writing-a-classifier.md` - Guide to implementing failure classifiers and testing them against Wind Tunnel's taxonomy fixtures.
- `references/writing-a-runtime.md` - Guide to implementing Wind Tunnel runtime protocols or Contract C endpoints with reset isolation and tool-call evidence.
- `references/writing-a-scenario.md` - Reference for authoring backend-agnostic Scenario objects, scoring fields, perturbations, dimensions, and scenario packs.
- `references/writing-an-optimizer.md` - Design guide for prompt optimizer implementations using Wind Tunnel failure classifications and fix vectors.
<!-- END GENERATED REFERENCE INDEX -->
