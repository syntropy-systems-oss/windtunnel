# Wind Tunnel — agent notes

unittest for agents: scenarios score tool-using LLM agents on four layers
(outcome / trajectory / constraint / robustness). Layout: `windtunnel/api`
(scenario authoring), `windtunnel/spi` (runtime Protocols),
`windtunnel/scenarios/dim_*` (the dimension catalog), `tests/`.

## Commands

```bash
uv sync                                  # setup (.venv + dev group)
uv run pytest -m "not integration" -q    # unit suite — must always pass, no infra
uv run ruff check windtunnel/ tests/     # lint — keep at zero
uv run wt run --scenario <name> --runtime in_memory --runs 1   # smoke a scenario
```

## Invariants (test-enforced — do not weaken)

- Scenarios NEVER import `windtunnel.runtimes.*` (`tests/test_import_invariants.py`).
- Scenario `must_call`/`forbidden_calls` use canonical bare tool names
  (`client_lookup`); the evaluator matches platform decorations.
- The per-run pass/fail gate is the **outcome layer only**; trajectory,
  constraint, and robustness are recorded, not gating
  (`windtunnel/api/aggregate.py`).
- `evaluate_outcome` scores the actual last assistant turn, even if empty —
  never backfill from earlier turns (`windtunnel/api/evaluators.py`).
- Synthetic data stays fictional: fake orgs, `.example` domains.

## Gotchas

- The `in_memory` runtime never calls tools — `requires_tool_use` scenarios
  correctly FAIL under it. That's the gate working, not a bug.
- Entry points (`windtunnel.runtimes` and `windtunnel.scenario_packs` groups)
  refresh only on reinstall: after touching pyproject entry points, `uv sync`
  before trusting resolution.
- `runs/` is generated output (gitignored); traces pair with `.score.json`
  sidecars written by `wt run`/`wt replay`.

Integrating Wind Tunnel into another repo? Use
[docs/agent-quickstart.md](docs/agent-quickstart.md).
