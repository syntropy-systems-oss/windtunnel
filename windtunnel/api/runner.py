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

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from windtunnel.api.aggregate import AggregateResult, ScenarioRunResult, aggregate_runs
from windtunnel.api.evaluators import (
    evaluate_constraint,
    evaluate_outcome,
    evaluate_robustness,
    evaluate_trajectory,
)
from windtunnel.api.preconditions import (
    FileExists,
    Precondition,
    PreconditionContext,
    ToolAvailable,
    WorldMismatchError,
)
from windtunnel.api.scenario import PreSendPerturbation, Scenario
from windtunnel.api.score import LayerResult, Score
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, AgentRuntime, SamplingConfig
from windtunnel.spi.mcp_server import MCPHandle, MCPServer
from windtunnel.spi.state_probe import StateProbe

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
    ) -> None:
        self.aggregate = aggregate
        self.runs = runs

    def __repr__(self) -> str:
        return (
            f"ScenarioResult(verdict={self.aggregate.verdict!r}, "
            f"pass_rate={self.aggregate.pass_rate:.0%}, "
            f"runs={self.aggregate.total})"
        )


# ─── Turn-building helper ─────────────────────────────────────────────────────

def _build_messages(
    user_turns: list[str],
    assistant_responses: list[str],
) -> list[dict[str, Any]]:
    """Interleave user turns with prior assistant responses.

    Used for multi-turn scenarios. Mirrors the pattern from
    dim_multi_turn_drift/multi_turn.py build_turn_messages().

    Args:
        user_turns:          all user turns up to (and including) the current one.
        assistant_responses: prior assistant responses (len = len(user_turns) - 1).

    Returns list of OpenAI-format message dicts.
    """
    messages: list[dict[str, Any]] = []
    for i, user_text in enumerate(user_turns):
        messages.append({"role": "user", "content": user_text})
        if i < len(assistant_responses):
            messages.append({"role": "assistant", "content": assistant_responses[i]})
    return messages


# ─── MCP call-log collection ──────────────────────────────────────────────────

def _collect_mcp_evidence(mcp_handles: list[MCPHandle]) -> tuple[list[dict[str, Any]], list[str]]:
    """Drain every handle's call_log() into trace-serializable dicts.

    Normalization decisions:
    - Dicts, not MCPCall objects — the Trace is pure-stdlib JSON; storing
      dataclass instances would break save_trace() round-trips.
    - Dict keys mirror MCPCall fields ({"tool_name", "args", "result",
      "timestamp_ms", optional "extra"}) — the same shape the /calls HTTP
      endpoint already serves, so in-process and subprocess logs look
      identical in a trace.
    - result is coerced to repr() when not JSON-serializable (in-process
      handlers may return arbitrary objects; the trace must still save).
      extra gets the same guard — it is runtime-controlled metadata.
    - Sorted by timestamp_ms across ALL handles, so a multi-server run
      yields one chronological stream — the order trajectory scoring needs.
    - Best-effort per handle: a dead/unreachable handle contributes []
      rather than failing the run (matches _SubprocessMCPHandle semantics).
    The returned warning list is derived from call-log metadata, not a new
    component channel.  Universe replay misses are witnessed calls with
    ``extra.divergence``; the runner turns that evidence into the
    ``worker_warnings`` marker that perturbations already use for
    scorer-visible run metadata.
    """
    calls: list[dict[str, Any]] = []
    warnings: list[str] = []
    for mcp in mcp_handles:
        try:
            log = mcp.call_log()
        except Exception:
            continue
        for c in log:
            result = c.result
            try:
                json.dumps(result)
            except (TypeError, ValueError):
                result = repr(result)
            call_dict = {
                "tool_name": c.tool_name,
                "args": c.args,
                "result": result,
                "timestamp_ms": c.timestamp_ms,
            }
            if c.extra:
                extra = c.extra
                try:
                    json.dumps(extra)
                except (TypeError, ValueError):
                    extra = json.loads(json.dumps(extra, default=repr))
                call_dict["extra"] = extra
                divergence = c.extra.get("divergence")
                if isinstance(divergence, dict):
                    policy = divergence.get("policy")
                    if isinstance(policy, str):
                        warnings.append(
                            f"universe_divergence: tool={c.tool_name} policy={policy}"
                        )
            calls.append(call_dict)
    calls.sort(key=lambda c: c.get("timestamp_ms") or 0.0)
    return calls, warnings


# ─── External-state observation capture ───────────────────────────────────────

def _capture_observations(
    state_probe: StateProbe | None,
) -> tuple[dict[str, Any], list[str]]:
    """Snapshot external state via the probe. Return (observations, warnings).

    Failure semantics differ from _collect_mcp_evidence on purpose: a dead
    call-log handle silently contributes [] (logs are supplementary
    evidence), but a failed capture() gets a "probe_error: ..." warning —
    a Policy reading trace.observations would otherwise fail with a bare
    KeyError that triage can't distinguish from a genuine violation.

    Non-JSON-serializable leaves are coerced via repr() (json default=)
    so a sloppy probe can't brick save_trace(); a non-dict capture()
    return is rejected with a warning rather than stored, because every
    downstream consumer indexes observations by evidence-source key.
    """
    if state_probe is None:
        return {}, []
    try:
        observations = state_probe.capture()
    except Exception as exc:
        return {}, [f"probe_error: capture failed: {exc}"]
    if not isinstance(observations, dict):
        return {}, [
            f"probe_error: capture() returned {type(observations).__name__}, expected dict",
        ]
    try:
        json.dumps(observations)
    except (TypeError, ValueError):
        # default=repr rescues non-serializable VALUES; non-str KEYS still
        # raise — degrade to a warning, never crash the run over evidence.
        try:
            observations = json.loads(json.dumps(observations, default=repr))
        except (TypeError, ValueError) as exc:
            return {}, [f"probe_error: snapshot not JSON-serializable: {exc}"]
    return observations, []


# ─── Response-shape tolerance ─────────────────────────────────────────────────

def _extract_reply(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Extract (content, tool_calls) from an AgentHandle.send() response.

    Tolerates the response shapes the SPI accepts (see
    spi/agent_runtime.py AgentHandle.send):

    - OpenAI chat-completions: {"choices": [{"message": {...}}]}
      — kept for OpenAI-compat; an empty "choices" list yields ("", []).
    - flat message:            {"content": str, "tool_calls": [...]}
      — the forward-looking minimal contract.
    - wrapped message:         {"message": {"content": ..., "tool_calls": ...}}

    Missing/None content normalizes to ""; missing/None tool_calls to [].
    """
    msg: dict[str, Any] = {}
    choices = response.get("choices")
    if choices:
        msg = choices[0].get("message") or {}
    elif isinstance(response.get("message"), dict):
        msg = response["message"]
    elif "choices" not in response:
        msg = response  # flat shape: the response IS the message
    content: str = msg.get("content") or ""
    tool_calls: list[dict[str, Any]] = msg.get("tool_calls") or []
    return content, tool_calls


def _extract_response_worker_warnings(response: dict[str, Any]) -> list[str]:
    """Return runtime-supplied warnings from a send() response, if present.

    Runtimes may need to surface scoreable agent-level failures without
    raising and without inventing assistant content. A top-level
    ``worker_warnings`` list is copied into the Trace alongside probe/MCP
    warnings; malformed warning payloads degrade to one diagnostic string.
    """
    if "worker_warnings" not in response:
        return []
    warnings = response["worker_warnings"]
    if not isinstance(warnings, list):
        return [
            "runtime_warning_shape: response worker_warnings must be a list, "
            f"got {type(warnings).__name__}",
        ]
    return [str(warning) for warning in warnings]


# ─── World preconditions ─────────────────────────────────────────────────────

def _compiled_preconditions(scenario: Scenario) -> list[Precondition]:
    """Return explicit preconditions plus requires_tools/requires_files sugar."""
    return [
        *(ToolAvailable(name) for name in scenario.requires_tools),
        *(FileExists(path) for path in scenario.requires_files),
        *scenario.preconditions,
    ]


def _handle_path(handle: AgentHandle | None, attr: str) -> Path | None:
    if handle is None:
        return None
    try:
        raw = getattr(handle, attr)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return Path(raw)
    except TypeError:
        return None


def _bind_state_probe_workspace(
    state_probe: StateProbe | None,
    handle: AgentHandle | None,
) -> None:
    """Best-effort optional hook for probes that need a runtime workspace path."""
    if state_probe is None:
        return
    workspace_dir = _handle_path(handle, "workspace_dir")
    if workspace_dir is None:
        return
    bind = getattr(state_probe, "bind_workspace", None)
    if not callable(bind):
        bind = getattr(state_probe, "set_workspace_dir", None)
    if callable(bind):
        bind(workspace_dir)


def _check_world_preconditions(
    scenario: Scenario,
    mcp_handles: list[MCPHandle],
    config: AgentConfig,
    state_probe: StateProbe | None,
    handle: AgentHandle | None = None,
) -> None:
    """Evaluate all preconditions and raise one joined mismatch error."""
    checks = _compiled_preconditions(scenario)
    if not checks:
        return

    ctx = PreconditionContext(
        mcp_handles=mcp_handles,
        state_probe=state_probe,
        agent_config=config,
        runtime_handle=handle,
        workspace_dir=_handle_path(handle, "workspace_dir"),
        workspace_template=_handle_path(handle, "workspace_template"),
    )
    failures: list[str] = []
    for check in checks:
        try:
            failure = check.check(ctx)
        except Exception as exc:  # noqa: BLE001 - fail closed, but keep checking
            failure = f"check raised {exc}"
        if failure is not None:
            failures.append(f"{check!r}: {failure}")

    if failures:
        raise WorldMismatchError(scenario.name, failures)


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
    session_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    turns: list[Turn] = []

    # Reset MCP call logs before this run so trace.mcp_calls reflects
    # ONLY this run's traffic (logs accumulate across runs otherwise).
    for mcp in mcp_handles:
        mcp.reset_call_log()
    # Same contamination class for external state: wipe the fixture back to
    # its baseline so this run's observations carry only this run's mutations.
    if state_probe is not None:
        state_probe.reset()

    # Multi-turn when user_turns is non-empty; else single-turn [prompt]
    user_turns: list[str] = scenario.user_turns or [scenario.prompt]
    responses: list[str] = []
    runtime_warnings: list[str] = []

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

    trace = Trace(
        scenario_id=scenario.name,
        agent_id=agent_id,
        variant_id=variant_id,
        model=model,
        quant=quant,
        sampler=sampler,
        started_at=started_at,
        finished_at=finished_at,
        turns=turns,
        tool_schema_hash=compute_hash(scenario.name),
        worker_warnings=runtime_warnings + probe_warnings + mcp_warnings,
        mcp_calls=mcp_calls,
        observations=observations,
    )

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

    Returns ScenarioResult with aggregate verdict + per-run details.
    """
    if config is None:
        config = AgentConfig()
    if mcps is None:
        mcps = []

    mcp_handles: list[MCPHandle] = []
    handle: AgentHandle | None = None

    try:
        # Start MCP servers
        for mcp in mcps:
            mcp_handles.append(mcp.start())

        # Provision agent — pass already-started handles so platform runtime
        # plugins can register them into the live MCP server without
        # starting them a second time.
        handle = runtime.provision(config, mcps=mcp_handles)

        _bind_state_probe_workspace(state_probe, handle)
        _check_world_preconditions(scenario, mcp_handles, config, state_probe, handle)

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
                )
            except Exception as exc:
                # Create a minimal failed trace so aggregate still works
                now = datetime.now(UTC)
                trace = Trace(
                    scenario_id=scenario.name,
                    agent_id=config.agent_id,
                    variant_id=config.variant_id,
                    model=model,
                    quant=quant,
                    sampler=sampler,
                    started_at=now,
                    finished_at=now,
                    turns=[],
                    tool_schema_hash=compute_hash(scenario.name),
                    worker_warnings=[f"runner_error: {exc}"],
                )
                score = Score(
                    outcome=LayerResult(passed=False, detail=f"run error: {exc}"),
                    trajectory=LayerResult(passed=False, detail="run error"),
                    constraint=LayerResult(passed=True, detail="no policies checked"),
                    robustness=LayerResult(passed=True, detail="no perturbations checked"),
                    failure_cost=scenario.failure_cost,
                )

            run_results.append(ScenarioRunResult(score=score, trace=trace))

        agg = aggregate_runs(run_results, variance_allowed=scenario.variance_allowed)
        return ScenarioResult(aggregate=agg, runs=run_results)

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
