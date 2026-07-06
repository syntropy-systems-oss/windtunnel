"""Tests for the Contract A conformance kit: golden fixtures, `wt validate`,
and the `build_envelope` reference emitter.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from windtunnel.api.interchange import (
    InterchangeFormatError,
    build_envelope,
    load_interchange,
    parse_interchange,
)
from windtunnel.cli import main

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "interchange"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"


def _wt(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run `wt` via `python -m windtunnel.cli` and return CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "windtunnel.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _valid_fixtures() -> list[Path]:
    paths = sorted(VALID_DIR.glob("*.wtin.json"))
    assert paths, "expected at least one valid golden fixture"
    return paths


def _invalid_manifest() -> dict[str, str]:
    manifest = json.loads((INVALID_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest, "expected at least one invalid golden fixture"
    return manifest


class TestGoldenFixtures:
    """Every golden fixture behaves exactly as its directory promises."""

    def test_at_least_five_valid_fixtures(self) -> None:
        assert len(_valid_fixtures()) >= 5

    def test_at_least_six_invalid_fixtures(self) -> None:
        assert len(_invalid_manifest()) >= 6

    @pytest.mark.parametrize("path", _valid_fixtures(), ids=lambda p: p.name)
    def test_valid_fixture_parses(self, path: Path) -> None:
        trace = load_interchange(path)
        assert trace.windtunnel_interchange >= 1
        assert trace.model

    @pytest.mark.parametrize("name,expected_substring", sorted(_invalid_manifest().items()))
    def test_invalid_fixture_raises_with_expected_message(
        self, name: str, expected_substring: str
    ) -> None:
        path = INVALID_DIR / name
        assert path.is_file(), f"manifest references missing fixture: {name}"
        with pytest.raises(InterchangeFormatError) as excinfo:
            load_interchange(path)
        assert expected_substring in str(excinfo.value)

    def test_manifest_only_references_existing_files(self) -> None:
        manifest_names = set(_invalid_manifest().keys())
        actual_names = {p.name for p in INVALID_DIR.glob("*.wtin.json")}
        assert manifest_names == actual_names


class TestWtValidateCli:
    """`wt validate` exit codes and per-file output."""

    def test_all_valid_exits_zero(self) -> None:
        paths = [str(p) for p in _valid_fixtures()]
        rc = main(["validate", *paths])
        assert rc == 0

    def test_prints_ok_line_per_valid_file(self, capsys: pytest.CaptureFixture) -> None:
        path = VALID_DIR / "minimal.wtin.json"
        rc = main(["validate", str(path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert f"OK {path}" in out

    def test_any_invalid_exits_one(self, capsys: pytest.CaptureFixture) -> None:
        valid_path = VALID_DIR / "minimal.wtin.json"
        invalid_path = INVALID_DIR / "missing_version.wtin.json"
        rc = main(["validate", str(valid_path), str(invalid_path)])
        assert rc == 1
        out = capsys.readouterr().out
        assert f"OK {valid_path}" in out
        assert f"INVALID {invalid_path}:" in out
        assert "windtunnel_interchange must be a positive integer" in out

    def test_no_paths_is_usage_error(self) -> None:
        result = _wt("validate")
        assert result.returncode == 2

    def test_missing_file_is_usage_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.wtin.json"
        rc = main(["validate", str(missing)])
        assert rc == 2

    def test_missing_file_among_others_is_usage_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.wtin.json"
        rc = main(["validate", str(VALID_DIR / "minimal.wtin.json"), str(missing)])
        assert rc == 2


class TestBuildEnvelope:
    """The reference emitter round-trips through `parse_interchange`."""

    def test_minimal_round_trips(self) -> None:
        envelope = build_envelope(
            model="gpt-example-1",
            messages=[
                {
                    "role": "user",
                    "parts": [{"type": "text", "content": "What is the status of order 4821?"}],
                },
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "Order 4821 is in transit."}],
                },
            ],
        )
        trace = parse_interchange(envelope)
        assert trace.model == "gpt-example-1"
        assert len(trace.messages) == 2
        assert trace.tool_definitions is None
        assert trace.witnessed_calls is None

    def test_full_shape_round_trips(self) -> None:
        envelope = build_envelope(
            model="gpt-example-1",
            provider="openai",
            otel_genai_mapping="semantic-conventions-genai@development-2026-06",
            source={"ref": "incident-2026-06-30-412"},
            messages=[
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
            ],
            tool_definitions=[
                {
                    "name": "client_lookup",
                    "description": "Look up a client by name or account id.",
                    "input_schema": {"type": "object"},
                }
            ],
            witnessed_calls=[
                {
                    "tool_name": "client_lookup",
                    "args": {"query": "Bluewing"},
                    "result": {"email": "ops@bluewing.example"},
                }
            ],
        )
        trace = parse_interchange(envelope)
        assert trace.source_ref == "incident-2026-06-30-412"
        assert trace.tool_definitions is not None
        assert trace.tool_definitions[0].name == "client_lookup"
        assert trace.witnessed_calls is not None
        assert trace.witnessed_calls[0].tool_name == "client_lookup"

    def test_validates_via_wt_validate(self, tmp_path: Path) -> None:
        envelope = build_envelope(
            model="gpt-example-1",
            messages=[
                {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            ],
        )
        path = tmp_path / "emitted.wtin.json"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        assert main(["validate", str(path)]) == 0
