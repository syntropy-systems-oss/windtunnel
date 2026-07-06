"""FastMCPServer — reusable mock MCP base built on FastMCP.

Provides call_log() and configure_failure_mode() as first-class
primitives on top of any FastMCP tool set. Dim mock_mcp/server.py
files can use this as a base: declare tools, let the framework handle
call logging and failure injection uniformly.

Usage
-----
Dim authors extend LoggingFastMCP instead of FastMCP directly:

    from windtunnel.mcp.fastmcp.server import LoggingFastMCP, FastMCPServer

    mcp = LoggingFastMCP("windtunnel")

    @mcp.tool()
    def ops_client_lookup(query: str) -> dict:
        return synthetic_db.find_clients(query)

    if __name__ == "__main__":
        mcp.run()

From the bench (test / runner):

    server = FastMCPServer(mcp_instance=mcp, port=8080)
    handle = server.start()
    # ... run scenario ...
    calls = handle.call_log()
    assert any(c.tool_name == "ops_client_lookup" for c in calls)
    server.stop()

Architecture
------------
LoggingFastMCP wraps each registered tool in a logging interceptor
that records (tool_name, args, result, timestamp_ms) into a thread-safe
in-process list. The same instance is shared between the tool handlers
(running in the FastMCP/uvicorn thread) and the bench test thread.

FastMCPServer manages the subprocess lifecycle for the case where
the MCP server runs in a separate process (e.g. to expose a real HTTP
port). For in-process testing, InProcessMCPHandle can be used directly.

Failure injection
-----------------
configure_failure_mode() sets a module-level synthetic_db.failure_mode
string that the tool handlers check. This lifts the silent-failure dim's
MOCK_MCP_FAILURE_MODE env-var pattern to a first-class API call.

Call log shapes
---------------
args are stored in the shape they arrive: if the caller uses the
OpenAI wire shape ({"function": {"name": ..., "arguments": "..."}})
args will have that structure. If they use the flat shape ({name, args})
that's what's stored. Both shapes are preserved faithfully.

HTTP call log readback (subprocess mode)
----------------------------------------
In subprocess mode the bench process and the mock server process share no
memory, so the in-process _CallLog is invisible to the bench.  Fix: the mock
server exposes two HTTP endpoints on the same port as the MCP server:

    GET  /calls         → JSON array of recorded MCPCall dicts
    POST /calls/reset   → clears the log; returns {"ok": true}

These are injected via LoggingFastMCP.build_app() which appends them to
FastMCP's _custom_starlette_routes before run().  _SubprocessMCPHandle stores
the calls_url ("http://<host>:<port>/calls") and fetches from it in call_log()
/ reset_call_log().  On HTTP error or connection failure call_log() returns []
(not raises) so transient start-up windows don't crash the bench.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from windtunnel.spi.mcp_server import MCPCall, MCPHandle

# ─── In-process call log ──────────────────────────────────────────────────────

class _CallLog:
    """Thread-safe in-process call log shared between tool handlers and tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[MCPCall] = []
        self._failure_mode: str | None = None

    def record(self, tool_name: str, args: dict[str, Any], result: Any) -> None:
        call = MCPCall(
            tool_name=tool_name,
            args=args,
            result=result,
            timestamp_ms=time.time() * 1000,
        )
        with self._lock:
            self._calls.append(call)

    def get_calls(self) -> list[MCPCall]:
        with self._lock:
            return list(self._calls)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()

    def set_failure_mode(self, mode: str | None) -> None:
        with self._lock:
            self._failure_mode = mode

    def get_failure_mode(self) -> str | None:
        with self._lock:
            return self._failure_mode


# ─── LoggingFastMCP ───────────────────────────────────────────────────────────

class LoggingFastMCP:
    """FastMCP wrapper that adds call logging + failure injection.

    Drop-in replacement for FastMCP in dim mock_mcp/server.py files:

        # Before (original):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("windtunnel")

        # After (with logging):
        from windtunnel.mcp.fastmcp.server import LoggingFastMCP
        mcp = LoggingFastMCP("windtunnel")

    Tool registration is identical. The logging/failure machinery is
    transparent to tool implementations.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        try:
            from mcp.server.fastmcp import FastMCP as _FastMCP
            self._mcp = _FastMCP(name, **kwargs)
        except ImportError:
            self._mcp = None  # type: ignore[assignment]
        self._name = name
        self._call_log = _CallLog()
        self._served_tools: list[str] = []
        self._served_tools_lock = threading.Lock()

    def tool(self, **kwargs: Any) -> Any:
        """Decorator that registers a tool AND wraps it with call logging."""
        def decorator(fn: Any) -> Any:
            tool_name = kwargs.get("name") or fn.__name__
            with self._served_tools_lock:
                if tool_name not in self._served_tools:
                    self._served_tools.append(tool_name)

            def wrapped(*args: Any, **fkwargs: Any) -> Any:
                # Check failure mode before calling the real tool
                mode = self._call_log.get_failure_mode()
                result: Any
                if mode == "malformed_json":
                    result = "INVALID_JSON{{{"
                elif mode == "timeout":
                    # Synchronous sleep — will block the uvicorn worker thread.
                    # Set delay large enough to exceed the agent's connect_timeout.
                    time.sleep(30)
                    result = fn(*args, **fkwargs)
                elif mode == "empty_unexpected":
                    result = {"result": "[]", "structuredContent": {"columns": [], "rows": []}}
                elif mode == "auth_scope_denied":
                    result = {"error": "Permission denied: auth scope insufficient", "code": 403}
                else:
                    result = fn(*args, **fkwargs)

                # Record the call
                self._call_log.record(
                    tool_name=tool_name,
                    args=dict(zip(getattr(fn, "__code__", type("", (), {"co_varnames": ()})()).co_varnames, args)) | fkwargs,
                    result=result,
                )
                return result

            if self._mcp is not None:
                return self._mcp.tool(**kwargs)(wrapped)
            return wrapped

        return decorator

    def build_app(self) -> Any:
        """Return a Starlette ASGI app with /calls and /calls/reset injected.

        B4: injects the HTTP call-log endpoints into FastMCP's
        _custom_starlette_routes list before building the streamable-HTTP
        Starlette app.  The bench subprocess reads calls via GET /calls and
        clears them via POST /calls/reset on the same port as the MCP server.

        Returns the Starlette app (or None if mcp package is unavailable).
        """
        if self._mcp is None:
            return None


        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        call_log = self._call_log  # captured in closure

        async def _calls_get(request: Request) -> Response:
            calls = call_log.get_calls()
            data = [
                {
                    "tool_name": c.tool_name,
                    "args": c.args,
                    "result": c.result,
                    "timestamp_ms": c.timestamp_ms,
                }
                for c in calls
            ]
            return JSONResponse(data)

        async def _calls_reset(request: Request) -> Response:
            call_log.reset()
            return JSONResponse({"ok": True})

        async def _tools_get(request: Request) -> Response:
            return JSONResponse(self.served_tools())

        # Inject into FastMCP's custom routes list (appended once per instance).
        # Guard against duplicate injection if build_app() is called multiple times.
        existing_paths = {
            getattr(r, "path", None) for r in self._mcp._custom_starlette_routes
        }
        if "/calls" not in existing_paths:
            self._mcp._custom_starlette_routes.append(
                Route("/calls", endpoint=_calls_get, methods=["GET"])
            )
        if "/calls/reset" not in existing_paths:
            self._mcp._custom_starlette_routes.append(
                Route("/calls/reset", endpoint=_calls_reset, methods=["POST"])
            )
        if "/tools" not in existing_paths:
            self._mcp._custom_starlette_routes.append(
                Route("/tools", endpoint=_tools_get, methods=["GET"])
            )

        return self._mcp.streamable_http_app()

    @property
    def calls_app(self) -> Any:
        """Alias for build_app() — satisfies the hasattr check in tests."""
        return self.build_app()

    def run(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the FastMCP server (blocking) with /calls endpoint injected.

        B4: uses build_app() to ensure /calls and /calls/reset are available
        on the same port as the MCP server so subprocess call readback works.
        """
        if self._mcp is not None:
            import uvicorn

            app = self.build_app()
            self._mcp.settings.host = host
            self._mcp.settings.port = port
            uvicorn.run(app, host=host, port=port)
        else:
            raise RuntimeError("mcp package not installed; cannot run LoggingFastMCP server")

    @property
    def call_log(self) -> _CallLog:
        return self._call_log

    def served_tools(self) -> list[str]:
        """Return canonical tool names registered on this mock server."""
        with self._served_tools_lock:
            return list(self._served_tools)


# ─── InProcessMCPHandle ───────────────────────────────────────────────────────

class InProcessMCPHandle:
    """MCPHandle backed by an in-process LoggingFastMCP instance.

    Used when the MCP server runs in the same process as the test
    (e.g. for unit tests that don't need a real HTTP port).
    """

    def __init__(self, logging_mcp: LoggingFastMCP, url: str = "http://localhost:8080/mcp") -> None:
        self._logging_mcp = logging_mcp
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def call_log(self) -> list[MCPCall]:
        return self._logging_mcp.call_log.get_calls()

    def reset_call_log(self) -> None:
        self._logging_mcp.call_log.reset()

    def configure_failure_mode(self, mode: str | None) -> None:
        self._logging_mcp.call_log.set_failure_mode(mode)

    def served_tools(self) -> list[str]:
        return self._logging_mcp.served_tools()


# ─── FastMCPServer ────────────────────────────────────────────────────────────

@dataclass
class FastMCPServerConfig:
    """Config for FastMCPServer subprocess mode.

    server_module:  importable module path that calls mcp.run() at the
                    bottom (e.g. "windtunnel.scenarios.dim_tool_affordance.mock_mcp.server").
    host:           bind host (default "0.0.0.0").
    port:           HTTP port (default 8080).
    startup_delay:  seconds to wait after starting the process before
                    the first request (default 2.0).
    """
    server_module: str
    host: str = "0.0.0.0"
    port: int = 8080
    startup_delay: float = 2.0
    extra_env: dict[str, str] = field(default_factory=dict)


class _SubprocessMCPHandle:
    """MCPHandle for a FastMCP server running in a subprocess.

    B4: call_log() fetches from the HTTP /calls endpoint on the mock server
    (same host:port as the MCP server, injected by LoggingFastMCP.build_app()).
    reset_call_log() POSTs /calls/reset.  Both are best-effort — they return
    [] / silently succeed on connection failure so transient start-up windows
    don't crash the bench.

    calls_url: "http://<host>:<port>/calls" — set by FastMCPServer.start().
    """

    def __init__(self, url: str, proc: Any, calls_url: str = "") -> None:
        self._url = url
        self._proc = proc
        self.calls_url: str = calls_url

    @property
    def url(self) -> str:
        return self._url

    def call_log(self) -> list[MCPCall]:
        """Fetch recorded calls from the mock server's /calls HTTP endpoint.

        Returns [] on any connection/HTTP error (not raises) — caller treats
        an empty log as "no calls recorded yet" rather than crashing.
        """
        if not self.calls_url:
            return []
        try:
            import urllib.request as _urllib_request
            with _urllib_request.urlopen(self.calls_url, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
            data = __import__("json").loads(raw)
            return [
                MCPCall(
                    tool_name=d["tool_name"],
                    args=d.get("args", {}),
                    result=d.get("result"),
                    timestamp_ms=d.get("timestamp_ms", 0.0),
                )
                for d in data
            ]
        except Exception:
            return []

    def reset_call_log(self) -> None:
        """POST /calls/reset to clear the recorded calls on the mock server."""
        if not self.calls_url:
            return
        reset_url = self.calls_url.rstrip("/").replace("/calls", "/calls/reset")
        if not reset_url.endswith("/reset"):
            reset_url = self.calls_url.rstrip("/") + "/reset"
        try:
            import urllib.request as _urllib_request
            req = _urllib_request.Request(reset_url, data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            with _urllib_request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    def configure_failure_mode(self, mode: str | None) -> None:
        # In-process failure mode injection not available for subprocess mode.
        # Failure mode must be set via environment variable before the subprocess
        # starts (MOCK_MCP_FAILURE_MODE in extra_env).
        pass

    def served_tools(self) -> list[str]:
        """Fetch canonical tool names from the mock server's /tools endpoint."""
        if not self.calls_url:
            return []
        tools_url = self.calls_url.rstrip("/").replace("/calls", "/tools")
        if not tools_url.endswith("/tools"):
            tools_url = self.calls_url.rstrip("/") + "/tools"
        try:
            import urllib.request as _urllib_request
            with _urllib_request.urlopen(tools_url, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
            data = __import__("json").loads(raw)
            return [str(name) for name in data]
        except Exception:
            return []


class FastMCPServer:
    """MCPServer that starts a FastMCP-based mock in-process or as a subprocess.

    In-process mode (preferred for tests):
        Provide a LoggingFastMCP instance directly via `mcp_instance`.
        No subprocess is started; the InProcessMCPHandle connects directly.

    Subprocess mode (for dims that run a real HTTP server):
        Provide a FastMCPServerConfig and the server module is started
        as a subprocess on the given port.

    Examples::

        # In-process (unit tests)
        mcp = LoggingFastMCP("windtunnel")
        server = FastMCPServer(mcp_instance=mcp)
        handle = server.start()

        # Subprocess (integration tests / dim runners)
        server = FastMCPServer(config=FastMCPServerConfig(
            server_module="windtunnel.scenarios.dim_tool_affordance.mock_mcp.server",
            port=8080,
        ))
        handle = server.start()
    """

    def __init__(
        self,
        *,
        mcp_instance: LoggingFastMCP | None = None,
        config: FastMCPServerConfig | None = None,
    ) -> None:
        if mcp_instance is None and config is None:
            raise ValueError("Must provide mcp_instance or config")
        self._mcp_instance = mcp_instance
        self._config = config
        self._proc: Any = None
        self._handle: MCPHandle | None = None

    def start(self) -> MCPHandle:
        """Start the MCP server and return a live handle."""
        if self._mcp_instance is not None:
            # In-process mode — no subprocess
            url = f"http://localhost:{self._config.port if self._config else 8080}/mcp"
            handle: MCPHandle = InProcessMCPHandle(self._mcp_instance, url=url)
            self._handle = handle
            return handle

        # Subprocess mode
        assert self._config is not None
        import os
        import sys
        env = os.environ.copy()
        # Inject MOCK_MCP_HOST and MOCK_MCP_PORT so the mock
        # subprocess binds the port that handle.url advertises.  Without this
        # the subprocess uses its own default (8080) regardless of config.port,
        # causing a "could not read upstream MCP tools" URL mismatch.
        env["MOCK_MCP_HOST"] = self._config.host
        env["MOCK_MCP_PORT"] = str(self._config.port)
        env.update(self._config.extra_env)
        self._proc = __import__("subprocess").Popen(
            [sys.executable, "-m", self._config.server_module],
            env=env,
        )
        time.sleep(self._config.startup_delay)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"FastMCPServer subprocess exited immediately "
                f"(module={self._config.server_module})"
            )
        url = f"http://{self._config.host}:{self._config.port}/mcp"
        # Wire calls_url so _SubprocessMCPHandle.call_log() fetches from
        # the /calls HTTP endpoint injected by LoggingFastMCP.build_app().
        calls_url = f"http://{self._config.host}:{self._config.port}/calls"
        handle = _SubprocessMCPHandle(url=url, proc=self._proc, calls_url=calls_url)
        self._handle = handle
        return handle

    def stop(self) -> None:
        """Stop the MCP server subprocess (if running)."""
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
