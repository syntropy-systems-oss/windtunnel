"""Tests for Contract B recorded tool-universe fixtures.

These tests stay close to the design contract: the format is forward
tolerant, matching is deterministic and explainable, misses are recorded as
evidence, and a frozen ``call_log()`` can be served back without reshaping
tool-call records beyond argument normalization.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from windtunnel.api.runner import _run_once
from windtunnel.api.scenario import Scenario
from windtunnel.api.universe import (
    UniverseMatching,
    UniverseTool,
    freeze_universe,
    load_universe,
)
from windtunnel.mcp.recorded import RecordedMCPServer
from windtunnel.runtimes.in_memory.runtime import InMemoryRuntime
from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.mcp_server import MCPCall


def _tool(
    name: str = "client_lookup",
    *,
    result_schema: dict[str, Any] | None = None,
    mode: str = "stateless",
) -> UniverseTool:
    return UniverseTool(
        name=name,
        description="Look up a client.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "verbose": {"type": "boolean"},
            },
        },
        result_schema=result_schema or {"type": "object"},
        mode=mode,
    )


def _call(
    tool_name: str = "client_lookup",
    args: dict[str, Any] | None = None,
    result: Any | None = None,
    *,
    ts: float = 1.0,
) -> MCPCall:
    return MCPCall(
        tool_name=tool_name,
        args=args or {"query": "Bluewing Logistics"},
        result=result if result is not None else {"name": "Bluewing Logistics"},
        timestamp_ms=ts,
    )


def _server(
    *,
    recordings: list[MCPCall] | None = None,
    tools: list[UniverseTool] | None = None,
    matching: UniverseMatching | None = None,
    synthesize: Any | None = None,
) -> tuple[RecordedMCPServer, Any]:
    universe = freeze_universe(
        recordings or [_call()],
        tools or [_tool()],
        matching=matching or UniverseMatching(arg_keys={"client_lookup": ["query"]}),
    )
    server = RecordedMCPServer(universe, synthesize=synthesize)
    handle = server.start()
    return server, handle


class TestUniverseFormat:
    def test_load_forward_tolerates_unknown_fields_and_newer_version(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.universe.json"
        path.write_text(
            json.dumps(
                {
                    "windtunnel_universe": 2,
                    "producer": {"ignored": True},
                    "tools": [
                        {
                            "name": "client_lookup",
                            "description": "Look up a client.",
                            "input_schema": {"type": "object"},
                            "result_schema": {"type": "object"},
                            "future_tool_field": "ignored",
                        }
                    ],
                    "recordings": [
                        {
                            "tool_name": "client_lookup",
                            "args": {"query": "Bluewing Logistics"},
                            "result": {"name": "Bluewing Logistics"},
                            "future_recording_field": "ignored",
                        }
                    ],
                    "matching": {
                        "on_miss": "fail_call",
                        "arg_keys": {"client_lookup": ["query"]},
                        "per_tool_on_miss": {},
                        "future_matching_field": "ignored",
                    },
                }
            ),
            encoding="utf-8",
        )

        universe = load_universe(path)

        assert universe.windtunnel_universe == 2
        assert universe.tools[0].name == "client_lookup"
        assert universe.recordings[0].args == {"query": "Bluewing Logistics"}
        assert universe.matching.arg_keys == {"client_lookup": ["query"]}

    def test_freeze_normalizes_openai_wire_args_and_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.universe.json"
        call = _call(
            args={
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "client_lookup",
                    "arguments": json.dumps({"query": "Bluewing Logistics"}),
                },
            }
        )

        universe = freeze_universe(
            [call],
            [_tool()],
            matching=UniverseMatching(arg_keys={"client_lookup": ["query"]}),
            path=path,
        )
        loaded = load_universe(path)

        assert universe.recordings[0].args == {"query": "Bluewing Logistics"}
        assert loaded._to_dict() == universe._to_dict()


class TestRecordedMatching:
    def test_exact_match_uses_canonical_json_args(self) -> None:
        server, handle = _server(
            recordings=[
                _call(args={"query": "Bluewing Logistics", "filters": {"tier": "gold"}})
            ],
            matching=UniverseMatching(),
        )
        try:
            result = handle.call_tool(
                "client_lookup",
                {"filters": {"tier": "gold"}, "query": "Bluewing Logistics"},
            )
        finally:
            server.stop()

        assert result == {"name": "Bluewing Logistics"}
        assert handle.call_log()[0].extra == {}

    def test_keyed_match_ignores_extra_args(self) -> None:
        server, handle = _server()
        try:
            result = handle.call_tool(
                "client_lookup",
                {"query": "Bluewing Logistics", "verbose": True},
            )
        finally:
            server.stop()

        assert result == {"name": "Bluewing Logistics"}
        assert handle.call_log()[0].extra == {}

    def test_no_arg_keys_entry_skips_keyed_match_and_misses(self) -> None:
        server, handle = _server(matching=UniverseMatching())
        try:
            result = handle.call_tool(
                "client_lookup",
                {"query": "Bluewing Logistics", "verbose": True},
            )
        finally:
            server.stop()

        assert result == {
            "error": "no_recorded_result",
            "tool": "client_lookup",
            "args": {"query": "Bluewing Logistics", "verbose": True},
        }
        assert handle.call_log()[0].extra == {
            "divergence": {"policy": "fail_call", "matched": None}
        }

    def test_genuine_args_parameter_is_not_unwrapped(self) -> None:
        # A tool whose real parameter is named "args" must not have it
        # mistaken for the {"name": ..., "args": {...}} worker wrapper.
        server, handle = _server(
            recordings=[
                _call(args={"args": {"nested": True}}, result={"ok": True})
            ],
            matching=UniverseMatching(),
        )
        try:
            result = handle.call_tool("client_lookup", {"args": {"nested": True}})
        finally:
            server.stop()

        assert result == {"ok": True}
        assert handle.call_log()[0].args == {"args": {"nested": True}}

    def test_configure_failure_mode_preempts_replay(self) -> None:
        server, handle = _server()
        try:
            handle.configure_failure_mode("malformed_json")
            broken = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
            handle.configure_failure_mode(None)
            healed = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
        finally:
            server.stop()

        assert broken == "INVALID_JSON{{{"
        assert healed == {"name": "Bluewing Logistics"}
        # Failure injection is not a universe miss — no divergence evidence.
        assert handle.call_log()[0].extra == {}

    def test_live_openai_wire_args_match_recording(self) -> None:
        server, handle = _server()
        try:
            result = handle.call_tool(
                "client_lookup",
                {
                    "function": {
                        "name": "client_lookup",
                        "arguments": json.dumps({"query": "Bluewing Logistics"}),
                    }
                },
            )
        finally:
            server.stop()

        assert result == {"name": "Bluewing Logistics"}
        assert handle.call_log()[0].args == {"query": "Bluewing Logistics"}


class TestDivergencePolicies:
    def test_fail_call_policy_returns_structured_error(self) -> None:
        server, handle = _server(matching=UniverseMatching(on_miss="fail_call"))
        try:
            result = handle.call_tool("client_lookup", {"query": "Acme"})
        finally:
            server.stop()

        assert result == {
            "error": "no_recorded_result",
            "tool": "client_lookup",
            "args": {"query": "Acme"},
        }
        assert handle.call_log()[0].extra["divergence"]["policy"] == "fail_call"

    def test_empty_policy_uses_result_schema_shape(self) -> None:
        server, handle = _server(
            tools=[_tool(result_schema={"type": "array"})],
            matching=UniverseMatching(on_miss="empty"),
        )
        try:
            result = handle.call_tool("client_lookup", {"query": "Acme"})
        finally:
            server.stop()

        assert result == []
        assert handle.call_log()[0].extra == {
            "divergence": {"policy": "empty", "matched": None}
        }

    def test_nearest_policy_returns_best_same_tool_recording(self) -> None:
        server, handle = _server(
            recordings=[
                _call(args={"query": "Bluewing Logistics", "region": "east"}, result={"name": "east"}),
                _call(args={"query": "Bluewing Logistics", "region": "west"}, result={"name": "west"}),
            ],
            matching=UniverseMatching(on_miss="nearest"),
        )
        try:
            result = handle.call_tool(
                "client_lookup",
                {"query": "Bluewing Logistics", "region": "west", "verbose": True},
            )
        finally:
            server.stop()

        assert result == {"name": "west"}
        assert handle.call_log()[0].extra == {
            "divergence": {"policy": "nearest", "matched": 1}
        }

    def test_nearest_tie_breaks_by_recording_order(self) -> None:
        server, handle = _server(
            recordings=[
                _call(args={"query": "Bluewing Logistics", "region": "east"}, result={"name": "east"}),
                _call(args={"query": "Bluewing Logistics", "region": "west"}, result={"name": "west"}),
            ],
            matching=UniverseMatching(on_miss="nearest"),
        )
        try:
            result = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
        finally:
            server.stop()

        assert result == {"name": "east"}
        assert handle.call_log()[0].extra["divergence"]["matched"] == 0

    def test_synthesize_policy_uses_user_hook(self) -> None:
        def synthesize(tool_name: str, args: dict[str, Any], universe: Any) -> dict[str, Any]:
            return {
                "tool": tool_name,
                "query": args["query"],
                "known_recordings": len(universe.recordings),
            }

        server, handle = _server(
            matching=UniverseMatching(on_miss="synthesize"),
            synthesize=synthesize,
        )
        try:
            result = handle.call_tool("client_lookup", {"query": "Acme"})
        finally:
            server.stop()

        assert result == {
            "tool": "client_lookup",
            "query": "Acme",
            "known_recordings": 1,
        }
        assert handle.call_log()[0].extra == {
            "divergence": {"policy": "synthesize", "matched": None}
        }


class TestReplayState:
    def test_stateless_repeat_returns_same_recording(self) -> None:
        server, handle = _server(
            recordings=[
                _call(args={"query": "Bluewing Logistics"}, result={"rank": 1}),
                _call(args={"query": "Bluewing Logistics"}, result={"rank": 2}),
            ],
            matching=UniverseMatching(),
        )
        try:
            first = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
            second = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
        finally:
            server.stop()

        assert first == {"rank": 1}
        assert second == {"rank": 1}

    def test_sequence_mode_consumes_once_and_resets_with_call_log(self) -> None:
        server, handle = _server(
            tools=[_tool(mode="sequence")],
            recordings=[
                _call(args={"query": "Bluewing Logistics"}, result={"rank": 1}),
                _call(args={"query": "Bluewing Logistics"}, result={"rank": 2}),
            ],
            matching=UniverseMatching(on_miss="fail_call"),
        )
        try:
            first = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
            second = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
            miss = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
            handle.reset_call_log()
            reset_first = handle.call_tool("client_lookup", {"query": "Bluewing Logistics"})
        finally:
            server.stop()

        assert first == {"rank": 1}
        assert second == {"rank": 2}
        assert miss["error"] == "no_recorded_result"
        assert reset_first == {"rank": 1}


class TestDivergenceEvidence:
    def test_miss_lands_in_call_log_extra_and_worker_warning(self) -> None:
        server, handle = _server(matching=UniverseMatching(on_miss="empty"))
        try:
            handle.call_tool("client_lookup", {"query": "Acme"})

            trace, _score = _run_once(
                Scenario(
                    name="universe_divergence",
                    prompt="Return ok.",
                    target_facts=[["ok"]],
                ),
                InMemoryRuntime(["ok"]).provision(AgentConfig()),
                [handle],
                agent_id="agent",
                variant_id="default",
                model="model",
                quant="unknown",
                sampler={},
            )
        finally:
            server.stop()

        assert trace.mcp_calls == []

        # _run_once resets logs before driving the scenario. Exercise the
        # runner warning path with a handle that has a divergence during run.
        server, handle = _server(matching=UniverseMatching(on_miss="empty"))
        try:
            runtime_handle = _CallingRuntimeHandle(handle)
            trace, _score = _run_once(
                Scenario(
                    name="universe_divergence",
                    prompt="Use the tool.",
                    target_facts=[["done"]],
                ),
                runtime_handle,
                [handle],
                agent_id="agent",
                variant_id="default",
                model="model",
                quant="unknown",
                sampler={},
            )
        finally:
            server.stop()

        assert trace.mcp_calls[0]["extra"] == {
            "divergence": {"policy": "empty", "matched": None}
        }
        assert trace.worker_warnings == [
            "universe_divergence: tool=client_lookup policy=empty"
        ]


class TestFreezeUniverseServeRoundTrip:
    def test_call_log_to_file_to_load_to_recorded_server(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.universe.json"
        call_log = [
            _call(
                args={"name": "client_lookup", "args": {"query": "Bluewing Logistics"}},
                result={"email": "ops@bluewing.example"},
            )
        ]

        freeze_universe(
            call_log,
            [_tool()],
            matching=UniverseMatching(arg_keys={"client_lookup": ["query"]}),
            path=path,
        )
        server = RecordedMCPServer(load_universe(path))
        handle = server.start()
        try:
            result = handle.call_tool(
                "client_lookup",
                {"query": "Bluewing Logistics", "verbose": True},
            )
        finally:
            server.stop()

        assert result == {"email": "ops@bluewing.example"}


class _CallingRuntimeHandle:
    """AgentHandle test double that makes a recorded MCP call during send()."""

    def __init__(self, mcp_handle: Any) -> None:
        self._mcp_handle = mcp_handle

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self._mcp_handle.call_tool("client_lookup", {"query": "Acme"})
        return {"content": "done", "tool_calls": []}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass
