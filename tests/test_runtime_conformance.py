"""Runtime conformance contract test.

Any runtime that passes these tests satisfies the Wind Tunnel SPI contract.
Currently tests:
  - InMemoryRuntime    (always available — no infra needed)

Platform driver runtimes (e.g. AcmeRuntime) run this same gate from their
own driver-package suites, using mocked HTTP so no live platform instance
is needed.

New runtimes MUST pass all tests in this file before they can be used
in production scenarios. This is the gate documented in writing-a-runtime.md.

Contract pinned here:
  - Runtimes produce comparable Traces for the same scenario (modulo
    timing + run_ids). If they don't, the SPI has hidden coupling —
    fix the contract.
  - The platform runtime is part of the conformance gate. All runtimes
    produce comparable Traces for the same minimal scenario. Mocked HTTP
    means no live platform instance is needed for the unit conformance
    tests.
"""
from __future__ import annotations

import uuid

from windtunnel.api.runner import ScenarioResult, run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle

# ─── Shared scenario fixture ──────────────────────────────────────────────────

def _echo_scenario(expected_word: str = "ECHO") -> Scenario:
    """A minimal scenario that expects a specific word in the response."""
    return Scenario(
        name="echo_test",
        prompt="Say ECHO",
        target_facts=[[expected_word]],
    )


# ─── InMemoryRuntime conformance ─────────────────────────────────────────────

class TestInMemoryRuntimeConformance:
    """InMemoryRuntime must satisfy the full AgentRuntime/AgentHandle contract."""

    def test_provision_returns_agent_handle(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["hello"])
        handle = runtime.provision(AgentConfig())
        assert isinstance(handle, AgentHandle)

    def test_send_returns_openai_shaped_response(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ECHO response"])
        handle = runtime.provision(AgentConfig())
        resp = handle.send([{"role": "user", "content": "Say ECHO"}], str(uuid.uuid4()))
        assert "choices" in resp
        choices = resp["choices"]
        assert len(choices) > 0
        msg = choices[0]["message"]
        assert msg["role"] == "assistant"
        assert "content" in msg

    def test_session_id_explicit_threading(self) -> None:
        """Same session_id threaded across all turns — multi-turn contract."""
        runtime = InMemoryRuntime(scripted_responses=["turn1", "turn2", "turn3"])
        handle = runtime.provision(AgentConfig())
        sid = "fixed-session"
        handle.send([{"role": "user", "content": "turn 1"}], sid)
        handle.send([{"role": "user", "content": "turn 1"},
                     {"role": "assistant", "content": "turn1"},
                     {"role": "user", "content": "turn 2"}], sid)
        assert all(call[1] == sid for call in handle.calls)  # type: ignore[attr-defined]

    def test_reset_state_clears_call_history(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        handle = runtime.provision(AgentConfig())
        handle.send([{"role": "user", "content": "x"}], "sid")
        handle.reset_state()
        assert handle.calls == []  # type: ignore[attr-defined]

    def test_teardown_idempotent(self) -> None:
        runtime = InMemoryRuntime()
        handle = runtime.provision(AgentConfig())
        handle.teardown()
        handle.teardown()  # second call must not raise

    def test_run_scenario_produces_trace(self) -> None:
        scenario = _echo_scenario("ECHO")
        runtime = InMemoryRuntime(scripted_responses=["ECHO back at you"])
        result = run_scenario(scenario, runtime, runs_per_scenario=1)
        assert isinstance(result, ScenarioResult)
        assert len(result.runs) == 1
        trace = result.runs[0].trace
        assert trace.scenario_id == "echo_test"
        assert len(trace.turns) >= 2  # user + assistant

    def test_run_scenario_scores_outcome(self) -> None:
        scenario = _echo_scenario("ECHO")
        runtime = InMemoryRuntime(scripted_responses=["ECHO back at you"])
        result = run_scenario(scenario, runtime)
        assert result.runs[0].score.outcome.passed is True

    def test_run_scenario_outcome_fails_when_fact_missing(self) -> None:
        scenario = _echo_scenario("MISSING_WORD")
        runtime = InMemoryRuntime(scripted_responses=["something else entirely"])
        result = run_scenario(scenario, runtime)
        assert result.runs[0].score.outcome.passed is False

    def test_multi_run_aggregation(self) -> None:
        scenario = _echo_scenario("ECHO")
        runtime = InMemoryRuntime(scripted_responses=["ECHO"])
        result = run_scenario(scenario, runtime, runs_per_scenario=3)
        assert result.aggregate.total == 3
        assert result.aggregate.passed == 3
        assert result.aggregate.verdict == "PASS"

    def test_trace_has_scenario_id_and_agent_id(self) -> None:
        scenario = _echo_scenario("ECHO")
        config = AgentConfig(agent_id="test-agent", variant_id="v1")
        runtime = InMemoryRuntime(scripted_responses=["ECHO"])
        result = run_scenario(scenario, runtime, config=config)
        trace = result.runs[0].trace
        assert trace.scenario_id == "echo_test"
        assert trace.agent_id == "test-agent"
        assert trace.variant_id == "v1"

    def test_trace_run_id_unique_across_runs(self) -> None:
        scenario = _echo_scenario("ECHO")
        runtime = InMemoryRuntime(scripted_responses=["ECHO"])
        result = run_scenario(scenario, runtime, runs_per_scenario=3)
        run_ids = [r.trace.run_id for r in result.runs]
        assert len(set(run_ids)) == 3  # all unique

    def test_trace_sampler_dict_populated_from_config(self) -> None:
        from windtunnel.spi.agent_runtime import SamplingConfig
        scenario = _echo_scenario("ECHO")
        config = AgentConfig(sampling=SamplingConfig(temperature=0.7, top_p=0.9))
        runtime = InMemoryRuntime(scripted_responses=["ECHO"])
        result = run_scenario(scenario, runtime, config=config)
        trace = result.runs[0].trace
        assert trace.sampler.get("temperature") == 0.7
        assert trace.sampler.get("top_p") == 0.9


# ─── Cross-runtime comparability ─────────────────────────────────────────────

class TestCrossRuntimeComparability:
    """Two different runtimes run against the same scenario must produce
    structurally comparable Traces — same scenario_id, same turn structure,
    consistent scoring.
    """

    def _make_runtime_a(self) -> InMemoryRuntime:
        return InMemoryRuntime(scripted_responses=["ECHO alpha"])

    def _make_runtime_b(self) -> InMemoryRuntime:
        # Different text but same target word present
        return InMemoryRuntime(scripted_responses=["beta ECHO response"])

    def test_both_runtimes_produce_same_scenario_id(self) -> None:
        scenario = _echo_scenario("ECHO")
        r_a = run_scenario(scenario, self._make_runtime_a())
        r_b = run_scenario(scenario, self._make_runtime_b())
        assert r_a.runs[0].trace.scenario_id == r_b.runs[0].trace.scenario_id

    def test_both_runtimes_pass_outcome(self) -> None:
        """Both runtimes return responses containing ECHO → both should pass."""
        scenario = _echo_scenario("ECHO")
        r_a = run_scenario(scenario, self._make_runtime_a())
        r_b = run_scenario(scenario, self._make_runtime_b())
        assert r_a.runs[0].score.outcome.passed is True
        assert r_b.runs[0].score.outcome.passed is True

    def test_both_runtimes_same_turn_count_for_single_turn(self) -> None:
        scenario = _echo_scenario("ECHO")
        r_a = run_scenario(scenario, self._make_runtime_a())
        r_b = run_scenario(scenario, self._make_runtime_b())
        # Single-turn: 1 user + 1 assistant = 2 turns each
        assert len(r_a.runs[0].trace.turns) == len(r_b.runs[0].trace.turns) == 2

    def test_both_runtimes_different_run_ids(self) -> None:
        """Run IDs must be different across runtime instances."""
        scenario = _echo_scenario("ECHO")
        r_a = run_scenario(scenario, self._make_runtime_a())
        r_b = run_scenario(scenario, self._make_runtime_b())
        assert r_a.runs[0].trace.run_id != r_b.runs[0].trace.run_id


