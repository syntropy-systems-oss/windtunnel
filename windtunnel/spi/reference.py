"""Reference-trajectory contracts for deterministic harness self-tests.

Reference cases describe model decisions, not tool results or fabricated traces.
A capable runtime substitutes those decisions at its real inference seam while
leaving the normal agent loop, tools, fixtures, probes, and evidence collection
live. Core Wind Tunnel never needs to know where that seam is implemented.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, AgentRuntime

ReferenceKind = Literal["golden", "poison"]


@dataclass(frozen=True)
class ReferenceToolCall:
    """One tool call the scripted model decision asks the live agent loop to run."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("reference tool-call name must not be empty")
        if not isinstance(self.arguments, dict):
            raise ValueError("reference tool-call arguments must be an object")
        try:
            json.dumps(self.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("reference tool-call arguments must be JSON-serializable") from exc


@dataclass(frozen=True)
class ReferenceDecision:
    """One deterministic response from the substituted model inference seam.

    Non-final decisions carry one or more tool calls. The final decision carries
    user-visible content and no tool calls. Content alongside tool calls is
    allowed for runtimes whose model protocol supports narrated tool use.
    """

    content: str = ""
    tool_calls: tuple[ReferenceToolCall, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not isinstance(self.content, str):
            raise ValueError("reference decision content must be a string")
        if not self.content and not self.tool_calls:
            raise ValueError("reference decision requires content or at least one tool call")


@dataclass(frozen=True)
class ReferenceCase:
    """A known-good (golden) or known-bad (poison) scripted trajectory.

    A golden case certifies that the scenario gate can recognize a known-correct
    execution. A poison case certifies that the gate rejects a named defect.
    Decisions model successive inference responses inside one live agent turn;
    the runtime is responsible for injecting them at its inference seam.
    """

    name: str
    kind: ReferenceKind
    decisions: tuple[ReferenceDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))
        if not self.name.strip():
            raise ValueError("reference case name must not be empty")
        if self.kind not in {"golden", "poison"}:
            raise ValueError(f"unknown reference case kind: {self.kind!r}")
        if not self.decisions:
            raise ValueError("reference case requires at least one decision")
        for decision in self.decisions[:-1]:
            if not decision.tool_calls:
                raise ValueError("non-final reference decisions require at least one tool call")
        final = self.decisions[-1]
        if final.tool_calls or not final.content.strip():
            raise ValueError(
                "final reference decision requires non-empty content and no tool calls"
            )


@runtime_checkable
class ReferenceCapableAgentRuntime(AgentRuntime, Protocol):
    """Optional runtime capability for live golden/poison execution.

    Implement this once at the runtime-service boundary. Each call must install
    ``case.decisions`` at the runtime's genuine model-inference seam and return
    an otherwise normal AgentHandle. The handle must drive the real agent loop:
    tools, fixture mutations, MCP traffic, state probes, and evidence stay live.

    The capability is deliberately per-runtime, not per-scenario configuration.
    ``provision_reference`` is called once per case so golden and poison state
    cannot leak across handles. ``handle.teardown()`` must restore or remove the
    substitution before returning.
    """

    def provision_reference(
        self,
        config: AgentConfig,
        case: ReferenceCase,
        mcps: list[Any] | None = None,
    ) -> AgentHandle:
        """Provision an agent whose model decisions come from ``case``."""
        ...
