"""Trace.tool_schema_hash — real manifest identity, not a scenario-name hash.

The field promises "identity of the tool surface offered to the agent".
Before this fix it hashed scenario.name — a placeholder that became
load-bearing. These tests pin the replacement semantics:

  - hash of the canonical manifest JSON when every handle exposes metadata
  - compute_hash("[]") when there are no tool servers at all (truthful empty)
  - None when the manifest is unknown (opaque or raising handle) —
    honest absence, never a fabricated identity
"""
from __future__ import annotations

import json
from typing import Any

from windtunnel.api.runner import _tool_schema_hash
from windtunnel.api.trace import compute_hash
from windtunnel.api.universe import Universe
from windtunnel.mcp.recorded import RecordedMCPHandle
from windtunnel.spi.mcp_server import MCPCall


class _DefinitionsHandle:
    """Fake handle exposing full tool definitions."""

    url = "http://127.0.0.1:0"

    def __init__(self, definitions: list[dict[str, Any]]) -> None:
        self._definitions = definitions

    def call_log(self) -> list[MCPCall]:
        return []

    def reset_call_log(self) -> None:
        pass

    def configure_failure_mode(self, mode: str | None) -> None:
        pass

    def served_tool_definitions(self) -> list[dict[str, Any]]:
        return self._definitions


class _NamesOnlyHandle:
    """Fake handle exposing served_tools() names but no definitions."""

    url = "http://127.0.0.1:0"

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def call_log(self) -> list[MCPCall]:
        return []

    def reset_call_log(self) -> None:
        pass

    def configure_failure_mode(self, mode: str | None) -> None:
        pass

    def served_tools(self) -> list[str]:
        return self._names


class _OpaqueHandle:
    """Fake handle with no tool metadata at all."""

    url = "http://127.0.0.1:0"

    def call_log(self) -> list[MCPCall]:
        return []

    def reset_call_log(self) -> None:
        pass

    def configure_failure_mode(self, mode: str | None) -> None:
        pass


class _RaisingHandle(_NamesOnlyHandle):
    """Fake handle whose introspection raises."""

    def __init__(self) -> None:
        super().__init__([])

    def served_tools(self) -> list[str]:
        raise RuntimeError("introspection exploded")


def _defs(description: str = "Look up a client record.") -> list[dict[str, Any]]:
    return [
        {
            "name": "client_lookup",
            "description": description,
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "name": "order_status",
            "description": "Check an order's routing status.",
            "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        },
    ]


class TestToolSchemaHash:
    def test_no_handles_hashes_empty_manifest(self):
        assert _tool_schema_hash([]) == compute_hash("[]")

    def test_full_definitions_hash_is_canonical_manifest_json(self):
        handle = _DefinitionsHandle(_defs())
        expected = compute_hash(json.dumps(_defs(), sort_keys=True, ensure_ascii=False))
        assert _tool_schema_hash([handle]) == expected

    def test_same_manifest_same_hash(self):
        assert _tool_schema_hash([_DefinitionsHandle(_defs())]) == _tool_schema_hash(
            [_DefinitionsHandle(_defs())]
        )

    def test_description_change_changes_hash(self):
        before = _tool_schema_hash([_DefinitionsHandle(_defs())])
        after = _tool_schema_hash([_DefinitionsHandle(_defs("Look up a client by id."))])
        assert before != after

    def test_reordered_manifest_changes_hash(self):
        # Order-sensitive by design: a reordered manifest is a changed surface.
        forward = _tool_schema_hash([_DefinitionsHandle(_defs())])
        backward = _tool_schema_hash([_DefinitionsHandle(list(reversed(_defs())))])
        assert forward != backward

    def test_entry_key_order_is_canonicalized(self):
        shuffled = [dict(reversed(list(entry.items()))) for entry in _defs()]
        assert _tool_schema_hash([_DefinitionsHandle(_defs())]) == _tool_schema_hash(
            [_DefinitionsHandle(shuffled)]
        )

    def test_names_only_fallback(self):
        handle = _NamesOnlyHandle(["client_lookup", "order_status"])
        expected = compute_hash(
            json.dumps(
                [{"name": "client_lookup"}, {"name": "order_status"}],
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        assert _tool_schema_hash([handle]) == expected

    def test_definitions_preferred_over_names(self):
        class Both(_DefinitionsHandle):
            def served_tools(self) -> list[str]:
                return ["client_lookup", "order_status"]

        assert _tool_schema_hash([Both(_defs())]) == _tool_schema_hash(
            [_DefinitionsHandle(_defs())]
        )

    def test_opaque_handle_makes_manifest_unknown(self):
        # One opaque handle poisons the whole manifest — a partial hash
        # would claim identity over tools we never saw.
        assert _tool_schema_hash([_DefinitionsHandle(_defs()), _OpaqueHandle()]) is None

    def test_raising_introspection_makes_manifest_unknown(self):
        assert _tool_schema_hash([_RaisingHandle()]) is None

    def test_multiple_handles_concatenate_in_handle_order(self):
        a = _NamesOnlyHandle(["alpha"])
        b = _NamesOnlyHandle(["beta"])
        assert _tool_schema_hash([a, b]) != _tool_schema_hash([b, a])


class TestRecordedHandleDefinitions:
    def _universe(self, mode: str = "stateless") -> Universe:
        return Universe._from_dict(
            {
                "windtunnel_universe": 1,
                "tools": [
                    {
                        "name": "client_lookup",
                        "description": "Look up a client record.",
                        "input_schema": {"type": "object"},
                        "mode": mode,
                    }
                ],
                "recordings": [],
            }
        )

    def test_served_tool_definitions_shape(self):
        handle = RecordedMCPHandle(self._universe(), url="http://127.0.0.1:0")
        assert handle.served_tool_definitions() == [
            {
                "name": "client_lookup",
                "description": "Look up a client record.",
                "input_schema": {"type": "object"},
            }
        ]

    def test_mode_is_not_part_of_the_surface(self):
        # Replay matching config doesn't change what the agent sees —
        # flipping mode must not move tool_schema_hash.
        stateless = RecordedMCPHandle(self._universe("stateless"), url="http://127.0.0.1:0")
        sequence = RecordedMCPHandle(self._universe("sequence"), url="http://127.0.0.1:0")
        assert _tool_schema_hash([stateless]) == _tool_schema_hash([sequence])
        assert _tool_schema_hash([stateless]) == compute_hash(
            json.dumps(
                stateless.served_tool_definitions(), sort_keys=True, ensure_ascii=False
            )
        )


class TestTraceRoundTrip:
    def test_none_hash_round_trips(self, tmp_path):
        from datetime import UTC, datetime

        from windtunnel.api.trace import Trace, load_trace, save_trace

        trace = Trace(
            scenario_id="s",
            agent_id="a",
            variant_id="v",
            model="m",
            quant="q",
            sampler={},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            turns=[],
            tool_schema_hash=None,
        )
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        assert load_trace(path).tool_schema_hash is None
