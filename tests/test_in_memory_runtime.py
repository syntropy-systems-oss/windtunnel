"""InMemoryRuntime scripted-tool-call tests.

The shipped in_memory runtime (windtunnel/runtimes/in_memory/) accepts
scripted entries that are either plain strings (content-only replies) or
dicts with optional ``content`` / ``tool_calls`` keys (OpenAI wire shape).
These tests cover:

  - dict entries with tool_calls → the response message carries them
    verbatim and finish_reason == "tool_calls"
  - backward compat: plain str entries → tool_calls=[] / finish_reason="stop"
  - an end-to-end run_scenario() where a scripted tool call satisfies the
    requires_tool_use outcome gate AND a must_call trajectory check

The basic AgentRuntime/AgentHandle contract (provision, reset_state,
script exhaustion, call recording) is covered by test_runtime_conformance.py.
"""
from __future__ import annotations

import uuid

from windtunnel.api.runner import run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime
from windtunnel.spi.agent_runtime import AgentConfig

# A canonical OpenAI-wire-shape tool call used across tests.
_LOOKUP_CALL = {
    "id": "call_001",
    "type": "function",
    "function": {"name": "client_lookup", "arguments": '{"client": "Jane"}'},
}


def _send(runtime: InMemoryRuntime, prompt: str = "hi") -> dict:
    handle = runtime.provision(AgentConfig())
    return handle.send([{"role": "user", "content": prompt}], str(uuid.uuid4()))


class TestScriptedToolCalls:
    def test_dict_entry_with_tool_calls(self) -> None:
        """A dict entry's tool_calls pass through as-given; finish_reason=tool_calls."""
        runtime = InMemoryRuntime(scripted_responses=[
            {"content": "Looking that up.", "tool_calls": [_LOOKUP_CALL]},
        ])
        resp = _send(runtime)
        choice = resp["choices"][0]
        assert choice["message"]["tool_calls"] == [_LOOKUP_CALL]
        assert choice["message"]["content"] == "Looking that up."
        assert choice["finish_reason"] == "tool_calls"

    def test_dict_entry_without_tool_calls_is_stop(self) -> None:
        """A dict entry with content only behaves like a plain str entry."""
        runtime = InMemoryRuntime(scripted_responses=[{"content": "just text"}])
        resp = _send(runtime)
        choice = resp["choices"][0]
        assert choice["message"]["content"] == "just text"
        assert choice["message"]["tool_calls"] == []
        assert choice["finish_reason"] == "stop"

    def test_dict_entry_content_defaults_to_empty(self) -> None:
        """A pure tool-call turn (no content key) yields content=''."""
        runtime = InMemoryRuntime(scripted_responses=[{"tool_calls": [_LOOKUP_CALL]}])
        resp = _send(runtime)
        choice = resp["choices"][0]
        assert choice["message"]["content"] == ""
        assert choice["finish_reason"] == "tool_calls"

    def test_plain_str_entry_backward_compat(self) -> None:
        """Plain str entries keep the original semantics: no tool calls, stop."""
        runtime = InMemoryRuntime(scripted_responses=["just a reply"])
        resp = _send(runtime)
        choice = resp["choices"][0]
        assert choice["message"]["content"] == "just a reply"
        assert choice["message"]["tool_calls"] == []
        assert choice["finish_reason"] == "stop"

    def test_mixed_script_repeats_last_entry(self) -> None:
        """str and dict entries mix; exhaustion repeats the last entry."""
        runtime = InMemoryRuntime(scripted_responses=[
            {"content": "", "tool_calls": [_LOOKUP_CALL]},
            "done",
        ])
        handle = runtime.provision(AgentConfig())
        sid = str(uuid.uuid4())
        first = handle.send([{"role": "user", "content": "go"}], sid)
        second = handle.send([{"role": "user", "content": "and?"}], sid)
        third = handle.send([{"role": "user", "content": "more?"}], sid)
        assert first["choices"][0]["finish_reason"] == "tool_calls"
        assert second["choices"][0]["message"]["content"] == "done"
        assert third["choices"][0]["message"]["content"] == "done"  # repeats last

    def test_reset_state_rewinds_script_with_dict_entries(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=[
            {"content": "", "tool_calls": [_LOOKUP_CALL]},
            "done",
        ])
        handle = runtime.provision(AgentConfig())
        sid = str(uuid.uuid4())
        handle.send([{"role": "user", "content": "go"}], sid)
        handle.send([{"role": "user", "content": "and?"}], sid)
        handle.reset_state()
        resp = handle.send([{"role": "user", "content": "go again"}], sid)
        assert resp["choices"][0]["finish_reason"] == "tool_calls"


class TestRunScenarioWithScriptedToolCalls:
    def test_requires_tool_use_gate_passes_with_scripted_tool_call(self) -> None:
        """E2E: a scripted tool_call satisfies requires_tool_use + must_call.

        Both gates read the TRACE, not an MCP call_log: evaluate_outcome's
        requires_tool_use gate checks turn.tool_calls non-empty, and
        evaluate_trajectory's must_call extracts names from turn.tool_calls
        (OpenAI shape: tc["function"]["name"]). The runner copies the response
        message's tool_calls onto the assistant Turn, so the scripted shape
        is sufficient — no MCP server needed.
        """
        scenario = Scenario(
            name="lookup_email",
            prompt="What is Jane's email?",
            target_facts=[["jane@example.com"]],
            requires_tool_use=True,
            must_call=["client_lookup"],
        )
        # Single-turn scenario → one send(); the one scripted entry must carry
        # BOTH the tool call (for the gates) and the final answer text (for
        # target_facts, which scores the last assistant turn's content).
        runtime = InMemoryRuntime(scripted_responses=[
            {"content": "Jane's email is jane@example.com.",
             "tool_calls": [_LOOKUP_CALL]},
        ])
        result = run_scenario(scenario, runtime)
        score = result.runs[0].score
        assert score.outcome.passed is True, score.outcome.detail
        assert score.trajectory.passed is True, score.trajectory.detail
        # The trace itself carries the scripted tool call.
        assistant_turns = [t for t in result.runs[0].trace.turns if t.role == "assistant"]
        assert assistant_turns[-1].tool_calls == [_LOOKUP_CALL]

    def test_requires_tool_use_gate_fails_with_plain_str_script(self) -> None:
        """Control: same scenario with a content-only script trips the gate."""
        scenario = Scenario(
            name="lookup_email",
            prompt="What is Jane's email?",
            target_facts=[["jane@example.com"]],
            requires_tool_use=True,
            must_call=["client_lookup"],
        )
        runtime = InMemoryRuntime(scripted_responses=[
            "Jane's email is jane@example.com.",  # right answer, no tool call
        ])
        result = run_scenario(scenario, runtime)
        score = result.runs[0].score
        assert score.outcome.passed is False
        assert "no_tools_used" in score.outcome.detail
        assert score.trajectory.passed is False  # must_call unsatisfied too
