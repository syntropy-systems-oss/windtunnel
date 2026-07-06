---
description: "Agent-only shortest path for getting a project benched by Wind Tunnel through Contract C and one authored scenario."
agent:
  only: true
---
# Integration checklist

## 1. Implement Contract C

Expose exactly two routes from the agent process:

- `POST /wt/inject` accepts `{ "wt_inject": 1, "session_id": "...", "text": "...", "timeout_s": 120 }`.
- `POST /wt/reset` accepts `{ "wt_inject": 1 }` and returns only after all bench-visible state is gone.

Validation command:

```bash
WT_INJECT_URL=http://127.0.0.1:8647 uv run wt doctor --runtime http_inject
```

## 2. Prove reset isolation

Do not treat a 200 from reset as proof by itself. Run the canary against the
same endpoint the bench will use:

```bash
WT_INJECT_URL=http://127.0.0.1:8647 uv run wt doctor --runtime http_inject
```

For CI without a live model, write a pytest that calls `run_reset_canary(...,
probe_recall=False, state_probe=...)`.

## 3. Author one scenario

Create a backend-agnostic `Scenario` with fictional data, canonical bare tool
names, and no imports from `windtunnel.runtimes.*`.

Validation command:

```bash
uv run wt run --runtime in_memory --scenario <scenario-name> --runs 1
```

A tool-gated scenario should fail under `in_memory`; the useful signal is that
it discovers and scores cleanly.

## 4. Run against the endpoint

Use the built-in runtime once Contract C and reset isolation are in place:

```bash
WT_INJECT_URL=http://127.0.0.1:8647 uv run wt run --runtime http_inject --scenario <scenario-name> --runs 3 --label baseline
uv run wt report --runs runs/ --format html --out report.html
```

The project is benched when the authored scenario runs through the real
endpoint, the trace shows the expected tool-call evidence, and the outcome gate
passes for the configured run count.
