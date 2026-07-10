"""Platform-agnostic scenario runner — the API/SPI junction.

run_scenario()  — run one Scenario against one runtime + mcp set, N times.
run_matrix()    — run one Scenario across a matrix of SamplingConfig variants.

Hard invariant: this module imports ONLY from windtunnel.api.* and
windtunnel.spi.* — never from windtunnel.runtimes.* or windtunnel.mcp.*.
The import-invariant test enforces this.

Design
------
Single-turn scenarios:
    Scenario.prompt is the sole user message. A fresh session_id is
    generated per run. The runner posts [{"role":"user","content":prompt}]
    to AgentHandle.send() once and scores the response.

Multi-turn scenarios:
    When Scenario.user_turns (a first-class Scenario field) is non-empty,
    it IS the full ordered user-turn list: the runner sends each entry
    sequentially, threads the SAME session_id across all turns, and
    accumulates the message history. Scenario.prompt is ignored on this
    path (convention: authors set it to a copy of the final turn so
    prompt-reading surfaces still show the scored question). Empty
    user_turns = single-turn path.

Session threading contract:
    turn 1: [user_1]
    turn 2: [user_1, assistant_1, user_2]
    turn 3: [user_1, assistant_1, user_2, assistant_2, user_3]
    Same session_id on every call — the agent accumulates context in state.db.

Perturbations:
    If scenario.perturbations is non-empty, each perturbation is applied
    to the SEED trace before scoring (see perturbations.py). The robustness
    evaluator then verifies the perturbation markers are present.

Matrix dispatch:
    run_matrix() runs the same scenario across a list of SamplingConfig
    variants, returning a dict of variant_label -> list[ScenarioRunResult].
    Each cell is scored independently; the caller can aggregate across
    cells to get the variance picture sampler-sensitivity analysis needs.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from windtunnel.api._runner.evidence import (
    capture_observations as _capture_observations,
)
from windtunnel.api._runner.evidence import (
    capture_surface as _capture_surface_impl,
)
from windtunnel.api._runner.evidence import (
    collect_mcp_evidence as _collect_mcp_evidence,
)
from windtunnel.api._runner.evidence import (
    tool_schema_hash as _tool_schema_hash_impl,
)
from windtunnel.api._runner.hooks import (
    RunHookState as _RunHookState,
)
from windtunnel.api._runner.hooks import (
    SerializedAgentHandle as _SerializedAgentHandle,
)
from windtunnel.api._runner.hooks import (
    dispatch_hooks as _dispatch_hooks,
)
from windtunnel.api._runner.messages import (
    build_messages as _build_messages,
)
from windtunnel.api._runner.messages import (
    extract_reply as _extract_reply_impl,
)
from windtunnel.api._runner.messages import (
    extract_response_worker_warnings as _extract_response_worker_warnings,
)
from windtunnel.api._runner.world import (
    bind_state_probe_workspace as _bind_state_probe_workspace,
)
from windtunnel.api._runner.world import (
    check_world_preconditions as _check_world_preconditions,
)
from windtunnel.api.aggregate import AggregateResult, ScenarioRunResult, aggregate_runs
from windtunnel.api.evaluators import (
    evaluate_constraint,
    evaluate_outcome,
    evaluate_robustness,
    evaluate_trajectory,
)
from windtunnel.api.scenario import PreSendPerturbation, Scenario
from windtunnel.api.score import LayerResult, Score
from windtunnel.api.trace import Trace, Turn
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, AgentRuntime, SamplingConfig
from windtunnel.spi.hooks import HookArtifact
from windtunnel.spi.mcp_server import MCPHandle, MCPServer
from windtunnel.spi.state_probe import StateProbe

# Compatibility attributes for callers that historically imported these
# private helpers from api.runner. Their implementations now live in the
# private runner engine.
_capture_surface = _capture_surface_impl
_extract_reply = _extract_reply_impl
_tool_schema_hash = _tool_schema_hash_impl

# ─── ScenarioResult ───────────────────────────────────────────────────────────

class ScenarioResult:
    """Result of running one Scenario N times.

    aggregate: AggregateResult across all runs (verdict, pass_rate, stddev).
    runs:      list of individual ScenarioRunResult (one per run).
    """
    def __init__(
        self,
        aggregate: AggregateResult,
        runs: list[ScenarioRunResult],
        worker_warnings: list[str] | None = None,
        hook_artifacts: list[HookArtifact] | None = None,
    ) -> None:
        self.aggregate = aggregate
        self.runs = runs
        self.worker_warnings = worker_warnings or []
        self.hook_artifacts = hook_artifacts or []

    def __repr__(self) -> str:
        return (
            f"ScenarioResult(verdict={self.aggregate.verdict!r}, "
            f"pass_rate={self.aggregate.pass_rate:.0%}, "
            f"runs={self.aggregate.total})"
        )


# ─── Core single-run driver ───────────────────────────────────────────────────

def _run_once(
    scenario: Scenario,
    handle: AgentHandle,
    mcp_handles: list[MCPHandle],
    agent_id: str,
    variant_id: str,
    model: str,
    quant: str,
    sampler: dict[str, Any],
    state_probe: StateProbe | None = None,
    hooks: Sequence[object] = (),
    hook_state: _RunHookState | None = None,
    config: AgentConfig | None = None,
) -> tuple[Trace, Score]:
    """Drive one scenario through a live AgentHandle. Return (Trace, Score).

    Handles both single-turn (Scenario.prompt) and multi-turn
    (Scenario.user_turns non-empty — prompt is ignored on that path).

    mcp_handles are passed so the runner can reset their call logs
    before each run and store the server-witnessed call log on the
    trace (trace.mcp_calls) — the evidence evaluate_trajectory prefers
    over the transcript's self-reported tool_calls.

    state_probe (optional) snapshots EXTERNAL non-MCP state into
    trace.observations, with the same per-run reset + freeze-before-score
    lifecycle as the call logs (see spi/state_probe.py).
    """
    if hooks and hook_state is None:
        hook_state = _RunHookState()
    session_id = hook_state.session_id if hook_state is not None else str(uuid.uuid4())
    started_at = datetime.now(UTC)
    turns: list[Turn] = []

    pre_send_perturbations = [
        perturbation
        for perturbation in scenario.perturbations
        if isinstance(perturbation, PreSendPerturbation)
    ]
    if pre_send_perturbations and not getattr(
        handle, "_windtunnel_consumes_full_history", True
    ):
        names = ", ".join(type(perturbation).__name__ for perturbation in pre_send_perturbations)
        raise RuntimeError(
            "runtime cannot deliver history-shaped perturbations to the model: "
            f"{names}; refusing to mark an unseen perturbation as applied"
        )

    # Reset MCP call logs before this run so trace.mcp_calls reflects
    # ONLY this run's traffic (logs accumulate across runs otherwise).
    for mcp in mcp_handles:
        mcp.reset_call_log()
    # Same contamination class for external state: wipe the fixture back to
    # its baseline so this run's observations carry only this run's mutations.
    if state_probe is not None:
        state_probe.reset()

    # Probe the prompt surface AFTER reset and BEFORE the first send, per
    # the Contract C surface-introspection timing: what a fresh session's
    # next turn would be composed from. Frozen into the trace like every
    # other evidence field.
    surface, surface_warnings = _capture_surface(handle)

    if hook_state is not None:
        _dispatch_hooks(
            hooks,
            "on_run_start",
            warning_sink=hook_state.warnings,
            artifact_sink=hook_state.artifacts,
            scenario=scenario,
            agent=config,
            run_id=hook_state.run_id,
            session_id=session_id,
            handle=handle,
        )

    # Multi-turn when user_turns is non-empty; else single-turn [prompt]
    user_turns: list[str] = scenario.user_turns or [scenario.prompt]
    responses: list[str] = []
    runtime_warnings: list[str] = list(surface_warnings)
    if hook_state is not None:
        runtime_warnings.extend(hook_state.warnings)

    for turn_idx, user_text in enumerate(user_turns):
        # Record user turn
        turns.append(Turn(
            role="user",
            content=user_text,
            tool_calls=[],
            tool_results=[],
            latency_ms=0.0,
        ))

        # Build accumulated message history
        messages = _build_messages(user_turns[: turn_idx + 1], responses)

        # Pre-send history shaping: PreSendPerturbation instances inject the
        # corrupted prior turns (wrong tool call + result, blank turn, stale
        # memory, …) into the MESSAGES the model receives — so the live model
        # actually runs its scored turn on top of them, instead of the post-hoc
        # apply(trace) the model never saw.
        # Only on the final (scored) turn of a multi-turn scenario.
        if turn_idx == len(user_turns) - 1:
            for _p in scenario.perturbations:
                if isinstance(_p, PreSendPerturbation):
                    messages = _p.shape_messages(messages, scenario)

        t0 = datetime.now(UTC)
        response = handle.send(messages, session_id)
        t1 = datetime.now(UTC)
        latency_ms = (t1 - t0).total_seconds() * 1000

        # Extract assistant content + tool_calls (shape-tolerant)
        reply_text, tool_calls = _extract_reply(response)
        runtime_warnings.extend(_extract_response_worker_warnings(response))

        turns.append(Turn(
            role="assistant",
            content=reply_text,
            tool_calls=tool_calls,
            tool_results=[],
            latency_ms=latency_ms,
        ))
        responses.append(reply_text)

    finished_at = datetime.now(UTC)

    # Collect the server-witnessed call log BEFORE scoring. Drained here
    # (not lazily in the evaluator) so the evidence is frozen into the trace
    # at the moment the run ends — re-scoring a saved trace later sees the
    # same calls the live scoring did.
    mcp_calls, mcp_warnings = _collect_mcp_evidence(mcp_handles)
    # External-state snapshot under the same freeze-before-score rule: a
    # Policy reading trace.observations during live scoring and during a
    # later offline re-score must see identical evidence.
    observations, probe_warnings = _capture_observations(state_probe)

    trace_kwargs: dict[str, Any] = {
        "scenario_id": scenario.name,
        "agent_id": agent_id,
        "variant_id": variant_id,
        "model": model,
        "quant": quant,
        "sampler": sampler,
        "started_at": started_at,
        "finished_at": finished_at,
        "turns": turns,
        "tool_schema_hash": _tool_schema_hash(mcp_handles),
        "worker_warnings": runtime_warnings + probe_warnings + mcp_warnings,
        "mcp_calls": mcp_calls,
        "observations": observations,
        "surface": surface,
    }
    if hook_state is not None:
        trace_kwargs["run_id"] = hook_state.run_id
    trace = Trace(**trace_kwargs)

    # Apply perturbations to the trace before scoring (robustness layer).
    # Pre-send perturbations were ALREADY injected into the live messages above —
    # skip their post-hoc apply() so we don't double-apply (and so the recorded
    # trace reflects what the model actually saw + did, not a re-mutation).
    for perturbation in scenario.perturbations:
        if isinstance(perturbation, PreSendPerturbation):
            # Already injected into the live messages — DON'T re-mutate the turns,
            # but record the marker so evaluate_robustness still sees it applied.
            object.__setattr__(
                trace,
                "worker_warnings",
                list(trace.worker_warnings) + [perturbation.marker],
            )
            continue
        trace = perturbation.apply(trace)

    score = Score(
        outcome=evaluate_outcome(trace, scenario),
        trajectory=evaluate_trajectory(trace, scenario),
        constraint=evaluate_constraint(trace, scenario),
        robustness=evaluate_robustness(trace, scenario),
        failure_cost=scenario.failure_cost,
    )

    if hook_state is not None:
        _dispatch_hooks(
            hooks,
            "on_run_scored",
            warning_sink=trace.worker_warnings,
            artifact_sink=hook_state.artifacts,
            scenario=scenario,
            agent=config,
            run_id=hook_state.run_id,
            session_id=session_id,
            trace=trace,
            score=score,
            handle=handle,
        )
        _dispatch_hooks(
            hooks,
            "on_run_end",
            warning_sink=trace.worker_warnings,
            artifact_sink=hook_state.artifacts,
            scenario=scenario,
            agent=config,
            run_id=hook_state.run_id,
            session_id=session_id,
            trace=trace,
            score=score,
            handle=handle,
        )

    return trace, score


# ─── Public API ───────────────────────────────────────────────────────────────

def run_scenario(
    scenario: Scenario,
    runtime: AgentRuntime,
    mcps: list[MCPServer] | None = None,
    *,
    config: AgentConfig | None = None,
    runs_per_scenario: int = 1,
    skip_reset: bool = False,
    state_probe: StateProbe | None = None,
    hooks: Sequence[object] = (),
) -> ScenarioResult:
    """Run one Scenario N times against the given runtime + mcp set.

    This is the primary entry point for scenario authors. It:
    1. Provisions an AgentHandle via runtime.provision(config).
    2. Starts all MCP servers via mcp.start().
    3. For each run:
       a. Calls handle.reset_state() (unless skip_reset=True).
       b. Calls _run_once() to drive the scenario and score it.
    4. Aggregates results across all runs.
    5. Calls handle.teardown() and mcp.stop() when done.

    Args:
        scenario:           the Scenario to run.
        runtime:            AgentRuntime implementation (RawDockerRuntime, etc.).
        mcps:               list of MCPServer implementations to start/stop.
        config:             AgentConfig for the runtime. Defaults to AgentConfig().
        runs_per_scenario:  number of runs (default 1 for smoke; 3+ for CI).
        skip_reset:         skip handle.reset_state() between runs (debug mode).
        state_probe:        optional StateProbe snapshotting external non-MCP
                            state into trace.observations per run (reset
                            before each run, captured before scoring). The
                            caller owns the probe's fixture lifecycle; the
                            runner never starts/stops it.
        hooks:              explicit lifecycle hooks, fired in activation order.

    Returns ScenarioResult with aggregate verdict + per-run details.
    """
    if runs_per_scenario < 1:
        raise ValueError("runs_per_scenario must be at least 1")
    if config is None:
        config = AgentConfig()
    if mcps is None:
        mcps = []

    mcp_handles: list[MCPHandle] = []
    handle: AgentHandle | None = None
    scenario_warnings: list[str] = []
    scenario_artifacts: list[HookArtifact] = []

    try:
        # Start MCP servers
        for mcp in mcps:
            mcp_handles.append(mcp.start())

        # Provision agent — pass already-started handles so platform runtime
        # plugins can register them into the live MCP server without
        # starting them a second time.
        handle = _SerializedAgentHandle(runtime.provision(config, mcps=mcp_handles))

        _bind_state_probe_workspace(state_probe, handle)
        _check_world_preconditions(scenario, mcp_handles, config, state_probe, handle)

        _dispatch_hooks(
            hooks,
            "on_provisioned",
            warning_sink=scenario_warnings,
            artifact_sink=scenario_artifacts,
            scenario=scenario,
            agent=config,
            handle=handle,
        )

        model = config.model.name if config.model else "unknown"
        quant = config.model.quant if config.model else "unknown"
        sampler: dict[str, Any] = {}
        if config.sampling:
            if config.sampling.temperature is not None:
                sampler["temperature"] = config.sampling.temperature
            if config.sampling.top_p is not None:
                sampler["top_p"] = config.sampling.top_p
            if config.sampling.tool_choice is not None:
                sampler["tool_choice"] = config.sampling.tool_choice

        run_results: list[ScenarioRunResult] = []

        for _ in range(runs_per_scenario):
            if not skip_reset:
                handle.reset_state()

            hook_state = _RunHookState() if hooks else None
            try:
                trace, score = _run_once(
                    scenario,
                    handle,
                    mcp_handles,
                    agent_id=config.agent_id,
                    variant_id=config.variant_id,
                    model=model,
                    quant=quant,
                    sampler=sampler,
                    state_probe=state_probe,
                    hooks=hooks,
                    hook_state=hook_state,
                    config=config,
                )
            except Exception as exc:
                # Create a minimal failed trace so aggregate still works
                now = datetime.now(UTC)
                warnings = [f"runner_error: {exc}"]
                if hook_state is not None:
                    warnings.extend(hook_state.warnings)
                trace = Trace(
                    run_id=hook_state.run_id if hook_state is not None else str(uuid.uuid4()),
                    scenario_id=scenario.name,
                    agent_id=config.agent_id,
                    variant_id=config.variant_id,
                    model=model,
                    quant=quant,
                    sampler=sampler,
                    started_at=now,
                    finished_at=now,
                    turns=[],
                    tool_schema_hash=_tool_schema_hash(mcp_handles),
                    worker_warnings=warnings,
                )
                score = Score(
                    outcome=LayerResult(passed=False, detail=f"run error: {exc}"),
                    trajectory=LayerResult(passed=False, detail="run error"),
                    constraint=LayerResult(passed=False, detail="not evaluated due run error"),
                    robustness=LayerResult(passed=False, detail="not evaluated due run error"),
                    failure_cost=scenario.failure_cost,
                )
                if hook_state is not None:
                    _dispatch_hooks(
                        hooks,
                        "on_run_scored",
                        warning_sink=trace.worker_warnings,
                        artifact_sink=hook_state.artifacts,
                        scenario=scenario,
                        agent=config,
                        run_id=hook_state.run_id,
                        session_id=hook_state.session_id,
                        trace=trace,
                        score=score,
                        handle=handle,
                    )
                    _dispatch_hooks(
                        hooks,
                        "on_run_end",
                        warning_sink=trace.worker_warnings,
                        artifact_sink=hook_state.artifacts,
                        scenario=scenario,
                        agent=config,
                        run_id=hook_state.run_id,
                        session_id=hook_state.session_id,
                        trace=trace,
                        score=score,
                        handle=handle,
                    )

            run_results.append(
                ScenarioRunResult(
                    score=score,
                    trace=trace,
                    hook_artifacts=list(hook_state.artifacts) if hook_state is not None else [],
                )
            )

        agg = aggregate_runs(run_results, variance_allowed=scenario.variance_allowed)
        _dispatch_hooks(
            hooks,
            "on_scenario_end",
            warning_sink=scenario_warnings,
            artifact_sink=scenario_artifacts,
            scenario=scenario,
            agent=config,
            aggregate=agg,
            handle=handle,
        )
        return ScenarioResult(
            aggregate=agg,
            runs=run_results,
            worker_warnings=scenario_warnings,
            hook_artifacts=scenario_artifacts,
        )

    finally:
        if handle is not None:
            try:
                handle.teardown()
            except Exception:
                pass
        for mcp_server, mcp_handle in zip(mcps, mcp_handles):
            try:
                mcp_server.stop()
            except Exception:
                pass


def run_matrix(
    scenario: Scenario,
    runtime: AgentRuntime,
    mcps: list[MCPServer] | None = None,
    *,
    base_config: AgentConfig | None = None,
    sampling_variants: list[tuple[str, SamplingConfig]] | None = None,
    runs_per_cell: int = 1,
) -> dict[str, ScenarioResult]:
    """Run one Scenario across a matrix of SamplingConfig variants.

    The sampler-sensitivity pattern: same scenario × multiple sampler configs to
    find which configurations are stochastic-flaky vs deterministic-pass.

    Args:
        scenario:          the Scenario to run across the matrix.
        runtime:           AgentRuntime implementation.
        mcps:              MCP servers (shared across all matrix cells).
        base_config:       base AgentConfig to clone per cell.
        sampling_variants: list of (label, SamplingConfig) tuples.
                           Each label becomes a key in the result dict.
        runs_per_cell:     runs per (scenario × variant) cell.

    Returns dict of variant_label -> ScenarioResult.
    Each ScenarioResult has aggregate + per-run details for that cell.
    """
    if base_config is None:
        base_config = AgentConfig()
    if sampling_variants is None:
        sampling_variants = [("default", SamplingConfig())]

    results: dict[str, ScenarioResult] = {}

    for label, sampling in sampling_variants:
        import dataclasses
        cell_config = dataclasses.replace(
            base_config,
            variant_id=f"{base_config.variant_id}__{label}" if base_config.variant_id else label,
            sampling=sampling,
        )
        results[label] = run_scenario(
            scenario,
            runtime,
            mcps,
            config=cell_config,
            runs_per_scenario=runs_per_cell,
        )

    return results
