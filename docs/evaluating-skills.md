---
description: "How to evaluate whether generated agent skills improve Wind Tunnel task performance."
---
# Evaluating Skills

Wind Tunnel can evaluate its own generated agent skill by running the same
Terminus scenarios across three workspace-documentation arms:

- `skill`: a root `AGENTS.md` plus the full generated skill under
  `.agents/skills/windtunnel/`.
- `agents-md`: only the root `AGENTS.md` index.
- `bare`: no Wind Tunnel documentation.

The task prompts are identical across arms and deliberately do not mention the
available documentation. That makes documentation discovery part of the measured
behavior, instead of assuming the agent will consult a skill just because it is
present.

The example pack lives under `examples/skill-eval/`. It uses the Terminus
workspace-template mechanism: `prepare.py` materializes three template
directories, then each run selects an arm with
`WT_TERMINUS_WORKSPACE_TEMPLATE=examples/skill-eval/templates/<arm>` and labels
the trace with `--label skill`, `--label agents-md`, or `--label bare`.
Terminus runs in docker isolation by default, so `prepare.py` also writes a
template bootstrap hook that rebuilds the workspace `.venv` inside the Linux
container and points it at the read-only repo mount. Explicit host mode still
uses the host `.venv` that `prepare.py` creates.

Scoring is deterministic. A `WorkspaceCheckProbe` runs scenario-specific
verification commands in the agent workspace after the terminal agent finishes
and freezes the command results into `trace.observations`. Scenario outcome
functions read only those observations, so saved traces can be re-scored later
with `wt rescore` without re-running the agent.
When the Terminus runtime writes docker metadata into the workspace, the probe
runs those verification commands through `docker exec` in the live container;
otherwise it runs them on the host workspace for host-mode compatibility.

The pack also records consultation as trajectory evidence. A custom trajectory
check scans terminal commands for reads of `AGENTS.md` or `.agents/skills/` and
adds `docs_read=...` to the trajectory detail. This is an invocation-reliability
signal, not a gate: a run can pass or fail the task independently of whether it
read the docs.

Run instructions and exact commands are in
`examples/skill-eval/README.md`.

## First live results

The matrix has been run at least once for real — the day the pack shipped,
against a local qwen3.6:35b via an OpenAI-compatible endpoint. The table and
observations live in
[`examples/skill-eval/README.md`](https://github.com/syntropy-systems-oss/windtunnel/tree/main/examples/skill-eval)
(single runs, so read them as observations, not conclusions). The headline:
outcomes mostly tied while *cost* diverged — and the one catastrophic
divergence was an agent that completed the work but, without the reference
explaining that imported scenarios are deliberately failing stubs, could
never convince itself it was done. Documentation bought termination
knowledge more than task knowledge.
