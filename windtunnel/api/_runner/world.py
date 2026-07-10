"""World precondition compilation and runtime workspace binding."""
from __future__ import annotations

from pathlib import Path

from windtunnel.api.preconditions import (
    FileExists,
    Precondition,
    PreconditionContext,
    ToolAvailable,
    WorldMismatchError,
)
from windtunnel.api.scenario import Scenario
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle
from windtunnel.spi.mcp_server import MCPHandle
from windtunnel.spi.state_probe import StateProbe


def compiled_preconditions(scenario: Scenario) -> list[Precondition]:
    """Return explicit preconditions plus requires-tools/files sugar."""
    return [
        *(ToolAvailable(name) for name in scenario.requires_tools),
        *(FileExists(path) for path in scenario.requires_files),
        *scenario.preconditions,
    ]


def handle_path(handle: AgentHandle | None, attr: str) -> Path | None:
    """Read an optional filesystem path exposed by a runtime handle."""
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


def bind_state_probe_workspace(
    state_probe: StateProbe | None,
    handle: AgentHandle | None,
) -> None:
    """Best-effort binding for probes that need a runtime workspace path."""
    if state_probe is None:
        return
    workspace_dir = handle_path(handle, "workspace_dir")
    if workspace_dir is None:
        return
    bind = getattr(state_probe, "bind_workspace", None)
    if not callable(bind):
        bind = getattr(state_probe, "set_workspace_dir", None)
    if callable(bind):
        bind(workspace_dir)


def check_world_preconditions(
    scenario: Scenario,
    mcp_handles: list[MCPHandle],
    config: AgentConfig,
    state_probe: StateProbe | None,
    handle: AgentHandle | None = None,
) -> None:
    """Evaluate every world precondition and raise one joined mismatch."""
    checks = compiled_preconditions(scenario)
    if not checks:
        return
    context = PreconditionContext(
        mcp_handles=mcp_handles,
        state_probe=state_probe,
        agent_config=config,
        runtime_handle=handle,
        workspace_dir=handle_path(handle, "workspace_dir"),
        workspace_template=handle_path(handle, "workspace_template"),
    )
    failures: list[str] = []
    for check in checks:
        try:
            failure = check.check(context)
        except Exception as exc:
            failure = f"check raised {exc}"
        if failure is not None:
            failures.append(f"{check!r}: {failure}")
    if failures:
        raise WorldMismatchError(scenario.name, failures)
