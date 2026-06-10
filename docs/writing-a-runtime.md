# Writing a runtime

A **runtime** is how Wind Tunnel talks to your agent platform. Implement two
small Protocols — `AgentRuntime` and `AgentHandle` — and every scenario,
perturbation, evaluator, and report in the framework works against your
platform unchanged.

The contracts live in `windtunnel/spi/agent_runtime.py`. They are
`typing.Protocol`s: no base class to inherit, no framework registration —
structural typing. If your class has the methods, it's a runtime.

## The contract at a glance

```python
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, Message, Response


class MyRuntime:
    def provision(self, config: AgentConfig, mcps: list | None = None) -> AgentHandle:
        """Stand up a live agent wired to the given (already-started) MCP handles."""


class MyHandle:
    def send(self, messages: list[Message], session_id: str) -> Response:
        """Drive one turn. Returns a flat {"content", "tool_calls"} dict
        (or an OpenAI-shaped chat completion — both accepted, see below)."""

    def reset_state(self) -> None:
        """Wipe cross-run state. Cheap, called before every run. Idempotent."""

    def teardown(self) -> None:
        """Release everything. Called once per batch. Idempotent, must not raise."""
```

A conformance suite (`tests/test_runtime_conformance.py`) runs the same
scenarios through the in-memory reference runtime; point it at yours to
verify the contract before debugging real failures.

## `provision(config, mcps)` — once per batch

Called once before a batch of runs. Receives:

- **`config: AgentConfig`** — the agent's identity and knobs:

  | Field | Meaning |
  |---|---|
  | `agent_id`, `variant_id` | identity; lands in every `Trace` |
  | `system_prompt` | override the platform's default system prompt |
  | `persona_doc` | path to an operating-notes doc (e.g. AGENTS.md) |
  | `skills` | optional skill/persona `.md` files |
  | `mcp_servers` | declarative MCP references (`MCPSpec(name, url)`) |
  | `model` | `ModelSpec(name, quant)` — which model to run |
  | `sampling` | `SamplingConfig(temperature, top_p, tool_choice, max_tokens)` |

  Your runtime interprets these for your platform. Unsupported fields should
  raise loudly, not silently no-op — a bench that silently ignores
  `temperature=0` produces lies, not results.

- **`mcps`** — **already-started** `MCPHandle` objects (the runner starts
  them before calling you). Your job is to make their `handle.url` reachable
  from wherever your agent runs and register their tools however your
  platform mounts tools. Two recurring gotchas from the reference drivers:
  - *Network translation:* the mock runs on the host; your agent runs in a
    container. `localhost` in `handle.url` must become
    `host.docker.internal` (or your network's equivalent) before
    registration.
  - *Tool-mount timing:* platforms that read tool registrations at startup
    need a restart (or explicit re-sync) after registration — and anything
    holding a session against the restarted service may need its own
    restart. Gate on the tools *actually being callable* (probe with a real
    request), not on the service being up.

Return an `AgentHandle`. Provisioning is the expensive step — containers,
auth, registration — which is exactly why it's separated from `reset_state()`.

## `send(messages, session_id)` — one turn

Drive one turn of the agent loop and return a response dict in either of
two accepted shapes (the runner normalizes both):

- **flat message** — the forward-looking minimal contract:
  `{"content": "...", "tool_calls": [...]}` (a `{"message": {...}}` wrapper
  is also tolerated). If you're writing a new runtime, return this.
- **OpenAI chat-completions** — `choices[0].message` with `role`, `content`,
  `tool_calls`, `finish_reason`. Kept for OpenAI-compat so a runtime can
  hand back an upstream completions payload untouched.

The two rules that matter:

1. **Surface intermediate tool calls.** If your platform runs the whole
   agent loop server-side and only hands back the final message, the
   trajectory layer (`must_call`) silently fails for every tool-using
   scenario. Use whatever streaming/events API exposes the intermediate
   steps and reconstruct the full picture. (This single mistake cost the
   reference implementation a rewrite — it is the most common way to build a
   runtime that looks fine and scores garbage.)
2. **Don't normalize tool-call shapes.** Record `tool_calls` exactly as your
   platform emits them (OpenAI wire shape, flattened shape, whatever). The
   `Trace` preserves them faithfully; evaluators handle both. Normalizing at
   the runtime layer destroys evidence — drift in tool-call shape is itself
   a failure mode worth catching.

Multi-turn scenarios call `send()` repeatedly with the same `session_id`;
your platform's session affinity must honor it.

Also worth capturing if your platform exposes it: the **rendered prompt**
(the exact chat-template output per turn). `Turn.rendered_prompt` and its
auto-computed hash let you diff what the model *actually saw* across runs —
the fastest way to catch template regressions.

## `reset_state()` vs `teardown()`

Two lifecycle levels, deliberately distinct:

- **`reset_state()`** — between runs. Cheap and frequent. Wipe sessions,
  conversation history, memory stores, tool-call logs — anything that lets
  run N leak into run N+1. **Treat a failed wipe as fatal.** Cross-run
  contamination is the classic source of false passes (the reference bench
  was once burned by a full-text-search index that survived the wipe and
  answered scenario N+1's question from scenario N's data — wipe the
  *indexes* too).
- **`teardown()`** — end of batch. Expensive and rare. Stop containers, kill
  tunnels, revoke grants, drop registrations. Idempotent and non-raising:
  it runs in `finally` paths.

## Auth and identity

The runner doesn't know about auth — credentials are the runtime's problem.
The reference pattern: a dev-mode identity (`Bearer dev:<agent_id>`) behind
a flag in the platform, plus a stub for any external identity provider, so
the bench is hermetic. Whatever you do, keep real credentials in env vars,
never in runtime code or scenario definitions.

## A minimal skeleton

```python
import httpx

from windtunnel.spi.agent_runtime import AgentConfig


class HttpAgentHandle:
    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout,
                                    headers={"Authorization": f"Bearer {api_key}"})

    def send(self, messages, session_id):
        run = self._client.post("/v1/runs", json={
            "input": messages, "session_id": session_id,
        }).json()
        # Drain the events stream; reconstruct an OpenAI-shaped response,
        # collecting intermediate tool_calls — not just the final message.
        return self._drain_events(run["run_id"])

    def reset_state(self):
        resp = self._client.post("/admin/reset", json={"scope": "all"})
        resp.raise_for_status()   # a failed wipe must be fatal

    def teardown(self):
        try:
            self._client.close()
        except Exception:
            pass                  # teardown must not raise


class HttpRuntime:
    def __init__(self, base_url: str, api_key: str):
        self._base_url, self._api_key = base_url, api_key

    def provision(self, config: AgentConfig, mcps=None) -> HttpAgentHandle:
        for handle in (mcps or []):
            self._register_tools(handle.url)   # incl. localhost translation
        return HttpAgentHandle(self._base_url, self._api_key)
```

## Checklist before trusting your runtime

- [ ] Conformance tests pass (`test_runtime_conformance.py` pointed at your runtime).
- [ ] A `must_call` scenario passes — proves intermediate tool calls surface.
- [ ] Two scenarios run back-to-back with a deliberately stateful first
      scenario — proves `reset_state()` actually isolates runs.
- [ ] `sampling.temperature=0` twice produces (near-)identical traces —
      proves the sampling config actually reaches the model.
- [ ] Kill the bench mid-run; rerun — proves `teardown()`/`provision()`
      recover from dirty state.
