"""MCPServer + MCPHandle SPI — the mock-MCP contract.

Runtime/scenario authors implement these Protocols to provide injectable
MCP tool servers that the bench can start, inspect, and tear down per
batch.

The key insight: call_log() is a FIRST-CLASS introspection primitive.
Scenarios assert against it to verify trajectory (right tools called
with right args) and constraint (no forbidden tool calls). Without it,
trajectory scoring would rely only on what the agent reports it did —
which is exactly the "silent failure" class this bench exists to catch.

MCPCall preserves args in BOTH the OpenAI wire shape AND the flat
{name, args} shape (worker backends emit one or the other).
This ensures the bench is robust to whichever wire format the runtime
happens to emit.

Hierarchy
---------
MCPServer.start() -> MCPHandle
    Starts the server process/container. Returns a live handle.

MCPHandle
    Live handle to a running MCP server.

    url: the HTTP URL the agent should be configured to use.
    served_tools() -> list[str] (optional)
        Best-effort metadata: canonical tool names this handle serves.
        Used by world preconditions when available.
    served_tool_definitions() -> list[dict] (optional)
        Richer metadata: full tool definitions ({name, description,
        input_schema, result_schema?}) as offered to the agent. Used to
        compute Trace.tool_schema_hash when available.
    call_log() -> list[MCPCall]
        Every tool call routed through this server since last reset.
    reset_call_log() -> None
        Clear the log between scenarios.
    configure_failure_mode(mode) -> None
        Inject failure modes for the silent-failure dim (timeout,
        malformed JSON, empty result). None = normal operation.

MCPServer.stop() -> None
    Stops the server process/container.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from windtunnel.spi.agent_runtime import MCPSpec as MCPSpec

# ─── MCPCall ─────────────────────────────────────────────────────────────────

@dataclass
class MCPCall:
    """Record of one tool call routed through an MCP server.

    tool_name:   the name of the tool that was called (e.g. "ops_client_lookup").
    args:        the arguments dict as received — may be in OpenAI wire shape
                 {"function": {"name": ..., "arguments": "..."}} OR flat
                 shape {"name": ..., "args": {...}}. Both shapes are preserved
                 faithfully.
    result:      the value returned to the caller (serialized as str or dict).
    timestamp_ms: wall-clock milliseconds since Unix epoch when the call was made.
    """
    tool_name: str
    args: dict[str, Any]
    result: Any
    timestamp_ms: float
    # Extra metadata (optional, runtime-specific)
    extra: dict[str, Any] = field(default_factory=dict)


# ─── Protocols ───────────────────────────────────────────────────────────────

@runtime_checkable
class MCPHandle(Protocol):
    """Live handle to a running MCP server.

    Scenarios use call_log() to assert trajectory + constraint:
        assert any(c.tool_name == "ops_client_lookup" for c in mcp.call_log())

    Implementers: FastMCPServer.MCPHandleImpl, InMemoryMCPHandle (test fixture).
    """

    @property
    def url(self) -> str:
        """The HTTP URL the agent should be configured to use.

        Format: "http://<host>:<port>/mcp" (or path variant).
        This value is passed to AgentConfig.mcp_servers[*].url.
        """
        ...

    def call_log(self) -> list[MCPCall]:
        """Return every tool call routed through this server since last reset.

        Order: chronological by timestamp_ms.
        Thread-safe: implementations must guard the log with a lock if
        the MCP server serves concurrent requests.

        Used by scenarios for trajectory scoring:
            calls = handle.call_log()
            assert calls[0].tool_name == "ops_client_lookup"
            assert calls[1].tool_name == "ops_order_query"
        """
        ...

    def reset_call_log(self) -> None:
        """Clear the call log.

        Called by the runner between scenarios (after reset_state() on the
        AgentHandle) so each scenario gets a fresh log. Idempotent.
        """
        ...

    def configure_failure_mode(self, mode: str | None) -> None:
        """Inject a failure mode into the server for the next scenario.

        Lifts the silent-failure dim's MOCK_MCP_FAILURE_MODE env-var pattern to the SPI
        so failure injection is first-class and testable.

        mode:
          None                — normal operation (reset to clean state)
          "malformed_json"    — return invalid JSON for every tool call
          "timeout"           — sleep past the agent's connect_timeout
          "empty_unexpected"  — return empty results even when data exists
          "auth_scope_denied" — return a 403/permission-denied error

        Implementers: store mode and check it in each tool handler.
        """
        ...


@runtime_checkable
class ToolIntrospectableMCPHandle(Protocol):
    """Optional MCPHandle extension for served-tool metadata.

    This stays separate from MCPHandle so existing third-party handles remain
    conformant.  Implement it when the server can report the canonical bare
    tool names it exposes; ToolAvailable preconditions use it to fail fast
    when a scenario's expected world is not actually mounted.
    """

    def served_tools(self) -> list[str]:
        """Return canonical tool names served by this handle."""
        ...


@runtime_checkable
class ToolDefinitionIntrospectableMCPHandle(Protocol):
    """Optional MCPHandle extension for full tool-definition metadata.

    Separate from ToolIntrospectableMCPHandle for the same reason that one
    is separate from MCPHandle: existing handles remain conformant.
    Implement it when the server knows the complete definitions it offers
    the agent — name, description, and schemas — not just the names.
    Trace.tool_schema_hash prefers this over served_tools() because a
    changed tool description is a changed tool surface even when every
    name is identical.
    """

    def served_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions as offered to the agent, in manifest order.

        Entries are plain dicts: {"name": str, "description": str,
        "input_schema": dict, "result_schema": dict (optional)}. Only
        surface-visible fields belong here — server-side replay/matching
        configuration does not change what the agent sees and must be
        excluded.
        """
        ...


@runtime_checkable
class MCPServer(Protocol):
    """Factory that starts/stops a mock MCP server for one test batch.

    Implementers: FastMCPServer, any custom per-dim mock server.

    Lifecycle:
        handle = server.start()          # called once per batch
        handle.configure_failure_mode(mode)
        handle.call_log()                # inspect between scenarios
        handle.reset_call_log()          # clear between scenarios
        server.stop()                    # called once after all scenarios
    """

    def start(self) -> MCPHandle:
        """Start the MCP server and return a live handle.

        May perform expensive operations: container start, port binding,
        tool registration. Called once per batch.

        Returns an MCPHandle with a live url ready to accept MCP calls.
        Raises RuntimeError if the server fails to start.
        """
        ...

    def stop(self) -> None:
        """Stop the MCP server and release resources.

        Called once when the batch is finished. Must be idempotent —
        safe to call if never started or already stopped.
        """
        ...
