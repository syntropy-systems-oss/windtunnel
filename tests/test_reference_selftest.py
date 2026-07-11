"""Golden/poison reference execution through the live scenario path."""
from __future__ import annotations

from typing import Any

import pytest

from windtunnel.api import (
    Policy,
    ReferenceCase,
    ReferenceDecision,
    ReferenceToolCall,
    Scenario,
    SelfTestVerdict,
    StateProbeAvailable,
    run_reference_case,
    selftest_case_to_dict,
)
from windtunnel.runtimes.in_memory import InMemoryRuntime
from windtunnel.spi import AgentConfig, ReferenceCapableAgentRuntime, ReferenceKind


def _decision(*, safe: bool) -> ReferenceDecision:
    return ReferenceDecision(
        tool_calls=(
            ReferenceToolCall(name="write_artifact", arguments={"safe": safe}),
        )
    )


def _case(name: str, kind: ReferenceKind, *, safe: bool) -> ReferenceCase:
    return ReferenceCase(
        name=name,
        kind=kind,
        decisions=(
            _decision(safe=safe),
            ReferenceDecision(content="artifact complete"),
        ),
    )


class _ArtifactProbe:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1
        self.state.clear()

    def capture(self) -> dict[str, Any]:
        return {"artifact": dict(self.state)}


class _ReferenceHandle:
    def __init__(self, case: ReferenceCase, probe: _ArtifactProbe) -> None:
        self.case = case
        self.probe = probe
        self.send_count = 0
        self.reset_count = 0
        self.teardown_count = 0

    def reset_state(self) -> None:
        self.reset_count += 1

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        del messages, session_id
        self.send_count += 1
        witnessed: list[dict[str, Any]] = []
        for decision_index, decision in enumerate(self.case.decisions):
            for call_index, call in enumerate(decision.tool_calls):
                self.probe.state.update(call.arguments)
                witnessed.append(
                    {
                        "id": f"reference-{decision_index}-{call_index}",
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                )
        return {
            "content": self.case.decisions[-1].content,
            "tool_calls": witnessed,
        }

    def teardown(self) -> None:
        self.teardown_count += 1


class _ReferenceRuntime:
    accepts_runner_managed_mcps = True

    def __init__(self, probe: _ArtifactProbe) -> None:
        self.probe = probe
        self.cases: list[ReferenceCase] = []
        self.handles: list[_ReferenceHandle] = []

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> _ReferenceHandle:
        del config, mcps
        raise AssertionError("ordinary provision must not serve a reference case")

    def provision_reference(
        self,
        config: AgentConfig,
        case: ReferenceCase,
        mcps: list[Any] | None = None,
    ) -> _ReferenceHandle:
        del config, mcps
        self.cases.append(case)
        handle = _ReferenceHandle(case, self.probe)
        self.handles.append(handle)
        return handle


def _scenario(*cases: ReferenceCase) -> Scenario:
    return Scenario(
        name="artifact_guard",
        prompt="Create the safe artifact.",
        target_facts=[["artifact complete"]],
        requires_tool_use=True,
        policies=[
            Policy(
                name="artifact_is_safe",
                predicate=lambda trace: trace.observations["artifact"]["safe"] is True,
            )
        ],
        preconditions=[StateProbeAvailable()],
        reference_cases=list(cases),
    )


def test_reference_contract_validates_decision_shape() -> None:
    with pytest.raises(ValueError, match="requires content or"):
        ReferenceDecision()
    with pytest.raises(ValueError, match="final reference decision"):
        ReferenceCase(
            name="no-final",
            kind="golden",
            decisions=(_decision(safe=True),),
        )
    with pytest.raises(ValueError, match="non-final"):
        ReferenceCase(
            name="early-final",
            kind="golden",
            decisions=(
                ReferenceDecision(content="stops too early"),
                ReferenceDecision(content="actual final"),
            ),
        )


def test_reference_tool_arguments_must_be_json_serializable() -> None:
    with pytest.raises(ValueError, match="JSON-serializable"):
        ReferenceToolCall(name="write", arguments={"value": object()})


def test_scenario_rejects_duplicate_reference_case_names() -> None:
    first = _case("same-name", "golden", safe=True)
    second = _case("same-name", "poison", safe=False)
    with pytest.raises(ValueError, match="names must be unique"):
        _scenario(first, second)


def test_runtime_capability_is_structural_and_explicit() -> None:
    runtime = _ReferenceRuntime(_ArtifactProbe())
    assert isinstance(runtime, ReferenceCapableAgentRuntime)
    assert not isinstance(InMemoryRuntime(), ReferenceCapableAgentRuntime)


def test_unsupported_runtime_is_visible_and_does_not_run() -> None:
    case = _case("golden", "golden", safe=True)
    result = run_reference_case(_scenario(case), InMemoryRuntime(), case)

    assert result.verdict is SelfTestVerdict.UNSUPPORTED
    assert "provision_reference" in result.detail
    assert result.trace is None


def test_golden_passes_when_live_probe_and_gate_accept_reference() -> None:
    case = _case("golden", "golden", safe=True)
    probe = _ArtifactProbe()
    runtime = _ReferenceRuntime(probe)

    result = run_reference_case(_scenario(case), runtime, case, state_probe=probe)

    assert result.verdict is SelfTestVerdict.PASS
    assert result.score is not None and result.score.constraint.passed
    assert result.trace is not None
    assert result.trace.observations == {"artifact": {"safe": True}}
    assert runtime.cases == [case]
    assert runtime.handles[0].teardown_count == 1


def test_golden_failure_is_distinct_from_agent_run_failure() -> None:
    case = _case("broken-golden", "golden", safe=False)
    probe = _ArtifactProbe()

    result = run_reference_case(
        _scenario(case),
        _ReferenceRuntime(probe),
        case,
        state_probe=probe,
    )

    assert result.verdict is SelfTestVerdict.GOLDEN_FAILED
    assert result.score is not None and not result.score.constraint.passed


def test_poison_passes_selftest_only_when_declared_gate_rejects_it() -> None:
    case = _case("unsafe-write", "poison", safe=False)
    probe = _ArtifactProbe()

    result = run_reference_case(
        _scenario(case),
        _ReferenceRuntime(probe),
        case,
        state_probe=probe,
    )

    assert result.verdict is SelfTestVerdict.PASS
    assert result.score is not None and not result.score.constraint.passed


def test_poison_passing_agent_gate_is_a_selftest_failure() -> None:
    case = _case("ineffective-poison", "poison", safe=True)
    probe = _ArtifactProbe()

    result = run_reference_case(
        _scenario(case),
        _ReferenceRuntime(probe),
        case,
        state_probe=probe,
    )

    assert result.verdict is SelfTestVerdict.POISON_PASSED
    assert result.score is not None and result.score.gate_passed(
        _scenario(case).resolved_gate_layers()
    )


def test_missing_required_probe_is_harness_error_before_send() -> None:
    case = _case("golden", "golden", safe=True)
    runtime = _ReferenceRuntime(_ArtifactProbe())

    result = run_reference_case(_scenario(case), runtime, case)

    assert result.verdict is SelfTestVerdict.ERROR
    assert "world precondition failed" in result.detail
    assert runtime.handles[0].send_count == 0
    assert runtime.handles[0].teardown_count == 1


def test_probe_capture_failure_is_error_not_golden_or_poison_signal() -> None:
    class BrokenProbe(_ArtifactProbe):
        def capture(self) -> dict[str, Any]:
            raise RuntimeError("synthetic capture outage")

    case = _case("golden", "golden", safe=True)
    probe = BrokenProbe()

    result = run_reference_case(
        _scenario(case),
        _ReferenceRuntime(probe),
        case,
        state_probe=probe,
    )

    assert result.verdict is SelfTestVerdict.ERROR
    assert "probe_error: capture failed" in result.detail


def test_cases_are_isolated_by_fresh_handle_and_probe_reset() -> None:
    golden = _case("golden", "golden", safe=True)
    poison = _case("poison", "poison", safe=False)
    probe = _ArtifactProbe()
    runtime = _ReferenceRuntime(probe)
    scenario = _scenario(golden, poison)

    first = run_reference_case(scenario, runtime, golden, state_probe=probe)
    second = run_reference_case(scenario, runtime, poison, state_probe=probe)

    assert [first.verdict, second.verdict] == [SelfTestVerdict.PASS, SelfTestVerdict.PASS]
    assert probe.reset_count == 2
    assert len(runtime.handles) == 2
    assert all(handle.reset_count == 1 for handle in runtime.handles)
    assert all(handle.teardown_count == 1 for handle in runtime.handles)


def test_machine_payload_keeps_selftest_verdict_separate_from_score() -> None:
    case = _case("unsafe-write", "poison", safe=False)
    probe = _ArtifactProbe()
    result = run_reference_case(
        _scenario(case),
        _ReferenceRuntime(probe),
        case,
        state_probe=probe,
    )

    payload = selftest_case_to_dict(result)

    assert payload["verdict"] == "PASS"
    assert payload["kind"] == "poison"
    assert payload["score"]["constraint"]["passed"] is False
