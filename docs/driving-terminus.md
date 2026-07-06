---
description: "Guide to driving Harbor Terminus-2 from Wind Tunnel as a terminal-agent runtime."
---
# Driving Terminus-2

The `terminus` runtime lets Wind Tunnel bench Terminus-2 — the reference
terminal agent that ships inside [Harbor](https://harborframework.com/),
the Terminal-Bench team's evaluation framework — as a neutral coding and
shell agent. Harbor and Terminus-2 are not separate products: Harbor is the
orchestration framework, Terminus-2 is one agent implementation it hosts.
This runtime imports Terminus-2 from the `harbor` package and drives it
directly, while Wind Tunnel owns the scenario, fixture workspace, scoring
rules, and trajectory checks; Terminus-2 gets only its normal terminal loop.

Wind Tunnel deliberately does **not** wrap Harbor's own trial orchestration:
Harbor's `Trial` machinery bundles task packaging, environment lifecycle,
verifiers, and result persistence — jobs Wind Tunnel already owns. Harbor
rewards and verifiers are therefore unused; Wind Tunnel scores the final
workspace state and the recorded trace. (A Trial-wrapping driver would be
the right shape for a different goal — benching Harbor's *other* agent
adapters or its cloud-sandbox environments — and can exist alongside this
one if that demand materializes.)

## Install

Terminus-2 support is optional because Harbor requires Python 3.12 or newer,
while Wind Tunnel core supports Python 3.11.

```bash
pip install "windtunnel-bench[terminus]"
```

You also need Docker available for bench boxes that run container-backed
fixtures, plus `tmux` on the machine running the driver.

## Configuration

The runtime is configured only through environment variables:

| Variable | Required | Meaning |
|---|---:|---|
| `WT_TERMINUS_MODEL` | yes | LiteLLM model string, such as `openai/<model>`. |
| `WT_TERMINUS_API_BASE` | no | Base URL for an OpenAI-compatible endpoint. |
| `WT_TERMINUS_MAX_TURNS` | no | Maximum Terminus-2 episodes per Wind Tunnel turn. Defaults to `80`. |
| `WT_TERMINUS_WORKSPACE_TEMPLATE` | no | Directory copied into a fresh workspace before every scenario run. |
| `WT_TERMINUS_LOGS_DIR` | no | Parent directory for driver workspaces and Terminus logs. Defaults under the system temp directory. |

Example:

```bash
export WT_TERMINUS_MODEL="openai/<model>"
export WT_TERMINUS_API_BASE="http://localhost:11434/v1"
export WT_TERMINUS_WORKSPACE_TEMPLATE="/path/to/fixture-repo"
export WT_TERMINUS_MAX_TURNS=80

wt run --runtime terminus --scenario <scenario-name> --runs 1
```

If `WT_TERMINUS_WORKSPACE_TEMPLATE` is unset, the agent starts in an empty
workspace. `reset_state()` always wipes the prior workspace and copies the
template again, including before the first run.

## Trajectory Evidence

Terminus-2 writes Harbor ATIF trajectories. In that format, parsed terminal
actions appear as `bash_command` tool calls with `arguments.keystrokes`.

Wind Tunnel maps those to OpenAI-shaped tool calls:

```json
{
  "type": "function",
  "function": {
    "name": "terminal",
    "arguments": "{\"command\":\"pytest -q\\n\"}"
  }
}
```

Use `must_call: ["terminal"]` when a scenario only needs to prove the terminal
was used. Use outcome scoring or a workspace probe when the exact command text
matters, because the `command` value is raw keystrokes rather than a shell AST.

## Caveats

The runtime treats one Wind Tunnel `send()` as one coarse Terminus-2 task run.
Multi-turn scenarios can call `send()` repeatedly, but each call is a new
Terminus-2 task over the current workspace rather than a native chat turn.

MCP mocks are not mounted into Terminus-2. The agent has exactly one tool, its
terminal, so scenarios for this runtime should assert outcomes from files,
commands, logs, or other workspace-observable state.
