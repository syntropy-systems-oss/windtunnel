"""Tests for Contract A interchange parsing and ``wt import``."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.interchange import InterchangeFormatError, parse_interchange
from windtunnel.api.universe import load_universe
from windtunnel.cli import main


def _envelope(
    *,
    witnessed_calls: list[dict[str, Any]] | None | bool = True,
    tool_definitions: list[dict[str, Any]] | None | bool = True,
    source: dict[str, Any] | None | bool = True,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "windtunnel_interchange": 1,
        "otel_genai_mapping": "semantic-conventions-genai@development-2026-06",
        "session": {"model": "gpt-example-1", "provider": "openai"},
        "messages": messages or _messages(),
    }
    if source is True:
        data["source"] = {
            "ref": "incident-2026-06-30-412",
            "system": "acme-observability",
            "captured_at": "2026-06-30T12:01:00Z",
        }
    elif source is not None and source is not False:
        data["source"] = source

    if tool_definitions is True:
        data["tool_definitions"] = [
            {
                "name": "client_lookup",
                "description": "Look up a client.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ]
    elif tool_definitions is not None and tool_definitions is not False:
        data["tool_definitions"] = tool_definitions

    if witnessed_calls is True:
        data["witnessed_calls"] = [
            {
                "tool_name": "client_lookup",
                "args": {"query": "Bluewing"},
                "result": {"email": "ops@bluewing.example"},
            }
        ]
    elif witnessed_calls is not None and witnessed_calls is not False:
        data["witnessed_calls"] = witnessed_calls

    return data


def _messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "Email on file for Bluewing?"}],
        },
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "client_lookup",
                    "arguments": {"query": "Bluewing"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": "call_1",
                    "response": {"email": "ops@bluewing.example"},
                }
            ],
        },
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "ops@bluewing.example"}],
        },
    ]


def _write_trace(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "trace.wtin.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _import_scenario(path: Path):
    spec = importlib.util.spec_from_file_location("imported_scenario", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestInterchangeValidation:
    def test_required_fields_are_validated(self) -> None:
        with pytest.raises(InterchangeFormatError, match="windtunnel_interchange"):
            parse_interchange({"session": {"model": "m"}, "messages": []})
        with pytest.raises(InterchangeFormatError, match="session"):
            parse_interchange({"windtunnel_interchange": 1, "messages": []})
        with pytest.raises(InterchangeFormatError, match="session.model"):
            parse_interchange({"windtunnel_interchange": 1, "session": {}, "messages": []})
        with pytest.raises(InterchangeFormatError, match="messages"):
            parse_interchange({"windtunnel_interchange": 1, "session": {"model": "m"}})

    def test_tolerates_unknown_additive_fields_within_v1(self) -> None:
        data = _envelope()
        data["future_top_level"] = {"ignored": True}
        data["session"]["future_session_field"] = "kept"
        data["messages"][0]["future_message_field"] = "ignored"
        data["messages"][0]["parts"][0]["future_part_field"] = "ignored"

        trace = parse_interchange(data)

        assert trace.windtunnel_interchange == 1
        assert trace.model == "gpt-example-1"
        assert trace.session["future_session_field"] == "kept"
        assert trace.source_ref == "incident-2026-06-30-412"

    def test_rejects_unknown_future_version(self) -> None:
        data = _envelope()
        data["windtunnel_interchange"] = 2
        with pytest.raises(InterchangeFormatError, match="unsupported"):
            parse_interchange(data)


class TestWtImport:
    def test_import_with_witnessed_calls_prefers_server_evidence(self, tmp_path: Path) -> None:
        trace_path = _write_trace(tmp_path, _envelope())
        out_dir = tmp_path / "imported"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        universe = load_universe(out_dir / "fixture.universe.json")
        assert universe.matching.on_miss == "fail_call"
        assert [r._to_dict() for r in universe.recordings] == [
            {
                "tool_name": "client_lookup",
                "args": {"query": "Bluewing"},
                "result": {"email": "ops@bluewing.example"},
            }
        ]

        imported_md = (out_dir / "IMPORTED.md").read_text(encoding="utf-8")
        assert "Fixture evidence source: `witnessed_calls`" in imported_md
        assert '"ref": "incident-2026-06-30-412"' in imported_md

        scenario_text = (out_dir / "scenario.py").read_text(encoding="utf-8")
        scorer_text = (out_dir / "scorer.py").read_text(encoding="utf-8")
        compile(scenario_text, str(out_dir / "scenario.py"), "exec")
        compile(scorer_text, str(out_dir / "scorer.py"), "exec")
        assert "# must_call=[" in scenario_text
        assert "#     'client_lookup'," in scenario_text
        assert "ops@bluewing.example" in scorer_text

        scenario_module = _import_scenario(out_dir / "scenario.py")
        assert scenario_module.scenario.prompt == "Email on file for Bluewing?"
        assert scenario_module.scenario.requires_tool_use is True
        assert "origin:incident-2026-06-30-412" in scenario_module.scenario.tags

    def test_unauthored_import_cannot_pass_vacuously(self, tmp_path: Path) -> None:
        # target_facts=[] would make the outcome layer pass with no authored
        # gate (every fact group is vacuously satisfied), so an imported-but-
        # unreviewed scenario would sit green in CI. The skeleton must carry
        # a placeholder group that can never match.
        trace_path = _write_trace(tmp_path, _envelope())
        out_dir = tmp_path / "imported"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        scenario = _import_scenario(out_dir / "scenario.py").scenario
        assert scenario.target_facts, "unauthored import must fail until a human authors the gate"
        assert all(group for group in scenario.target_facts)

    def test_import_without_witnessed_calls_reconstructs_pairs_by_id(self, tmp_path: Path) -> None:
        trace_path = _write_trace(tmp_path, _envelope(witnessed_calls=False))
        out_dir = tmp_path / "imported"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        universe = load_universe(out_dir / "fixture.universe.json")
        assert universe.recordings[0].tool_name == "client_lookup"
        assert universe.recordings[0].args == {"query": "Bluewing"}
        assert universe.recordings[0].result == {"email": "ops@bluewing.example"}
        assert "Fixture evidence source: `reconstructed`" in (
            out_dir / "IMPORTED.md"
        ).read_text(encoding="utf-8")

        from windtunnel.mcp.recorded import RecordedMCPServer

        server = RecordedMCPServer(universe)
        handle = server.start()
        try:
            result = handle.call_tool("client_lookup", {"query": "Bluewing"})
        finally:
            server.stop()
        assert result == {"email": "ops@bluewing.example"}

    def test_multi_turn_user_messages_emit_user_turns(self, tmp_path: Path) -> None:
        messages = [
            {"role": "user", "parts": [{"type": "text", "content": "First question"}]},
            {"role": "assistant", "parts": [{"type": "text", "content": "First answer"}]},
            {"role": "user", "parts": [{"type": "text", "content": "Final question"}]},
            {"role": "assistant", "parts": [{"type": "text", "content": "Final answer"}]},
        ]
        trace_path = _write_trace(
            tmp_path,
            _envelope(
                witnessed_calls=False,
                tool_definitions=False,
                source=False,
                messages=messages,
            ),
        )
        out_dir = tmp_path / "multi"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        scenario_module = _import_scenario(out_dir / "scenario.py")
        assert scenario_module.scenario.prompt == "Final question"
        assert scenario_module.scenario.user_turns == ["First question", "Final question"]
        assert scenario_module.scenario.tags == []

    def test_absent_tool_definitions_stub_schema_and_flag_md(self, tmp_path: Path) -> None:
        trace_path = _write_trace(tmp_path, _envelope(witnessed_calls=False, tool_definitions=False))
        out_dir = tmp_path / "stubbed"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        universe = load_universe(out_dir / "fixture.universe.json")
        assert universe.tools[0].input_schema == {
            "type": "object",
            "additionalProperties": True,
        }
        imported_md = (out_dir / "IMPORTED.md").read_text(encoding="utf-8")
        assert "Replace stubbed `input_schema` values" in imported_md
        assert "`client_lookup`" in imported_md

    def test_origin_tag_absent_when_source_ref_absent(self, tmp_path: Path) -> None:
        trace_path = _write_trace(tmp_path, _envelope(source=False))
        out_dir = tmp_path / "no_origin"

        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 0

        scenario_module = _import_scenario(out_dir / "scenario.py")
        assert not any(tag.startswith("origin:") for tag in scenario_module.scenario.tags)
        assert "Origin tag: `(none)`" in (out_dir / "IMPORTED.md").read_text(encoding="utf-8")

    def test_usage_errors_exit_2(self, tmp_path: Path) -> None:
        assert main(["import", "--trace", str(tmp_path / "missing.wtin.json"), "--out", str(tmp_path / "out")]) == 2

        invalid = _write_trace(tmp_path, {"windtunnel_interchange": 1})
        assert main(["import", "--trace", str(invalid), "--out", str(tmp_path / "invalid")]) == 2

        trace_path = _write_trace(tmp_path, _envelope())
        out_dir = tmp_path / "nonempty"
        out_dir.mkdir()
        (out_dir / "keep.txt").write_text("existing", encoding="utf-8")
        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir)]) == 2
        assert main(["import", "--trace", str(trace_path), "--out", str(out_dir), "--force"]) == 0
