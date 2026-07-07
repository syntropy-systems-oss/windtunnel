"""RecordedMCPServer — serve a frozen universe as an MCP tool server.

This is the concrete Contract B adapter.  A ``*.universe.json`` file holds
tool schemas and recorded call/result pairs; ``RecordedMCPServer`` turns that
fixture into an ``MCPServer.start() -> MCPHandle`` implementation so existing
``ScenarioPack.mcp_factory`` wiring does not need a special replay path.

Replay is queryable, not scripted.  A live call first tries exact canonical
argument equality, then the fixture's keyed match tier, and only then applies
the configured divergence policy.  The default is stateless: repeated calls
with the same arguments return the same recording.  Per-tool sequence mode
consumes matching recordings once within a run and resets when the runner
calls ``reset_call_log()`` before the next run.

Divergence is evidence.  Every miss is recorded in ``call_log()`` with
``MCPCall.extra["divergence"]``; the runner drains that into
``trace.mcp_calls`` and emits the corresponding ``worker_warnings`` marker.
"""
from __future__ import annotations

import copy
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

from windtunnel.api.universe import (
    SynthesizeHook,
    Universe,
    empty_result_for,
    fail_call_result,
    find_exact_recording,
    find_keyed_recording,
    find_nearest_recording,
    load_universe,
    normalize_tool_args,
)
from windtunnel.spi.mcp_server import MCPCall, MCPHandle


class RecordedMCPHandle:
    """Live handle backed by a recorded universe.

    ``call_tool()`` is intentionally public even though the SPI only requires
    ``url`` and call-log methods: tests and in-process runtimes can exercise
    the exact same replay logic without speaking MCP over HTTP.  Network MCP
    calls go through this method too.
    """

    def __init__(
        self,
        universe: Universe,
        *,
        url: str,
        synthesize: SynthesizeHook | None = None,
    ) -> None:
        self._universe = universe
        self._url = url
        self._synthesize = synthesize
        self._lock = threading.Lock()
        self._calls: list[MCPCall] = []
        self._consumed: set[int] = set()
        self._failure_mode: str | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def universe(self) -> Universe:
        """The immutable fixture served by this handle."""
        return self._universe

    def call_log(self) -> list[MCPCall]:
        with self._lock:
            return list(self._calls)

    def reset_call_log(self) -> None:
        """Clear witnessed calls and reset sequence-mode consumption."""
        with self._lock:
            self._calls.clear()
            self._consumed.clear()

    def configure_failure_mode(self, mode: str | None) -> None:
        """Inject existing mock-MCP failure modes ahead of universe replay."""
        with self._lock:
            self._failure_mode = mode

    def served_tools(self) -> list[str]:
        """Return canonical tool names from the recorded universe."""
        return [tool.name for tool in self._universe.tools]

    def served_tool_definitions(self) -> list[dict[str, Any]]:
        """Return full tool definitions from the recorded universe.

        Manifest order, surface-visible fields only: ``mode`` is replay
        configuration, not part of what the agent sees, so it is excluded
        (a mode flip must not change Trace.tool_schema_hash).
        """
        definitions: list[dict[str, Any]] = []
        for tool in self._universe.tools:
            entry: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            if tool.result_schema is not None:
                entry["result_schema"] = tool.result_schema
            definitions.append(entry)
        return definitions

    def call_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> Any:
        """Resolve one tool call against the universe and record the result."""
        normalized_args = normalize_tool_args(args or {})
        with self._lock:
            failure_mode = self._failure_mode
        if failure_mode == "timeout":
            # Stall OUTSIDE the handle lock: timeout injection exists to
            # outlast the agent's connect_timeout, not to serialize parallel
            # tool calls or block the runner's call_log() drain for 30s.
            time.sleep(30)
        with self._lock:
            result, divergence, matched_index = self._resolve_locked(
                tool_name, normalized_args, failure_mode
            )
            extra: dict[str, Any] = {}
            if divergence is not None:
                extra["divergence"] = {
                    "policy": divergence,
                    "matched": matched_index,
                }
            call = MCPCall(
                tool_name=tool_name,
                args=copy.deepcopy(normalized_args),
                result=copy.deepcopy(result),
                timestamp_ms=time.time() * 1000,
                extra=extra,
            )
            self._calls.append(call)
            return copy.deepcopy(result)

    def _resolve_locked(
        self,
        tool_name: str,
        args: dict[str, Any],
        failure_mode: str | None,
    ) -> tuple[Any, str | None, int | None]:
        """Return ``(result, divergence_policy, matched_index)``.

        ``divergence_policy`` is ``None`` on a normal exact/keyed hit.  On a
        miss it is the effective policy, even when that policy still returns a
        recording (``nearest``).
        """
        failure_result = _injected_failure_result(failure_mode)
        if failure_result is not None:
            return failure_result, None, None

        tool_map = self._universe.tool_map
        mode = self._universe.matching.mode_for(tool_name, tool_map)
        candidates = self._candidate_indices_locked(tool_name, mode)

        idx = find_exact_recording(self._universe, tool_name, args, candidates)
        if idx is not None:
            self._consume_if_sequence_locked(idx, mode)
            return copy.deepcopy(self._universe.recordings[idx].result), None, idx

        idx = find_keyed_recording(self._universe, tool_name, args, candidates)
        if idx is not None:
            self._consume_if_sequence_locked(idx, mode)
            return copy.deepcopy(self._universe.recordings[idx].result), None, idx

        policy = self._universe.matching.policy_for(tool_name)
        if policy == "fail_call":
            return fail_call_result(tool_name, args), policy, None
        if policy == "empty":
            return empty_result_for(tool_map.get(tool_name)), policy, None
        if policy == "nearest":
            idx = find_nearest_recording(self._universe, tool_name, args, candidates)
            if idx is None:
                return fail_call_result(tool_name, args), policy, None
            self._consume_if_sequence_locked(idx, mode)
            return copy.deepcopy(self._universe.recordings[idx].result), policy, idx
        if policy == "synthesize":
            if self._synthesize is None:
                return {
                    "error": "no_synthesizer",
                    "tool": tool_name,
                    "args": args,
                }, policy, None
            return self._synthesize(tool_name, copy.deepcopy(args), self._universe), policy, None

        # Universe validation rejects unknown policies.  Keep this defensive
        # fallback so a hand-built object still fails as a tool result instead
        # of crashing the run.
        return fail_call_result(tool_name, args), str(policy), None

    def _candidate_indices_locked(self, tool_name: str, mode: str) -> list[int]:
        indices = self._universe.recording_indices_for(tool_name)
        if mode == "sequence":
            return [idx for idx in indices if idx not in self._consumed]
        return indices

    def _consume_if_sequence_locked(self, idx: int, mode: str) -> None:
        if mode == "sequence":
            self._consumed.add(idx)



class RecordedMCPServer:
    """Concrete ``MCPServer`` that serves a recorded universe fixture.

    ``universe`` may be a ``Universe`` object or a path to a
    ``*.universe.json`` file.  ``synthesize`` is the optional miss-policy hook
    from Contract B; core never generates synthesized data on its own.
    """

    def __init__(
        self,
        universe: Universe | str | Path,
        *,
        name: str = "windtunnel-recorded",
        host: str = "127.0.0.1",
        port: int = 0,
        synthesize: SynthesizeHook | None = None,
    ) -> None:
        self._universe = load_universe(universe) if isinstance(universe, str | Path) else universe
        self._name = name
        self._host = host
        self._requested_port = port
        self._synthesize = synthesize
        self._handle: RecordedMCPHandle | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> MCPHandle:
        """Start a streamable-HTTP MCP server and return its handle."""
        if self._handle is not None:
            return self._handle

        try:
            port = self._requested_port or _free_port(self._host)
        except PermissionError:
            # Some test sandboxes deny even localhost binds.  The SPI still
            # has useful in-process semantics via call_tool()/call_log(); in
            # a normal runtime environment this branch is not taken and the
            # handle URL points at a real MCP server.
            handle = RecordedMCPHandle(
                self._universe,
                url=f"recorded://{self._name}",
                synthesize=self._synthesize,
            )
            self._handle = handle
            return handle

        url = f"http://{self._host}:{port}/mcp"
        handle = RecordedMCPHandle(
            self._universe,
            url=url,
            synthesize=self._synthesize,
        )
        app = _build_mcp_app(self._name, handle)

        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - project dependency supplies uvicorn
            raise RuntimeError("uvicorn is required to start RecordedMCPServer") from exc

        config = uvicorn.Config(app, host=self._host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 5.0
        while not getattr(server, "started", False):
            if not thread.is_alive() or time.time() > deadline:
                server.should_exit = True
                thread.join(timeout=1.0)
                raise RuntimeError("RecordedMCPServer failed to start")
            time.sleep(0.01)

        self._server = server
        self._thread = thread
        self._handle = handle
        return handle

    def stop(self) -> None:
        """Stop the MCP server.  Safe to call more than once."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
        self._handle = None


def _injected_failure_result(mode: str | None) -> Any | None:
    """Map a configure_failure_mode() mode to its canned result.

    "timeout" maps to None on purpose: the stall happens in call_tool()
    before the handle lock is taken, and after the sleep the call resolves
    normally — moot to an agent whose connect_timeout already expired.
    """
    if mode == "malformed_json":
        return "INVALID_JSON{{{"
    if mode == "empty_unexpected":
        return {"result": "[]", "structuredContent": {"columns": [], "rows": []}}
    if mode == "auth_scope_denied":
        return {"error": "Permission denied: auth scope insufficient", "code": 403}
    return None


def _free_port(host: str) -> int:
    """Ask the OS for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _build_mcp_app(name: str, handle: RecordedMCPHandle) -> Any:
    """Build the low-level MCP ASGI app for a recorded handle."""
    try:
        from mcp import types
        from mcp.server import Server
        from mcp.server.fastmcp.server import StreamableHTTPASGIApp
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - project dependency supplies these
        raise RuntimeError("mcp and starlette are required to start RecordedMCPServer") from exc

    server = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
                outputSchema=tool.result_schema,
            )
            for tool in handle.universe.tools
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        result = handle.call_tool(tool_name, arguments)
        is_error = isinstance(result, dict) and "error" in result
        text = json.dumps(result, ensure_ascii=False, default=repr)
        structured = result if isinstance(result, dict) else None
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=structured,
            isError=is_error,
        )

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
    )
    streamable_http_app = StreamableHTTPASGIApp(session_manager)
    return Starlette(
        routes=[Route("/mcp", endpoint=streamable_http_app)],
        lifespan=lambda app: session_manager.run(),
    )


__all__ = [
    "RecordedMCPHandle",
    "RecordedMCPServer",
]
