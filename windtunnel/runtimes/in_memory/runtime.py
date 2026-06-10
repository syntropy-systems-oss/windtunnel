"""InMemoryRuntime — the zero-infrastructure AgentRuntime.

Records all send() calls in-process and replies from a script. Does NOT
make network calls. This is the fastest path for learning the scoring
model and for testing scenario definitions in CI: script what the agent
"says" (including tool calls), run the scenario, and assert on the score.

It also serves as the conformance-test reference — test_runtime_conformance.py
uses it to verify that run_scenario() produces comparable Traces regardless
of the runtime used.

Scripted entries (one per send() call) are either:

- ``str`` — a content-only reply (``tool_calls=[]``, ``finish_reason="stop"``).
- ``dict`` — optional keys ``content: str`` and ``tool_calls: list[dict]``
  (OpenAI wire shape, passed through as-given into the response message).
  ``finish_reason`` is ``"tool_calls"`` when tool_calls is non-empty,
  else ``"stop"``.

When the script is exhausted, the last entry repeats.
"""
from __future__ import annotations

from typing import Any

from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, Message, Response

# One scripted reply: plain text, or {"content": ..., "tool_calls": [...]}.
ScriptedEntry = str | dict[str, Any]


def _entry_to_message(entry: ScriptedEntry) -> tuple[dict[str, Any], str]:
    """Convert a scripted entry into (assistant message, finish_reason)."""
    if isinstance(entry, str):
        content: str = entry
        tool_calls: list[dict[str, Any]] = []
    else:
        content = entry.get("content", "")
        # Pass through as-given (OpenAI wire shape) — no normalisation.
        tool_calls = entry.get("tool_calls", []) or []
    finish_reason = "tool_calls" if tool_calls else "stop"
    message = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }
    return message, finish_reason


class _InMemoryHandle:
    """AgentHandle that records calls and returns scripted responses."""

    def __init__(self, responses: list[ScriptedEntry]) -> None:
        # responses: list of scripted entries, one per send() call.
        # When exhausted, repeats the last response.
        self._responses = list(responses) if responses else ["ok"]
        self._call_count = 0
        self.calls: list[tuple[list[Message], str]] = []  # (messages, session_id)
        self.reset_count = 0
        self.teardown_count = 0

    def send(self, messages: list[Message], session_id: str) -> Response:
        self.calls.append((list(messages), session_id))
        idx = min(self._call_count, len(self._responses) - 1)
        entry = self._responses[idx]
        self._call_count += 1
        message, finish_reason = _entry_to_message(entry)
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ]
        }

    def reset_state(self) -> None:
        self.reset_count += 1
        self._call_count = 0
        self.calls.clear()

    def teardown(self) -> None:
        self.teardown_count += 1


class InMemoryRuntime:
    """Zero-infrastructure AgentRuntime with scripted responses.

    Provide scripted_responses to control what the agent says — plain
    strings for content-only replies, or dicts with ``content`` /
    ``tool_calls`` keys for tool-calling turns (see module docstring).
    Each call to provision() returns a fresh handle.

    Usage::

        runtime = InMemoryRuntime(scripted_responses=["The answer is 42."])
        handle = runtime.provision(AgentConfig())
        resp = handle.send([{"role": "user", "content": "What is 6*7?"}], "sid")
        assert resp["choices"][0]["message"]["content"] == "The answer is 42."

    With a scripted tool call::

        runtime = InMemoryRuntime(scripted_responses=[
            {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "client_lookup", "arguments": "{}"},
                }],
            },
            "Found it: jane@example.com",
        ])
    """

    def __init__(self, scripted_responses: list[ScriptedEntry] | None = None) -> None:
        self._responses = scripted_responses or ["ok"]
        self.provisions: list[tuple[AgentConfig, _InMemoryHandle]] = []

    def provision(self, config: AgentConfig, mcps: list | None = None) -> AgentHandle:  # type: ignore[override]
        # mcps: ignored — InMemoryRuntime is network-free; the MCP handles are
        # not needed because send() returns scripted responses.
        handle = _InMemoryHandle(self._responses)
        self.provisions.append((config, handle))
        return handle  # type: ignore[return-value]
