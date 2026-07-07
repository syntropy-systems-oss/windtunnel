"""Surface goldens + wt surface CLI — the prevention half.

The golden's semantic core is per-segment hashes: comparison never reads
text, hash-only goldens contain zero prompt text, and diffs name the
segment that moved ("tool 'client_lookup' changed"), never "bytes
differ". `diff` informs (exit 0); `check` gates (exit 1 on any change) —
strictness lives in the invocation, not a config knob.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from windtunnel.api.surface import (
    SENSITIVITY_WARNING,
    SurfaceGoldenError,
    build_surface_golden,
    diff_surface_goldens,
    parse_surface_golden,
)
from windtunnel.runtimes.in_memory import InMemoryRuntime

SECRET_INSTRUCTION = "You are the operations assistant for Bluewing Logistics."


def _block(**overrides: Any) -> dict[str, Any]:
    block = {
        "status": "reported",
        "system_instructions": [{"type": "text", "content": SECRET_INSTRUCTION}],
        "tool_definitions": [
            {"name": "client_lookup", "description": "Look up a client record."},
            {"name": "order_status", "description": "Check an order's status."},
        ],
        "extra_segments": [
            {"name": "narration:tool_started", "content": "Checking {tool_name}…"}
        ],
    }
    block.update(overrides)
    return block


class TestBuildSurfaceGolden:
    def test_hash_only_golden_contains_no_prompt_text(self):
        golden = build_surface_golden(_block())
        serialized = json.dumps(golden)
        assert SECRET_INSTRUCTION not in serialized
        assert "Look up a client record." not in serialized
        assert "Checking {tool_name}" not in serialized

    def test_store_text_sidecar_carries_warning_and_text(self):
        golden = build_surface_golden(_block(), store_text=True)
        assert golden["sensitivity_warning"] == SENSITIVITY_WARNING
        assert golden["text"]["system_instructions"][0]["content"] == SECRET_INSTRUCTION

    def test_unavailable_surface_cannot_be_goldened(self):
        with pytest.raises(SurfaceGoldenError, match="unavailable"):
            build_surface_golden({"status": "unavailable"})

    def test_invalid_surface_cannot_be_goldened(self):
        with pytest.raises(SurfaceGoldenError, match="invalid"):
            build_surface_golden({"status": "invalid", "detail": "boom"})

    def test_duplicate_tool_names_raise(self):
        block = _block(tool_definitions=[{"name": "dup"}, {"name": "dup"}])
        with pytest.raises(SurfaceGoldenError, match="duplicate tool"):
            build_surface_golden(block)

    def test_duplicate_extra_segment_names_raise(self):
        block = _block(
            extra_segments=[
                {"name": "dup", "content": "a"},
                {"name": "dup", "content": "b"},
            ]
        )
        with pytest.raises(SurfaceGoldenError, match="duplicate extra segment"):
            build_surface_golden(block)

    def test_round_trips_through_parse(self):
        golden = build_surface_golden(_block())
        assert parse_surface_golden(json.loads(json.dumps(golden))) == golden


class TestDiffSurfaceGoldens:
    def _diff(self, old_block: dict[str, Any], new_block: dict[str, Any]) -> list[str]:
        return diff_surface_goldens(
            build_surface_golden(old_block), build_surface_golden(new_block)
        )

    def test_identical_surfaces_have_no_changes(self):
        assert self._diff(_block(), _block()) == []

    def test_text_sidecar_never_affects_comparison(self):
        old = build_surface_golden(_block(), store_text=True)
        new = build_surface_golden(_block())
        assert diff_surface_goldens(old, new) == []

    def test_system_instruction_change_is_named(self):
        changed = _block(
            system_instructions=[{"type": "text", "content": "You are someone else."}]
        )
        assert self._diff(_block(), changed) == ["system instructions changed"]

    def test_tool_description_change_names_the_tool(self):
        changed = _block(
            tool_definitions=[
                {"name": "client_lookup", "description": "Look up a client BY ID."},
                {"name": "order_status", "description": "Check an order's status."},
            ]
        )
        assert self._diff(_block(), changed) == ["tool changed: 'client_lookup'"]

    def test_tool_added_and_removed_are_named(self):
        changed = _block(
            tool_definitions=[
                {"name": "client_lookup", "description": "Look up a client record."},
                {"name": "pallet_trace", "description": "Trace a pallet."},
            ]
        )
        changes = self._diff(_block(), changed)
        assert "tool added: 'pallet_trace'" in changes
        assert "tool removed: 'order_status'" in changes

    def test_reordered_manifest_with_same_tools_is_reported(self):
        changed = _block(
            tool_definitions=list(reversed(_block()["tool_definitions"]))
        )
        assert self._diff(_block(), changed) == ["tool manifest order changed"]

    def test_extra_segment_change_names_the_segment(self):
        changed = _block(
            extra_segments=[
                {"name": "narration:tool_started", "content": "Now checking {tool_name}…"}
            ]
        )
        assert self._diff(_block(), changed) == [
            "extra segment changed: 'narration:tool_started'"
        ]

    def test_status_change_is_reported(self):
        assert self._diff(_block(), _block(status="rendered")) == [
            "surface status changed: 'reported' → 'rendered'"
        ]


# ─── wt surface CLI ───────────────────────────────────────────────────────────

# Module-level mutable surface so tests can steer the dotted-path plugin
# between record and check invocations.
PLUGIN_SURFACE: dict[str, Any] = _block()


class SurfacePlugin:
    """RuntimePlugin fixture for the module:attr dotted-path form."""

    def build(self, runtime_name: str, label: str, soul_path: str | None):
        return InMemoryRuntime(scripted_responses=["ok"], surface=dict(PLUGIN_SURFACE))


_RUNTIME_ARG = f"{__name__}:SurfacePlugin"


@pytest.fixture(autouse=True)
def _reset_plugin_surface():
    global PLUGIN_SURFACE
    original = _block()
    PLUGIN_SURFACE = _block()
    yield
    PLUGIN_SURFACE = original


class TestSurfaceCli:
    def _main(self, *argv: str) -> int:
        from windtunnel.cli import main

        return main(list(argv))

    def _golden(self, tmp_path) -> str:
        return str(tmp_path / "surface.golden.json")

    def test_record_then_check_clean(self, tmp_path, capsys):
        golden = self._golden(tmp_path)
        assert self._main(
            "surface", "record", "--runtime", _RUNTIME_ARG, "--golden", golden
        ) == 0
        assert self._main(
            "surface", "check", "--runtime", _RUNTIME_ARG, "--golden", golden
        ) == 0
        assert "matches golden" in capsys.readouterr().out

    def test_recorded_golden_is_hash_only(self, tmp_path):
        golden = self._golden(tmp_path)
        self._main("surface", "record", "--runtime", _RUNTIME_ARG, "--golden", golden)
        content = (tmp_path / "surface.golden.json").read_text()
        assert SECRET_INSTRUCTION not in content
        assert "sha256:" in content

    def test_store_text_writes_sidecar_with_warning(self, tmp_path):
        golden = self._golden(tmp_path)
        self._main(
            "surface", "record", "--runtime", _RUNTIME_ARG,
            "--golden", golden, "--store-text",
        )
        data = json.loads((tmp_path / "surface.golden.json").read_text())
        assert data["sensitivity_warning"] == SENSITIVITY_WARNING
        assert data["text"]["system_instructions"][0]["content"] == SECRET_INSTRUCTION

    def test_changed_surface_fails_check_and_names_segment(self, tmp_path, capsys):
        global PLUGIN_SURFACE
        golden = self._golden(tmp_path)
        self._main("surface", "record", "--runtime", _RUNTIME_ARG, "--golden", golden)
        PLUGIN_SURFACE = _block(
            extra_segments=[
                {"name": "narration:tool_started", "content": "CHANGED narration"}
            ]
        )
        rc = self._main("surface", "check", "--runtime", _RUNTIME_ARG, "--golden", golden)
        out = capsys.readouterr().out
        assert rc == 1
        assert "extra segment changed: 'narration:tool_started'" in out
        assert "bench run before merge" in out

    def test_diff_informs_but_exits_zero_on_change(self, tmp_path, capsys):
        global PLUGIN_SURFACE
        golden = self._golden(tmp_path)
        self._main("surface", "record", "--runtime", _RUNTIME_ARG, "--golden", golden)
        PLUGIN_SURFACE = _block(system_instructions=[{"type": "text", "content": "new"}])
        rc = self._main("surface", "diff", "--runtime", _RUNTIME_ARG, "--golden", golden)
        assert rc == 0
        assert "system instructions changed" in capsys.readouterr().out

    def test_check_fails_when_surface_becomes_unavailable(self, tmp_path, capsys):
        golden = self._golden(tmp_path)
        self._main("surface", "record", "--runtime", _RUNTIME_ARG, "--golden", golden)
        # Plain in_memory reports an honest "unavailable".
        rc = self._main("surface", "check", "--runtime", "in_memory", "--golden", golden)
        assert rc == 1
        assert "unavailable" in capsys.readouterr().out

    def test_record_refuses_unavailable_surface(self, tmp_path, capsys):
        rc = self._main(
            "surface", "record", "--runtime", "in_memory",
            "--golden", self._golden(tmp_path),
        )
        assert rc == 1
        assert "unavailable" in capsys.readouterr().err

    def test_check_without_golden_is_usage_error(self, tmp_path, capsys):
        rc = self._main(
            "surface", "check", "--runtime", _RUNTIME_ARG,
            "--golden", self._golden(tmp_path),
        )
        assert rc == 2
        assert "record" in capsys.readouterr().err

    def test_missing_action_is_usage_error(self, capsys):
        assert self._main("surface") == 2
        assert "record | diff | check" in capsys.readouterr().err
