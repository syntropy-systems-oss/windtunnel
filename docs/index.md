# Wind Tunnel

**unittest for agents.** A reliability bench for tool-using LLM agents —
structured, diff-able, and runnable in CI.

You don't fly a new airframe straight into a storm; you put it in a wind
tunnel. Wind Tunnel is the same idea for agents: a controlled replica of
production conditions where you watch how the agent behaves *before* you
deploy it.

```bash
pip install windtunnel-bench   # installs `import windtunnel` + the `wt` CLI
wt --help
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

## The workflow

Wind Tunnel's import path turns an observed agent failure into a regression
test:

```bash
wt validate --strict incident.wtin.json
wt import --trace incident.wtin.json --out scenarios/imported/incident/
# review the generated scenario.py, scorer.py, and fixture.universe.json
wt run --runtime <your-runtime> --scenario incident --runs 3
```

The importer generates a skeleton, not a green test. A human still authors the
outcome gate, because a trace proves what happened, not what should pass. See
[importing a trace](importing-a-trace.md) for the full path.

## CLI at a glance

| Command | Use it to |
|---|---|
| `wt run` | Execute scenarios against `in_memory`, `http_inject`, or a runtime plugin. |
| `wt report` | Render saved runs as HTML, Markdown, or JSON. |
| `wt compare` | Diff variant labels such as `baseline` and `candidate`. |
| `wt replay` | Re-run a saved trace's last user turn against a runtime. |
| `wt doctor` | Run the reset-isolation canary against a live runtime. |
| `wt import` | Generate a scenario skeleton from a `*.wtin.json` trace envelope. |
| `wt validate` | Validate and lint interchange envelopes, with `--strict` for producer CI. |
| `wt triage` | Classify failed saved runs with the shipped rule-based classifier. |

See the [CLI reference](cli-reference.md) for options and exit codes.

## Where to start

- **[Getting started](getting-started.md)** — install, first scenario, first report.
- **[Writing a scenario](writing-a-scenario.md)** — the `Scenario` schema, field by field.
- **[Writing a runtime](writing-a-runtime.md)** — wire Wind Tunnel to your agent platform (four small methods).
- **[Importing a trace](importing-a-trace.md)** — turn a Contract A trace into an authored regression test.
- **[Recording a universe](recording-a-universe.md)** — serve recorded tool calls as a hermetic mock upstream.
- **[CLI reference](cli-reference.md)** — all eight `wt` commands in Wind Tunnel 0.5.0.
- **[Architecture](architecture.md)** — the API/SPI split and the four-layer scoring model.
- **[Failure taxonomy](failure-taxonomy.md)** — what the triage classifier can tell you.
- **[Agent quickstart](agent-quickstart.md)** — using a coding agent? Point it at this one page.
