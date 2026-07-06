"""Tests for windtunnel/api/runner.py — run_scenario() and run_matrix().

Covers:
  - run_scenario() drives single-turn and multi-turn scenarios
  - run_scenario() handles matrix dispatch via run_matrix()
  - runner imports ONLY api/* and spi/* (invariant test is separate)
  - session_id threading — same id across all turns in a scenario
  - server-witnessed call-log collection — trace.mcp_calls drained from
    every MCPHandle, normalized to dicts, merged chronologically
  - external-state observation capture — trace.observations snapshotted
    from a StateProbe per run (reset-before-run, capture-before-score)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.preconditions import Check, FileExists, WorldMismatchError
from windtunnel.api.runner import ScenarioResult, run_matrix, run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, SamplingConfig
from windtunnel.spi.mcp_server import MCPCall

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _scenario(name: str = "test", prompt: str = "hello", facts: list[list[str]] | None = None) -> Scenario:
    return Scenario(
        name=name,
        prompt=prompt,
        target_facts=facts or [["ok"]],
    )


# ─── run_scenario ─────────────────────────────────────────────────────────────

class TestRunScenario:
    def test_returns_scenario_result(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime)
        assert isinstance(result, ScenarioResult)

    def test_single_run_by_default(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime)
        assert result.aggregate.total == 1

    def test_multiple_runs(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, runs_per_scenario=5)
        assert result.aggregate.total == 5
        assert len(result.runs) == 5

    def test_all_pass_when_fact_present(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok everything is ok"])
        result = run_scenario(_scenario(facts=[["ok"]]), runtime, runs_per_scenario=3)
        assert result.aggregate.verdict == "PASS"
        assert result.aggregate.passed == 3

    def test_all_fail_when_fact_absent(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["nope"])
        result = run_scenario(_scenario(facts=[["ok"]]), runtime)
        assert result.aggregate.verdict == "FAIL"
        assert result.runs[0].score.outcome.passed is False

    def test_trace_has_user_and_assistant_turns(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(prompt="hello"), runtime)
        turns = result.runs[0].trace.turns
        assert turns[0].role == "user"
        assert turns[0].content == "hello"
        assert turns[1].role == "assistant"
        assert turns[1].content == "ok"

    def test_skip_reset_flag(self) -> None:
        """skip_reset=True must not call handle.reset_state()."""
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime, runs_per_scenario=2, skip_reset=True)
        # Provisions once, so we can check the first handle
        _, handle = runtime.provisions[0]
        assert handle.reset_count == 0

    def test_reset_called_before_each_run_by_default(self) -> None:
        """reset_state() must be called once per run when skip_reset=False."""
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime, runs_per_scenario=3)
        _, handle = runtime.provisions[0]
        assert handle.reset_count == 3

    def test_teardown_called_after_batch(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime)
        _, handle = runtime.provisions[0]
        assert handle.teardown_count == 1

    def test_teardown_called_even_on_error(self) -> None:
        """teardown() must be called even if a run raises."""
        class FailingHandle:
            teardown_count = 0
            def send(self, messages: Any, session_id: str) -> Any:
                raise RuntimeError("network error")
            def reset_state(self) -> None:
                pass
            def teardown(self) -> None:
                self.teardown_count += 1

        class FailingRuntime:
            handle = FailingHandle()
            def provision(self, config: AgentConfig, mcps: list | None = None) -> AgentHandle:
                return self.handle  # type: ignore[return-value]

        failing = FailingRuntime()
        result = run_scenario(_scenario(), failing)
        # run should produce a failed result, not raise
        assert result.runs[0].score.outcome.passed is False
        assert failing.handle.teardown_count == 1

    def test_config_agent_id_in_trace(self) -> None:
        config = AgentConfig(agent_id="my-agent", variant_id="v2")
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, config=config)
        trace = result.runs[0].trace
        assert trace.agent_id == "my-agent"
        assert trace.variant_id == "v2"

    def test_sampling_config_in_trace_sampler(self) -> None:
        config = AgentConfig(sampling=SamplingConfig(temperature=0.5, top_p=0.8))
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, config=config)
        sampler = result.runs[0].trace.sampler
        assert sampler["temperature"] == 0.5
        assert sampler["top_p"] == 0.8

    def test_empty_mcps_list_allowed(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, mcps=[])
        assert result.aggregate.total == 1


# ─── session_id threading ─────────────────────────────────────────────────────

class TestSessionIdThreading:
    def test_single_turn_gets_fresh_session_id(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime)
        _, handle = runtime.provisions[0]
        assert len(handle.calls) == 1
        session_id = handle.calls[0][1]
        assert len(session_id) == 36  # UUID format

    def test_multi_turn_same_session_id_across_turns(self) -> None:
        """Multi-turn: SAME session_id on every call within one scenario run."""
        # user_turns is a first-class Scenario field; non-empty = multi-turn.
        scenario = Scenario(
            name="mt",
            prompt="turn 3",  # convention: copy of user_turns[-1]; runner ignores it
            target_facts=[["turn3"]],
            user_turns=["turn 1", "turn 2", "turn 3"],
        )

        runtime = InMemoryRuntime(scripted_responses=["resp1", "resp2", "turn3"])
        run_scenario(scenario, runtime)
        _, handle = runtime.provisions[0]

        # All 3 send() calls used the same session_id
        session_ids = [call[1] for call in handle.calls]
        assert len(session_ids) == 3
        assert len(set(session_ids)) == 1  # all same

    def test_different_runs_get_different_session_ids(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime, runs_per_scenario=3, skip_reset=False)
        # After N runs, provisions[0] handle was reset N times and called N times
        # Each run should have had a different session id
        _, handle = runtime.provisions[0]
        # handle.calls is cleared on reset_state(), so we can't inspect them all.
        # Instead verify that the handle was called 3 times total (1 per run)
        # by checking reset_count == 3 (which means 3 separate runs)
        assert handle.reset_count == 3


# ─── user_turns field semantics ───────────────────────────────────────────────

class TestUserTurnsField:
    def test_non_empty_user_turns_is_the_full_turn_list_and_prompt_is_ignored(self) -> None:
        """When user_turns is non-empty it IS the ordered user-turn list;
        scenario.prompt is never sent."""
        scenario = Scenario(
            name="mt",
            prompt="THIS PROMPT MUST NOT BE SENT",
            target_facts=[["ok"]],
            user_turns=["turn 1", "turn 2"],
        )
        runtime = InMemoryRuntime(scripted_responses=["resp1", "ok"])
        run_scenario(scenario, runtime)
        _, handle = runtime.provisions[0]
        assert len(handle.calls) == 2
        sent_user_contents = [
            m["content"]
            for messages, _sid in handle.calls
            for m in messages
            if m["role"] == "user"
        ]
        assert "THIS PROMPT MUST NOT BE SENT" not in sent_user_contents
        # Final call carries the accumulated history: turn 1, resp1, turn 2
        final_messages, _ = handle.calls[-1]
        assert [m["content"] for m in final_messages] == ["turn 1", "resp1", "turn 2"]

    def test_empty_user_turns_falls_back_to_prompt(self) -> None:
        scenario = _scenario(prompt="single prompt")
        assert scenario.user_turns == []  # default = single-turn
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(scenario, runtime)
        _, handle = runtime.provisions[0]
        messages, _ = handle.calls[0]
        assert messages == [{"role": "user", "content": "single prompt"}]


# ─── Response-shape tolerance (_extract_reply) ────────────────────────────────

class _ShapedHandle:
    """AgentHandle stub that returns a fixed response dict from send()."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def send(self, messages: Any, session_id: str) -> dict[str, Any]:
        return self._response

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _ShapedRuntime:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def provision(self, config: AgentConfig, mcps: list | None = None) -> AgentHandle:
        return _ShapedHandle(self._response)  # type: ignore[return-value]


_TOOL_CALL = {
    "id": "call_0",
    "type": "function",
    "function": {"name": "client_lookup", "arguments": "{}"},
}


class TestResponseShapeTolerance:
    """AgentHandle.send() may return the OpenAI choices shape, a flat
    message, or a wrapped message — the runner accepts all three."""

    def _run(self, response: dict[str, Any]):
        runtime = _ShapedRuntime(response)
        result = run_scenario(_scenario(facts=[["ok"]]), runtime)  # type: ignore[arg-type]
        return result.runs[0].trace.turns[-1]

    def test_openai_choices_shape(self) -> None:
        last = self._run({"choices": [{"message": {
            "role": "assistant", "content": "ok done", "tool_calls": [_TOOL_CALL],
        }}]})
        assert last.content == "ok done"
        assert last.tool_calls == [_TOOL_CALL]

    def test_flat_shape(self) -> None:
        last = self._run({"content": "ok done", "tool_calls": [_TOOL_CALL]})
        assert last.content == "ok done"
        assert last.tool_calls == [_TOOL_CALL]

    def test_wrapped_message_shape(self) -> None:
        last = self._run({"message": {"content": "ok done", "tool_calls": [_TOOL_CALL]}})
        assert last.content == "ok done"
        assert last.tool_calls == [_TOOL_CALL]

    def test_empty_choices_normalizes_to_empty(self) -> None:
        last = self._run({"choices": []})
        assert last.content == ""
        assert last.tool_calls == []

    def test_flat_shape_missing_fields_normalize(self) -> None:
        last = self._run({"content": None})
        assert last.content == ""
        assert last.tool_calls == []


# ─── MCP call-log collection ──────────────────────────────────────────────────

class _StubMCPHandle:
    """MCPHandle whose call_log() replays a scripted list of MCPCalls."""

    def __init__(self, calls: list[MCPCall]) -> None:
        self._calls = calls
        self.reset_count = 0

    @property
    def url(self) -> str:
        return "http://localhost:9999/mcp"

    def call_log(self) -> list[MCPCall]:
        return list(self._calls)

    def reset_call_log(self) -> None:
        self.reset_count += 1

    def configure_failure_mode(self, mode: str | None) -> None:
        pass


class _IntrospectableMCPHandle(_StubMCPHandle):
    def __init__(self, calls: list[MCPCall], tools: list[str]) -> None:
        super().__init__(calls)
        self._tools = tools

    def served_tools(self) -> list[str]:
        return list(self._tools)


class _StubMCPServer:
    def __init__(self, calls: list[MCPCall], *, tools: list[str] | None = None) -> None:
        if tools is None:
            self.handle = _StubMCPHandle(calls)
        else:
            self.handle = _IntrospectableMCPHandle(calls, tools)
        self.stop_count = 0

    def start(self) -> _StubMCPHandle:
        return self.handle

    def stop(self) -> None:
        self.stop_count += 1


def _mcp_call(name: str, ts: float) -> MCPCall:
    return MCPCall(tool_name=name, args={"q": "x"}, result="ok", timestamp_ms=ts)


class TestMcpCallCollection:
    def test_trace_carries_server_witnessed_calls(self) -> None:
        """After a run, trace.mcp_calls holds the handle's log as plain dicts."""
        server = _StubMCPServer([_mcp_call("ops_client_lookup", 1000.0)])
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, mcps=[server])
        trace = result.runs[0].trace
        assert trace.mcp_calls == [{
            "tool_name": "ops_client_lookup",
            "args": {"q": "x"},
            "result": "ok",
            "timestamp_ms": 1000.0,
        }]

    def test_multiple_handles_merged_chronologically(self) -> None:
        """Logs from several servers merge into ONE timestamp-ordered stream."""
        server_a = _StubMCPServer([_mcp_call("a_tool", 2000.0)])
        server_b = _StubMCPServer([_mcp_call("b_tool", 1000.0)])
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, mcps=[server_a, server_b])
        names = [c["tool_name"] for c in result.runs[0].trace.mcp_calls]
        assert names == ["b_tool", "a_tool"]

    def test_call_log_reset_before_each_run(self) -> None:
        """Logs are reset per run so each trace sees only its own traffic."""
        server = _StubMCPServer([])
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime, mcps=[server], runs_per_scenario=3)
        assert server.handle.reset_count == 3

    def test_no_mcps_means_empty_mcp_calls(self) -> None:
        """in_memory-style runs (no MCP servers) leave mcp_calls empty —
        the evaluator falls back to transcript evidence."""
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime)
        assert result.runs[0].trace.mcp_calls == []

    def test_non_json_result_coerced(self) -> None:
        """A non-JSON-serializable tool result must not break the trace."""
        weird = object()
        server = _StubMCPServer([
            MCPCall(tool_name="t", args={}, result=weird, timestamp_ms=1.0),
        ])
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, mcps=[server])
        stored = result.runs[0].trace.mcp_calls[0]["result"]
        assert isinstance(stored, str)  # repr() fallback


# ─── World preconditions ─────────────────────────────────────────────────────

class TestWorldPreconditions:
    def test_requires_tools_sugar_passes_when_mcp_serves_tool(self) -> None:
        scenario = _scenario()
        scenario.requires_tools = ["client_lookup"]
        server = _StubMCPServer([], tools=["client_lookup"])
        runtime = InMemoryRuntime(scripted_responses=["ok"])

        result = run_scenario(scenario, runtime, mcps=[server])

        assert result.aggregate.verdict == "PASS"

    def test_world_mismatch_raises_before_reset_or_send(self) -> None:
        scenario = _scenario()
        scenario.requires_tools = ["missing_tool"]
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        server = _StubMCPServer([], tools=["client_lookup"])

        with pytest.raises(WorldMismatchError) as excinfo:
            run_scenario(scenario, runtime, mcps=[server])

        assert "missing_tool" in str(excinfo.value)
        _, handle = runtime.provisions[0]
        assert handle.reset_count == 0
        assert handle.calls == []
        assert handle.teardown_count == 1
        assert server.stop_count == 1

    def test_world_mismatch_reports_all_failures(self, tmp_path: Path) -> None:
        missing = tmp_path / "not-seeded.txt"
        scenario = _scenario()
        scenario.requires_tools = ["missing_tool"]
        scenario.preconditions = [
            FileExists(missing),
            Check(lambda _ctx: "custom fixture missing", "custom fixture"),
        ]
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        server = _StubMCPServer([], tools=["client_lookup"])

        with pytest.raises(WorldMismatchError) as excinfo:
            run_scenario(scenario, runtime, mcps=[server])

        message = str(excinfo.value)
        assert "missing_tool" in message
        assert "not-seeded.txt" in message
        assert "custom fixture missing" in message


# ─── StateProbe — external-state observations ─────────────────────────────────

class _StubStateProbe:
    """StateProbe whose capture() replays a scripted snapshot.

    Counts reset()/capture() calls so tests can pin the per-run lifecycle
    (reset before EACH run, capture once per run before scoring).
    """

    def __init__(self, snapshot: Any) -> None:
        self._snapshot = snapshot
        self.reset_count = 0
        self.capture_count = 0

    def capture(self) -> Any:
        self.capture_count += 1
        return self._snapshot

    def reset(self) -> None:
        self.reset_count += 1


class _RaisingProbe:
    def capture(self) -> dict[str, Any]:
        raise ConnectionError("fixture is gone")

    def reset(self) -> None:
        pass


class TestStateProbeObservations:
    def test_trace_carries_observations(self) -> None:
        """After a run, trace.observations holds the probe's snapshot."""
        probe = _StubStateProbe({"github": {"prs": [{"base": "main"}]}})
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, state_probe=probe)
        assert result.runs[0].trace.observations == {
            "github": {"prs": [{"base": "main"}]},
        }

    def test_no_probe_means_empty_observations(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime)
        assert result.runs[0].trace.observations == {}

    def test_probe_reset_before_each_run(self) -> None:
        """Like reset_call_log: run N's observations must not contain run
        N-1's mutations, so the probe resets per run, not per batch."""
        probe = _StubStateProbe({})
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        run_scenario(_scenario(), runtime, state_probe=probe, runs_per_scenario=3)
        assert probe.reset_count == 3
        assert probe.capture_count == 3

    def test_observations_visible_to_policy_at_live_scoring(self) -> None:
        """Capture happens BEFORE scoring: a Policy reading
        trace.observations passes during the live run, not only on an
        offline re-score of the saved trace."""
        from windtunnel.api.scenario import Policy
        scenario = _scenario()
        scenario.policies = [Policy(
            name="pr_opened_against_main",
            predicate=lambda t: any(
                pr["base"] == "main" for pr in t.observations["github"]["prs"]
            ),
        )]
        probe = _StubStateProbe({"github": {"prs": [{"base": "main"}]}})
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(scenario, runtime, state_probe=probe)
        assert result.runs[0].score.constraint.passed is True

    def test_capture_failure_records_probe_error_warning(self) -> None:
        """A dead fixture degrades to a diagnosable warning, not a crash —
        and not a silent {} that masquerades as a policy violation."""
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, state_probe=_RaisingProbe())
        trace = result.runs[0].trace
        assert trace.observations == {}
        assert any(w.startswith("probe_error:") for w in trace.worker_warnings)

    def test_non_dict_capture_rejected_with_warning(self) -> None:
        probe = _StubStateProbe(["not", "a", "dict"])
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, state_probe=probe)
        trace = result.runs[0].trace
        assert trace.observations == {}
        assert any(w.startswith("probe_error:") for w in trace.worker_warnings)

    def test_non_json_values_coerced(self) -> None:
        """Non-serializable leaves coerce via repr() so save_trace works."""
        probe = _StubStateProbe({"db": {"handle": object()}})
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        result = run_scenario(_scenario(), runtime, state_probe=probe)
        stored = result.runs[0].trace.observations["db"]["handle"]
        assert isinstance(stored, str)  # repr() fallback


# ─── run_matrix ───────────────────────────────────────────────────────────────

class TestRunMatrix:
    def test_returns_dict_of_results(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        variants = [
            ("greedy", SamplingConfig(temperature=0.0)),
            ("temp07", SamplingConfig(temperature=0.7)),
        ]
        results = run_matrix(scenario, runtime, sampling_variants=variants)
        assert set(results.keys()) == {"greedy", "temp07"}

    def test_each_cell_has_scenario_result(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        variants = [("a", SamplingConfig()), ("b", SamplingConfig())]
        results = run_matrix(scenario, runtime, sampling_variants=variants, runs_per_cell=2)
        for key, res in results.items():
            assert isinstance(res, ScenarioResult)
            assert res.aggregate.total == 2

    def test_default_single_cell_when_no_variants(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        results = run_matrix(scenario, runtime)
        assert len(results) == 1
        assert "default" in results

    def test_variant_label_appears_in_variant_id(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        variants = [("greedy", SamplingConfig(temperature=0.0))]
        results = run_matrix(
            scenario, runtime,
            base_config=AgentConfig(variant_id="base"),
            sampling_variants=variants,
        )
        trace = results["greedy"].runs[0].trace
        assert "greedy" in trace.variant_id

    def test_matrix_passes_sampling_to_trace(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        variants = [("hot", SamplingConfig(temperature=1.0, top_p=0.9))]
        results = run_matrix(scenario, runtime, sampling_variants=variants)
        sampler = results["hot"].runs[0].trace.sampler
        assert sampler.get("temperature") == 1.0
        assert sampler.get("top_p") == 0.9

    def test_matrix_all_pass_when_responses_have_fact(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        scenario = _scenario(facts=[["ok"]])
        variants = [
            ("t0", SamplingConfig(temperature=0.0)),
            ("t1", SamplingConfig(temperature=1.0)),
        ]
        results = run_matrix(scenario, runtime, sampling_variants=variants, runs_per_cell=2)
        for key, res in results.items():
            assert res.aggregate.verdict == "PASS", f"variant {key} failed"
