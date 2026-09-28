---
name: windtunnel
description: "Bench tool-using LLM agents with the wt CLI, scenario packs, trace import/interchange, Contract C inject endpoints, reset isolation, and recorded tool universes."
---
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
{{REFERENCE_INDEX}}
<!-- END GENERATED REFERENCE INDEX -->

<!-- BEGIN AGENTS_MD_TEMPLATE -->
# Wind Tunnel Agent Index

Wind Tunnel is unittest for tool-using LLM agents: scenarios gate declared
behavior expectations and verify experiment integrity from reproducible traces.

Installed skill: `wt skill path`

## Highest-Importance References

{{TOP_REFERENCES}}

## Commands

```bash
uv run wt skill path
uv run wt validate --strict <file.wtin.json>
uv run wt doctor --runtime <runtime>
uv run wt run --runtime in_memory --scenario <name> --runs 1
uv run pytest -q
```
<!-- END AGENTS_MD_TEMPLATE -->
