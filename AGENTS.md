<!-- GENERATED from agents/skill-template.md + docs/ at 291e8668126d — do not edit; edit docs/ or agents/skill-template.md. -->
# Wind Tunnel Agent Index

Wind Tunnel is unittest for tool-using LLM agents: scenarios score outcome,
trajectory, constraint, and robustness layers from reproducible traces.

Installed skill: `wt skill path`

## Highest-Importance References

- `references/agent-quickstart.md` - Self-contained guide for coding agents to add Wind Tunnel scenarios, runtime wiring, and run commands to a project.
- `references/agents/integration-checklist.md` - Agent-only shortest path for getting a project benched by Wind Tunnel through Contract C and one authored scenario.
- `references/agents/anti-patterns.md` - Agent-only list of Wind Tunnel integration mistakes that produce misleading benches or hard validation failures.
- `references/writing-a-runtime.md` - Guide to implementing Wind Tunnel runtime protocols or Contract C endpoints with reset isolation and tool-call evidence.
- `references/design/0002-inject-protocol.md` - Design specification for Contract C inject protocol, its reset route, optional surface-introspection route, error handling, built-in runtime, and canary.
- `references/importing-a-trace.md` - Workflow for validating a Contract A trace, importing a failing scenario skeleton, and authoring the regression gate.

## Commands

```bash
uv run wt skill path
uv run wt validate --strict <file.wtin.json>
uv run wt doctor --runtime <runtime>
uv run wt run --runtime in_memory --scenario <name> --runs 1
uv run pytest -q
```
