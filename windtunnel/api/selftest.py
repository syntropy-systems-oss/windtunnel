"""Harness self-certification with live golden and poison reference cases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from windtunnel.api.preconditions import WorldMismatchError
from windtunnel.api.runner import run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score, score_to_dict
from windtunnel.api.trace import Trace
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, AgentRuntime
from windtunnel.spi.mcp_server import MCPServer
from windtunnel.spi.reference import ReferenceCapableAgentRuntime, ReferenceCase
from windtunnel.spi.state_probe import StateProbe


class SelfTestVerdict(StrEnum):
    """One reference case's harness-certification result."""

    PASS = "PASS"
    GOLDEN_FAILED = "GOLDEN_FAILED"
    POISON_PASSED = "POISON_PASSED"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SelfTestCaseResult:
    """Result of executing one reference case through the live run path."""

    scenario_id: str
    case: ReferenceCase
    verdict: SelfTestVerdict
    detail: str
    trace: Trace | None = None
    score: Score | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is SelfTestVerdict.PASS


class _ReferenceRuntimeAdapter:
    """Present one reference case through the ordinary AgentRuntime seam."""

    def __init__(
        self,
        runtime: ReferenceCapableAgentRuntime,
        case: ReferenceCase,
    ) -> None:
        self._runtime = runtime
        self._case = case

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> AgentHandle:
        return self._runtime.provision_reference(config, self._case, mcps=mcps)


def run_reference_case(
    scenario: Scenario,
    runtime: AgentRuntime,
    case: ReferenceCase,
    mcps: list[MCPServer] | None = None,
    *,
    config: AgentConfig | None = None,
    state_probe: StateProbe | None = None,
) -> SelfTestCaseResult:
    """Run one golden/poison case through normal fixtures, evidence, and scoring.

    The only substituted component is model inference, owned by the runtime's
    ``provision_reference`` implementation. Wind Tunnel still calls the regular
    scenario runner, including MCP lifecycle, probe binding/reset/capture, world
    preconditions, trace construction, and all score layers.
    """
    if not isinstance(runtime, ReferenceCapableAgentRuntime):
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.UNSUPPORTED,
            detail=(
                f"runtime {type(runtime).__name__} does not implement "
                "provision_reference()"
            ),
        )

    resolved_config = config or AgentConfig(
        agent_id="wt-selftest",
        variant_id=f"selftest-{case.kind}-{case.name}",
    )
    adapter = _ReferenceRuntimeAdapter(runtime, case)
    try:
        scenario_result = run_scenario(
            scenario,
            adapter,
            mcps=mcps,
            config=resolved_config,
            runs_per_scenario=1,
            state_probe=state_probe,
        )
    except WorldMismatchError as exc:
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.ERROR,
            detail=f"world precondition failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - runtime failures are self-test results
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.ERROR,
            detail=f"reference runtime failed: {type(exc).__name__}: {exc}",
        )

    if len(scenario_result.runs) != 1:
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.ERROR,
            detail=f"expected exactly one reference run, got {len(scenario_result.runs)}",
        )

    run = scenario_result.runs[0]
    trace = run.trace
    score = run.score
    execution_errors = [
        warning
        for warning in trace.worker_warnings
        if warning.startswith(
            ("runner_error:", "probe_error:", "mcp_evidence: unavailable")
        )
    ]
    if execution_errors or not score.integrity.passed:
        detail = execution_errors[0] if execution_errors else score.integrity.detail
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.ERROR,
            detail=f"reference execution was invalid: {detail}",
            trace=trace,
            score=score,
        )

    gate_layers = scenario.resolved_gate_layers()
    actual_pass = score.gate_passed(gate_layers)
    expected_pass = case.kind == "golden"
    if actual_pass == expected_pass:
        expectation = "passed" if expected_pass else "failed"
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.PASS,
            detail=f"{case.kind} reference {expectation} the declared gate as expected",
            trace=trace,
            score=score,
        )

    verdict = (
        SelfTestVerdict.GOLDEN_FAILED if case.kind == "golden" else SelfTestVerdict.POISON_PASSED
    )
    detail = (
        "known-correct reference failed the declared gate"
        if case.kind == "golden"
        else "known-bad reference passed the declared gate"
    )
    return SelfTestCaseResult(
        scenario_id=scenario.name,
        case=case,
        verdict=verdict,
        detail=detail,
        trace=trace,
        score=score,
    )


def selftest_case_to_dict(result: SelfTestCaseResult) -> dict[str, Any]:
    """Return the stable machine-readable representation of one case."""
    payload: dict[str, Any] = {
        "scenario_id": result.scenario_id,
        "case": result.case.name,
        "kind": result.case.kind,
        "verdict": result.verdict.value,
        "detail": result.detail,
    }
    if result.trace is not None:
        payload["run_id"] = result.trace.run_id
    if result.score is not None:
        payload["score"] = score_to_dict(result.score)
    return payload
