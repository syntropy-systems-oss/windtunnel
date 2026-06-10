# Wind Tunnel

**unittest for agents.** A reliability bench for tool-using LLM agents —
structured, diff-able, and runnable in CI.

You don't fly a new airframe straight into a storm; you put it in a wind
tunnel. Wind Tunnel is the same idea for agents: a controlled replica of
production conditions where you watch how the agent behaves *before* you
deploy it.

```bash
pip install windtunnel-bench   # installs `import windtunnel` + the `wt` CLI
wt run --runtime in_memory --scenario lookup_before_action
```

## What it does

Conventional evals score the final answer. Wind Tunnel scores the **whole
flight**, on four independent layers:

| Layer | Question |
|---|---|
| **outcome** | Was the user-visible answer right? |
| **trajectory** | Were the right tools called, in the right order, none forbidden? |
| **constraint** | Did named policy predicates over the trace hold? |
| **robustness** | Were the declared perturbations actually applied? |

And it doesn't take the transcript's word for anything: when a logging mock
is in play, tool traffic is recorded at the tool server itself, so trajectory
scoring asserts what the agent **actually did** — not what it claimed to do.

## Where to start

- **[Getting started](getting-started.md)** — install, first scenario, first report.
- **[Writing a scenario](writing-a-scenario.md)** — the `Scenario` schema, field by field.
- **[Writing a runtime](writing-a-runtime.md)** — wire Wind Tunnel to your agent platform (four small methods).
- **[Architecture](architecture.md)** — the API/SPI split and the four-layer scoring model.
- **[Failure taxonomy](failure-taxonomy.md)** — what the triage classifier can tell you.
- **[Agent quickstart](agent-quickstart.md)** — using a coding agent? Point it at this one page.
