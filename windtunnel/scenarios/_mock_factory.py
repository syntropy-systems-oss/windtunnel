"""Deferred FastMCP mock construction for the built-in scenario packs.

WHY the importlib indirection instead of a plain `from windtunnel.mcp...
import FastMCPServer`: the import invariant (tests/test_import_invariants.py)
forbids any STATIC import of windtunnel.mcp.* anywhere under scenarios/ —
scenarios must stay runtime-agnostic and importable without mock infra. But
a pack's mcp_factory is CLI-facing wiring (it lived in cli.py's
_build_mcp_registry before packs existed), and moving it into each dim's
__init__ is what makes a pack self-contained. importlib.import_module
squares the two: importing a dim package (or filtering scenarios, or running
the in_memory runtime, which ignores mocks) never touches windtunnel.mcp —
the binding happens only at the moment the CLI invokes the factory to wire a
mock for a plugin runtime. This is the ONE sanctioned site for that
indirection; dims compose these helpers rather than repeating it.

Conventions baked in (kept byte-identical to the old _build_mcp_registry):

- TOOL_PREFIX='' always: upstream tool names are BARE (e.g. client_lookup),
  not double-prefixed. A platform typically prepends its own integration
  prefix, e.g. ops.client_lookup → mcp_acme_ops_client_lookup. Without the
  empty prefix the mock's default TOOL_PREFIX='ops_' would yield
  ops_client_lookup → mcp_acme_ops_ops_client_lookup (double-prefix).
- Per-dim host ports (declared in each dim's PACK) are deconflicted across
  8083–8097 so a future parallel runner won't collide; only one mock runs
  per batch today. The range also stays clear of the ports a platform's own
  stack typically publishes.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


def build_fastmcp_server(
    server_module: str, port: int, extra_env: dict[str, str] | None = None
) -> Any:
    """Construct (NOT start) a FastMCPServer for one dim's mock module.

    extra_env entries are merged over the mandatory TOOL_PREFIX='' base —
    scenario-aware factories (silent_failure) use this to inject
    MOCK_MCP_FAILURE_MODE / MOCK_MCP_TIMEOUT_SECONDS per scenario.
    Returns Any (not a concrete type) for the same reason the import is
    deferred: no static windtunnel.mcp coupling from scenarios/.
    """
    fastmcp = importlib.import_module("windtunnel.mcp.fastmcp.server")
    return fastmcp.FastMCPServer(
        config=fastmcp.FastMCPServerConfig(
            server_module=server_module,
            host="0.0.0.0",
            port=port,
            startup_delay=2.0,
            extra_env={"TOOL_PREFIX": "", **(extra_env or {})},
        )
    )


def fastmcp_factory(server_module: str, port: int) -> Callable[[Any], Any]:
    """Return a scenario-IGNORING ScenarioPack.mcp_factory.

    The factory accepts the selected Scenario (the mcp_factory contract —
    see windtunnel.api.pack) but discards it: every dim except
    silent_failure serves the same canned tools regardless of which of its
    scenarios is running. Each call constructs a FRESH server so each
    run_scenario() batch gets its own lifecycle (start/stop per batch).
    """

    def _factory(scenario: Any = None) -> Any:
        return build_fastmcp_server(server_module, port)

    return _factory
