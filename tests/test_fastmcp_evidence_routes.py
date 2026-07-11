"""End-to-end contract tests for built-in FastMCP evidence routes."""
from __future__ import annotations

from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from windtunnel.mcp.fastmcp.server import FastMCPServer, FastMCPServerConfig


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_recovery_subprocess_exposes_and_records_witnessed_evidence(
    unused_tcp_port: int,
) -> None:
    """The exact built-in subprocess path must serve tools and witnessed calls."""
    server = FastMCPServer(
        config=FastMCPServerConfig(
            server_module="windtunnel.scenarios.dim_recovery.mock_mcp.server",
            host="127.0.0.1",
            port=unused_tcp_port,
            startup_delay=2.0,
            extra_env={"TOOL_PREFIX": ""},
        )
    )
    handle = server.start()
    try:
        served_tools = handle.served_tools()  # type: ignore[attr-defined]
        assert "client_lookup" in served_tools

        handle.reset_call_log()
        async with streamable_http_client(handle.url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "client_lookup",
                    {"query": "Bluewing Logistics"},
                )
        assert result.isError is False

        calls = handle.call_log()
        assert [call.tool_name for call in calls] == ["client_lookup"]
        assert calls[0].args["query"] == "Bluewing Logistics"

        handle.reset_call_log()
        assert handle.call_log() == []
    finally:
        server.stop()


def test_all_builtin_subprocess_mocks_use_the_evidence_recording_factory() -> None:
    scenarios_dir = Path(__file__).parents[1] / "windtunnel" / "scenarios"
    server_paths = sorted(scenarios_dir.glob("dim_*/mock_mcp/server.py"))

    assert server_paths
    for path in server_paths:
        source = path.read_text(encoding="utf-8")
        assert "build_logging_fastmcp" in source, path
        assert "from mcp.server.fastmcp import FastMCP" not in source, path
