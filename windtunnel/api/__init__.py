"""Wind Tunnel API — public surface for scenario authors.

Import from here; never from windtunnel.runtimes.* or windtunnel.mcp.*.

Example::

    from windtunnel.api import Scenario, Trace, run_scenario
    from windtunnel.api.evaluators import evaluate_outcome
"""
from windtunnel.api.aggregate import AggregateResult, ScenarioRunResult, aggregate_runs
from windtunnel.api.canary import CanaryResult, run_reset_canary
from windtunnel.api.evaluators import (
    evaluate_constraint,
    evaluate_outcome,
    evaluate_robustness,
    evaluate_trajectory,
    tool_name_matches,
)
from windtunnel.api.importer import ImportResult, write_imported_scenario
from windtunnel.api.interchange import (
    InterchangeFormatError,
    InterchangeMessage,
    InterchangePart,
    InterchangeToolDefinition,
    InterchangeTrace,
    InterchangeWitnessedCall,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
    build_envelope,
    load_interchange,
    parse_interchange,
)
from windtunnel.api.pack import ScenarioPack
from windtunnel.api.perturbations import (
    BlankAssistantContent,
    CorruptPriorAssistantTurn,
    FallbackRenderLeak,
    InjectPaginationTruncation,
    InjectSchemaRejectedCall,
    InjectStaleMemory,
    InjectWrongPriorToolCall,
    MalformedToolCall,
    ToolReturnsEmptyUnexpected,
    ToolReturnsMalformed,
    ToolReturnsMalformedJson,
    ToolTimeout,
    ToolTimeoutPerScenario,
)
from windtunnel.api.preconditions import (
    Check,
    FileExists,
    Precondition,
    PreconditionContext,
    ToolAvailable,
    WorldMismatchError,
)
from windtunnel.api.replay import GenerateFn, replay
from windtunnel.api.runner import ScenarioResult, run_scenario
from windtunnel.api.scenario import (
    NumberFact,
    Perturbation,
    Policy,
    PreSendPerturbation,
    Scenario,
    TrajectoryCheck,
)
from windtunnel.api.score import FailureCost, LayerResult, Score, Verdict
from windtunnel.api.scorers import (
    all_of,
    any_of,
    llm_judge,
    no_divergence,
    observation,
    substantiated_by_tools,
)
from windtunnel.api.state_reset import StateResetConfig, reset_state_db
from windtunnel.api.trace import (
    Hash,
    Trace,
    Turn,
    compute_hash,
    load_trace,
    save_trace,
    storage_path,
)
from windtunnel.api.universe import (
    SynthesizeHook,
    Universe,
    UniverseFormatError,
    UniverseMatching,
    UniverseRecording,
    UniverseTool,
    freeze_universe,
    load_universe,
    save_universe,
)

__all__ = [
    # trace
    "Hash", "Trace", "Turn", "compute_hash", "load_trace", "save_trace", "storage_path",
    # score
    "FailureCost", "LayerResult", "Score", "Verdict",
    # scenario
    "NumberFact", "Perturbation", "Policy", "PreSendPerturbation", "Scenario",
    "TrajectoryCheck",
    # preconditions
    "Check", "FileExists", "Precondition", "PreconditionContext", "ToolAvailable",
    "WorldMismatchError",
    # scorers
    "all_of", "any_of", "observation", "llm_judge", "substantiated_by_tools",
    "no_divergence",
    # pack
    "ScenarioPack",
    # evaluators
    "evaluate_outcome", "evaluate_trajectory", "evaluate_constraint", "evaluate_robustness",
    "tool_name_matches",
    # perturbations
    "CorruptPriorAssistantTurn", "InjectStaleMemory", "ToolTimeout", "ToolReturnsMalformed",
    "BlankAssistantContent", "FallbackRenderLeak", "MalformedToolCall",
    "InjectWrongPriorToolCall", "InjectSchemaRejectedCall", "InjectPaginationTruncation",
    "ToolReturnsMalformedJson", "ToolTimeoutPerScenario", "ToolReturnsEmptyUnexpected",
    # aggregate
    "AggregateResult", "ScenarioRunResult", "aggregate_runs",
    # canary
    "CanaryResult", "run_reset_canary",
    # runner
    "ScenarioResult", "run_scenario",
    # replay
    "GenerateFn", "replay",
    # state_reset
    "StateResetConfig", "reset_state_db",
    # universe
    "SynthesizeHook", "Universe", "UniverseFormatError", "UniverseMatching",
    "UniverseRecording", "UniverseTool", "freeze_universe", "load_universe",
    "save_universe",
    # interchange/import
    "InterchangeFormatError", "InterchangeMessage", "InterchangePart",
    "InterchangeToolDefinition", "InterchangeTrace", "InterchangeWitnessedCall",
    "TextPart", "ToolCallPart", "ToolCallResponsePart", "build_envelope",
    "load_interchange", "parse_interchange", "ImportResult", "write_imported_scenario",
]
