"""Regression snapshots for the intentionally public Python surface.

These tests do not prohibit change. They make additions, removals, and field
reordering visible in review instead of letting the public contract drift as a
side effect of internal refactors.
"""
from __future__ import annotations

import inspect
from dataclasses import fields

import windtunnel.api as api
import windtunnel.spi as spi
from windtunnel.api import (
    ReferenceCase,
    ReferenceDecision,
    ReferenceToolCall,
    Scenario,
    Score,
    SelfTestCaseResult,
    Trace,
    run_matrix,
    run_reference_case,
    run_scenario,
)
from windtunnel.spi import AgentConfig, SamplingConfig

EXPECTED_API_EXPORTS = (
    "TRACE_FORMAT_VERSION", "Hash", "Trace", "TraceFormatError", "Turn", "compute_hash",
    "load_trace", "save_trace", "storage_path", "SCORE_FORMAT_VERSION", "FailureCost",
    "GateLayer", "LayerResult", "Score", "ScoreFormatError", "Verdict", "score_from_dict",
    "score_to_dict",
    "NumberFact", "Perturbation", "Policy", "PreSendPerturbation", "Scenario",
    "TrajectoryCheck", "ReferenceCase", "ReferenceDecision", "ReferenceKind",
    "ReferenceToolCall", "Check", "FileExists", "Precondition", "PreconditionContext",
    "StateProbeAvailable", "ToolAvailable", "WorldMismatchError", "all_of", "any_of",
    "observation", "llm_judge",
    "substantiated_by_tools", "no_divergence", "ScenarioPack", "evaluate_outcome",
    "evaluate_trajectory", "evaluate_constraint", "evaluate_integrity", "evaluate_robustness",
    "tool_name_matches",
    "CorruptPriorAssistantTurn", "InjectStaleMemory", "ToolTimeout", "ToolReturnsMalformed",
    "BlankAssistantContent", "FallbackRenderLeak", "MalformedToolCall",
    "InjectWrongPriorToolCall", "InjectSchemaRejectedCall", "InjectPaginationTruncation",
    "ToolReturnsMalformedJson", "ToolTimeoutPerScenario", "ToolReturnsEmptyUnexpected",
    "AggregateResult", "ScenarioRunResult", "aggregate_runs", "CanaryResult",
    "run_reset_canary", "ScenarioResult", "run_matrix", "run_scenario", "SelfTestCaseResult",
    "SelfTestVerdict", "run_reference_case", "selftest_case_to_dict", "GenerateFn", "replay",
    "StateResetConfig", "reset_state_db", "UNIVERSE_VERSION", "SynthesizeHook", "Universe",
    "UniverseFormatError", "UniverseMatching", "UniverseRecording", "UniverseTool",
    "freeze_universe", "load_universe", "save_universe", "INTERCHANGE_VERSION",
    "InterchangeFormatError", "InterchangeMessage", "InterchangePart",
    "InterchangeToolDefinition", "InterchangeTrace", "InterchangeWitnessedCall", "TextPart",
    "ToolCallPart", "ToolCallResponsePart", "build_envelope", "load_interchange",
    "parse_interchange", "ImportResult", "write_imported_scenario",
)

EXPECTED_SPI_EXPORTS = (
    "AgentConfig", "AgentHandle", "AgentRuntime", "Message", "ModelSpec", "Response",
    "RunnerMCPConfigurableRuntime", "SamplingConfig", "SurfaceIntrospectableAgentHandle",
    "Hook", "HookArtifact", "HookContext", "FailureInjectableMCPHandle", "MCPCall",
    "MCPHandle", "MCPServer", "MCPSpec", "ToolDefinitionIntrospectableMCPHandle",
    "ToolIntrospectableMCPHandle", "RuntimePlugin", "ReferenceCapableAgentRuntime",
    "ReferenceCase", "ReferenceDecision", "ReferenceKind", "ReferenceToolCall", "StateProbe",
)

EXPECTED_DATACLASS_FIELDS = {
    Scenario: (
        "name", "prompt", "target_facts", "target_numbers", "requires_tool_use",
        "forbidden_facts", "outcome_fn", "must_call", "forbidden_calls", "order_matters",
        "trajectory_checks", "user_turns", "preconditions", "requires_tools", "requires_files",
        "policies", "gate_layers", "perturbations", "failure_cost", "variance_allowed", "tags",
        "reference_cases",
    ),
    Trace: (
        "scenario_id", "agent_id", "variant_id", "model", "quant", "sampler", "started_at",
        "finished_at", "turns", "tool_schema_hash", "worker_warnings", "mcp_calls",
        "observations", "surface", "run_id",
    ),
    Score: ("outcome", "trajectory", "constraint", "integrity", "failure_cost"),
    AgentConfig: (
        "agent_id", "variant_id", "system_prompt", "persona_doc", "skills", "mcp_servers",
        "model", "sampling",
    ),
    SamplingConfig: ("temperature", "top_p", "tool_choice", "max_tokens"),
    ReferenceToolCall: ("name", "arguments"),
    ReferenceDecision: ("content", "tool_calls"),
    ReferenceCase: ("name", "kind", "decisions"),
    SelfTestCaseResult: ("scenario_id", "case", "verdict", "detail", "trace", "score"),
}


def test_api_root_export_snapshot() -> None:
    assert tuple(api.__all__) == EXPECTED_API_EXPORTS


def test_spi_root_export_snapshot() -> None:
    assert tuple(spi.__all__) == EXPECTED_SPI_EXPORTS


def test_public_dataclass_field_snapshot() -> None:
    actual = {cls: tuple(field.name for field in fields(cls)) for cls in EXPECTED_DATACLASS_FIELDS}
    assert actual == EXPECTED_DATACLASS_FIELDS


def test_runner_signature_shape() -> None:
    assert tuple(inspect.signature(run_scenario).parameters) == (
        "scenario", "runtime", "mcps", "config", "runs_per_scenario", "skip_reset",
        "state_probe", "hooks",
    )
    assert tuple(inspect.signature(run_matrix).parameters) == (
        "scenario", "runtime", "mcps", "base_config", "sampling_variants", "runs_per_cell",
    )
    assert tuple(inspect.signature(run_reference_case).parameters) == (
        "scenario", "runtime", "case", "mcps", "config", "state_probe",
    )
    assert inspect.signature(run_scenario).parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(run_matrix).parameters["base_config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(run_reference_case).parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY


def test_fastmcp_has_a_supported_package_import() -> None:
    from windtunnel.mcp.fastmcp import FastMCPServer, FastMCPServerConfig, LoggingFastMCP

    assert FastMCPServer.__name__ == "FastMCPServer"
    assert FastMCPServerConfig.__name__ == "FastMCPServerConfig"
    assert LoggingFastMCP.__name__ == "LoggingFastMCP"
