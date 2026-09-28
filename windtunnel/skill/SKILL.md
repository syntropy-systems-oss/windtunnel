---
name: windtunnel
description: Bench tool-using LLM agents with the wt CLI, scenario packs, trace import/interchange,
  Contract C inject endpoints, reset isolation, and recorded tool universes.
---
<!-- GENERATED from agents/skill-template.md + docs/ at 22241df66718 — do not edit; edit docs/ or agents/skill-template.md. -->
# Wind Tunnel

Wind Tunnel is unittest for agents: a reliability bench for tool-using LLM
agents that gates outcomes, trajectories, and constraints, verifies experiment
integrity, and measures robustness under perturbation from diffable traces.

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
- Iterate small to large. After a failing sweep, probe the broken step
  (`wt run --from-trace <trace> --question ... --expect ...`), then run the one
  scenario on its own, and only then the whole pack. `wt run` enforces this:
  a regression (several scenarios) is refused unless a focused run has passed
  on the current artifact since the last regression, and each selected
  scenario whose latest regression run failed has passed one of its own. To
  see what is working, read `wt results`; never run the whole pack to find out, and every sweep into a runs directory with
  history must declare `--question` and `--expect`. There is no bypass flag.
  Say what each outcome will change (`--if-pass`, `--if-fail`), record what
  it did (`wt review <sweep> --decision ...`), and check which tiers pay for
  themselves with `wt results --ladder`.
- Read `references/agents/anti-patterns.md` before building an importer,
  endpoint, or runtime driver.

## Generated Reference Index

<!-- BEGIN GENERATED REFERENCE INDEX -->
- `references/agent-quickstart.md` - Self-contained guide for coding agents to add Wind Tunnel scenarios, runtime wiring, and run commands to a project.
- `references/agents/anti-patterns.md` - Agent-only list of Wind Tunnel integration mistakes that produce misleading benches or hard validation failures.
- `references/agents/integration-checklist.md` - Agent-only shortest path for getting a project benched by Wind Tunnel through Contract C and one authored scenario.
- `references/architecture.md` - Architecture overview of Wind Tunnel's API/SPI split, runner data path, behavior gates, experiment integrity, perturbations, and CLI surfaces.
- `references/cli-reference.md` - Generated reference for wt CLI subcommands, usage, options, and exit-code semantics.
- `references/design/0001-trace-reseeding.md` - Design spine for trace re-seeding, Contract A interchange, Contract B universes, import, scorer, ledger, and CI ergonomics.
- `references/design/0002-inject-protocol.md` - Design specification for Contract C inject protocol, its reset route, optional surface-introspection route, error handling, built-in runtime, and canary.
- `references/design/0003-hook-system.md` - Design specification for lifecycle hooks: the windtunnel.hooks plugin SPI, per-point ordering contracts, the scoped hook context, sidecar artifacts, and the debrief reference hook.
- `references/design/0004-reference-selftest.md` - Design specification for live golden/poison scenario self-tests, the optional runtime inference-substitution capability, isolation, probe timing, and CI verdicts.
- `references/design/0005-experiment-ladder.md` - Design specification for the experiment ladder: derived run tiers, wall-clock budgets, declared questions, artifact fingerprints, the regression evidence gate, prefix replay probes, tier-ordered runtime queueing, per-tier model policy, recorded experiment cost and value, cache-aware pricing, and the prompt-cache-miss check.
- `references/driving-terminus.md` - Guide to driving Harbor Terminus-2 from Wind Tunnel as a terminal-agent runtime.
- `references/evaluating-skills.md` - How to evaluate whether generated agent skills improve Wind Tunnel task performance.
- `references/failure-taxonomy.md` - Catalog of Wind Tunnel failure categories, distinguishing signals, and fix vectors for triage.
- `references/getting-started.md` - Step-by-step guide to install Wind Tunnel, run and report scenarios, gate CI, and triage failures.
- `references/importing-a-trace.md` - Workflow for validating a Contract A trace, importing a failing scenario skeleton, and authoring the regression gate.
- `references/index.md` - Overview of Wind Tunnel's agent reliability gates, experiment integrity, import workflow, CLI, and starting points.
- `references/iterating.md` - Tight iteration loop for people and coding agents: follow sweeps live, tabulate results and metrics per label, compare labels, and rescore saved traces.
- `references/migrating-to-0.9.md` - Migration guide for Wind Tunnel 0.9 scoring gates, experiment integrity, failure risk, and persisted artifact versions.
- `references/recording-a-universe.md` - Reference for recorded tool-universe fixtures, matching rules, divergence policies, and RecordedMCPServer usage.
- `references/surface-goldens.md` - Task guide for capturing prompt-surface goldens and gating steering changes with wt surface.
- `references/using-hooks.md` - Task guide for enabling Wind Tunnel lifecycle hooks, reading debrief artifacts, and registering custom hooks.
- `references/writing-a-classifier.md` - Guide to implementing failure classifiers and testing them against Wind Tunnel's taxonomy fixtures.
- `references/writing-a-runtime.md` - Guide to implementing Wind Tunnel runtime protocols or Contract C endpoints with reset isolation and tool-call evidence.
- `references/writing-a-scenario.md` - Reference for authoring backend-agnostic Scenario objects, scoring fields, perturbations, dimensions, and scenario packs.
- `references/writing-an-optimizer.md` - Design guide for prompt optimizer implementations using Wind Tunnel failure classifications and fix vectors.
<!-- END GENERATED REFERENCE INDEX -->
