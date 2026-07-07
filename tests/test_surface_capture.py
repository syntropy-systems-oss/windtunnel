"""Trace.surface capture — the forensics half of the prompt-surface work.

The runner probes describe_surface() once per run — after reset, before
the first send — and freezes the block into Trace.surface. Honest-absence
discipline throughout: None = the handle has no surface introspection at
all; {"status": "unavailable"} = probed, nothing to report;
{"status": "invalid"} = probed, response failed validation (the malformed
payload is never stored, and the run proceeds — strict gate, resilient
run).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from windtunnel.api import Scenario, run_scenario
from windtunnel.api.runner import _capture_surface
from windtunnel.api.trace import Trace, load_trace, save_trace
from windtunnel.runtimes.in_memory import InMemoryRuntime

_REPORTED = {
    "status": "reported",
    "system_instructions": [
        {"type": "text", "content": "You are the operations assistant for Bluewing Logistics."}
    ],
    "tool_definitions": [
        {"name": "client_lookup", "description": "Look up a client record."}
    ],
    "extra_segments": [
        {"name": "narration:tool_started", "content": "Checking {tool_name} for you…"}
    ],
}


def _scenario() -> Scenario:
    return Scenario(
        name="surface_capture",
        prompt="Which client ordered the pallet?",
        target_facts=[["Bluewing Logistics"]],
    )


class TestCaptureSurfaceHelper:
    def test_handle_without_capability_is_none(self):
        class Bare:
            pass

        assert _capture_surface(Bare()) == (None, [])

    def test_conforming_block_passes_through(self):
        class Handle:
            def describe_surface(self) -> dict[str, Any]:
                return dict(_REPORTED)

        block, warnings = _capture_surface(Handle())
        assert block == _REPORTED
        assert warnings == []

    def test_raising_probe_becomes_invalid_with_warning(self):
        class Handle:
            def describe_surface(self) -> dict[str, Any]:
                raise RuntimeError("probe exploded")

        block, warnings = _capture_surface(Handle())
        assert block["status"] == "invalid"
        assert "probe exploded" in block["detail"]
        assert warnings and warnings[0].startswith("surface_invalid:")

    def test_non_conforming_block_becomes_invalid(self):
        class Handle:
            def describe_surface(self) -> Any:
                return "not a block"

        block, warnings = _capture_surface(Handle())
        assert block["status"] == "invalid"
        assert warnings and warnings[0].startswith("surface_invalid:")

    def test_endpoint_reported_invalid_carries_detail_warning(self):
        class Handle:
            def describe_surface(self) -> dict[str, Any]:
                return {"status": "invalid", "detail": "missing extra_segments"}

        block, warnings = _capture_surface(Handle())
        assert block["status"] == "invalid"
        assert warnings == ["surface_invalid: missing extra_segments"]


class TestRunScenarioSurface:
    def test_scripted_surface_is_frozen_into_trace(self):
        runtime = InMemoryRuntime(
            scripted_responses=["Bluewing Logistics"], surface=dict(_REPORTED)
        )
        result = run_scenario(_scenario(), runtime)
        trace = result.runs[0].trace
        assert trace.surface == _REPORTED
        assert trace.worker_warnings == []

    def test_in_memory_default_is_honest_unavailable(self):
        # A scripted runtime composes no prompt: "unavailable", not None
        # (the capability exists; there is simply no surface to report).
        runtime = InMemoryRuntime(scripted_responses=["Bluewing Logistics"])
        result = run_scenario(_scenario(), runtime)
        assert result.runs[0].trace.surface == {"status": "unavailable"}


class TestTraceSurfaceRoundTrip:
    def _trace(self, surface: dict[str, Any] | None) -> Trace:
        return Trace(
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
            surface=surface,
        )

    def test_surface_round_trips(self, tmp_path):
        path = tmp_path / "trace.json"
        save_trace(self._trace(dict(_REPORTED)), path)
        assert load_trace(path).surface == _REPORTED

    def test_absent_surface_round_trips_as_none(self, tmp_path):
        path = tmp_path / "trace.json"
        save_trace(self._trace(None), path)
        assert load_trace(path).surface is None

    def test_old_trace_without_surface_key_loads(self):
        d = self._trace(None)._to_dict()
        del d["surface"]
        assert Trace._from_dict(d).surface is None
