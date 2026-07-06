"""World preconditions - fail fast before spending an agent turn.

Scenario scoring is only meaningful when the mocked or fixture-backed world
matches the scenario's assumptions.  Preconditions are small checks the runner
executes after MCP servers are started and the runtime is provisioned, but
before reset_state() or send() for the first run.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.mcp_server import MCPHandle
from windtunnel.spi.state_probe import StateProbe


@dataclass(frozen=True)
class PreconditionContext:
    """Inputs available to a precondition check.

    mcp_handles: the live, already-started MCP handles for this scenario.
    state_probe: the optional StateProbe wired for the scenario, if any.
    agent_config: the AgentConfig passed to runtime.provision().
    """

    mcp_handles: list[MCPHandle]
    state_probe: StateProbe | None
    agent_config: AgentConfig


class Precondition(ABC):
    """A scenario-world requirement checked before the first agent turn.

    Return None when the world is acceptable.  Return a human-readable failure
    string when it is not; the runner evaluates every precondition and reports
    all failures together as a WorldMismatchError.
    """

    @abstractmethod
    def check(self, ctx: PreconditionContext) -> str | None:
        """Return None on pass, or a human-readable failure string."""
        ...


class WorldMismatchError(RuntimeError):
    """Raised when a scenario's declared world preconditions do not hold."""

    def __init__(self, scenario_name: str, failures: list[str]) -> None:
        self.scenario_name = scenario_name
        self.failures = list(failures)
        joined = "\n".join(f"- {failure}" for failure in self.failures)
        super().__init__(
            f"world preconditions failed for scenario {scenario_name!r}:\n{joined}"
        )


@dataclass(frozen=True)
class ToolAvailable(Precondition):
    """Require an MCP tool to be served by at least one live MCP handle."""

    name: str

    def check(self, ctx: PreconditionContext) -> str | None:
        if not ctx.mcp_handles:
            return f"required tool {self.name!r} but no MCP handles were started"

        inspected: list[str] = []
        uninspectable: list[str] = []
        errors: list[str] = []
        served_any: list[str] = []

        for handle in ctx.mcp_handles:
            where = _handle_where(handle)
            served_tools = getattr(handle, "served_tools", None)
            if not callable(served_tools):
                uninspectable.append(where)
                continue
            try:
                names = _coerce_tool_names(served_tools())
            except Exception as exc:  # noqa: BLE001 - report every broken handle
                errors.append(f"{where}: {exc}")
                continue
            inspected.append(f"{where}: {names}")
            served_any.extend(names)
            if any(_tool_name_matches(self.name, full) for full in names):
                return None

        parts = [f"required tool {self.name!r} was not served"]
        if inspected:
            parts.append(f"inspected handles: {'; '.join(inspected)}")
        if uninspectable:
            parts.append(
                "handles without served_tools() introspection: "
                + ", ".join(uninspectable)
            )
        if errors:
            parts.append(f"tool introspection errors: {'; '.join(errors)}")
        if served_any:
            parts.append(f"served tools seen: {sorted(set(served_any))}")
        return "; ".join(parts)


@dataclass(frozen=True)
class FileExists(Precondition):
    """Require a bench-host filesystem path to exist.

    This checks the machine running Wind Tunnel.  It is meaningful when the
    bench and agent world share that filesystem, which is common for local
    drivers; it is not proof that a remote agent container can see the path.
    """

    path: str | Path

    def check(self, ctx: PreconditionContext) -> str | None:
        path = Path(self.path)
        if path.exists():
            return None
        return f"required bench-host path does not exist: {path}"


CheckFn = Callable[[PreconditionContext], str | None | bool]


@dataclass(frozen=True)
class Check(Precondition):
    """Wrap a small function as a Precondition.

    The function may return:
      - None or True: pass
      - a string: fail with that detail
      - False: fail with the wrapper description
    """

    fn: CheckFn
    description: str

    def check(self, ctx: PreconditionContext) -> str | None:
        try:
            result = self.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - broken checks must fail closed
            return f"{self.description}: check raised {exc}"
        if result is None or result is True:
            return None
        if result is False:
            return self.description
        return str(result)


def _handle_where(handle: MCPHandle) -> str:
    try:
        url = handle.url
    except Exception:  # noqa: BLE001 - best-effort diagnostic only
        url = None
    return str(url or type(handle).__name__)


def _coerce_tool_names(raw: Any) -> list[str]:
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif hasattr(item, "name"):
            names.append(str(item.name))
        else:
            names.append(str(item))
    return names


def _tool_name_matches(canonical: str, full: str) -> bool:
    """Local copy of the evaluator's bare-name suffix rule.

    Kept local to avoid importing evaluators from this low-level authoring API:
    evaluators import Scenario, and Scenario imports this module.
    """
    return (
        full == canonical
        or full.endswith("_" + canonical)
        or full.endswith("." + canonical)
    )
