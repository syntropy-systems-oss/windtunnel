from __future__ import annotations

import json
from typing import Any

from windtunnel.api.runner import run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.hooks.state_probe import StateProbeHook
from windtunnel.spi.agent_runtime import AgentConfig


class _MemoryProbe:
    def __init__(self) -> None:
        self.files: list[str] = []

    def capture(self) -> dict[str, Any]:
        return {"files": list(self.files)}

    def reset(self) -> None:
        self.files.clear()


class _ProbeBackedHook(StateProbeHook):
    def capture_state(self) -> dict[str, Any]:
        return self._capture_from_probe()


class _StateHandle:
    def __init__(self, probe: _MemoryProbe, *, reset_clears: bool) -> None:
        self.probe = probe
        self.reset_clears = reset_clears
        self.sends = 0

    def reset_state(self) -> None:
        if self.reset_clears:
            self.probe.reset()

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self.sends += 1
        self.probe.files.append(f"run-artifact-{self.sends}")
        return {"content": "ok"}

    def teardown(self) -> None:
        pass


class _StateRuntime:
    def __init__(self, probe: _MemoryProbe, *, reset_clears: bool) -> None:
        self.probe = probe
        self.reset_clears = reset_clears

    def provision(self, config: AgentConfig, mcps: list | None = None) -> _StateHandle:
        return _StateHandle(self.probe, reset_clears=self.reset_clears)


def _scenario(name: str = "state_probe_case") -> Scenario:
    return Scenario(name=name, prompt="say ok", target_facts=[["ok"]])


def test_state_probe_first_run_establishes_baseline_without_warning() -> None:
    probe = _MemoryProbe()
    result = run_scenario(
        _scenario(),
        _StateRuntime(probe, reset_clears=False),
        hooks=[_ProbeBackedHook(probe)],
    )

    assert result.aggregate.verdict == "PASS"
    assert result.runs[0].trace.worker_warnings == []
    assert result.runs[0].hook_artifacts == []


def test_state_probe_leak_between_runs_warns_and_emits_violation_artifact() -> None:
    probe = _MemoryProbe()
    result = run_scenario(
        _scenario(),
        _StateRuntime(probe, reset_clears=False),
        runs_per_scenario=2,
        hooks=[_ProbeBackedHook(probe)],
    )

    first, second = result.runs
    assert result.aggregate.verdict == "PASS"
    assert first.trace.worker_warnings == []
    assert first.hook_artifacts == []
    assert any(
        warning.startswith("hook:state_probe: post-reset state differs")
        for warning in second.trace.worker_warnings
    )
    assert len(second.hook_artifacts) == 1

    artifact = second.hook_artifacts[0]
    payload = artifact.payload
    assert artifact.hook_name == "state_probe"
    assert artifact.label == "violation"
    assert {
        "schema_version",
        "run_id",
        "baseline_run_id",
        "violation",
        "message",
        "difference",
        "baseline_fingerprint",
        "observed_fingerprint",
        "baseline_summary",
        "observed_summary",
        "previous_run_end",
    } <= set(payload)
    assert payload["schema_version"] == 1
    assert payload["run_id"] == second.trace.run_id
    assert payload["baseline_run_id"] == first.trace.run_id
    assert payload["violation"] == "post_reset_state_mismatch"
    assert payload["difference"]["changed_keys"] == ["files"]
    assert payload["previous_run_end"]["run_id"] == first.trace.run_id


def test_state_probe_clean_multi_run_batch_produces_no_warning_or_artifact() -> None:
    probe = _MemoryProbe()
    result = run_scenario(
        _scenario(),
        _StateRuntime(probe, reset_clears=True),
        runs_per_scenario=2,
        hooks=[_ProbeBackedHook(probe)],
    )

    assert result.aggregate.verdict == "PASS"
    for run in result.runs:
        assert run.trace.worker_warnings == []
        assert run.hook_artifacts == []


def test_state_probe_reused_hook_resets_baseline_per_scenario_batch() -> None:
    probe = _MemoryProbe()
    hook = _ProbeBackedHook(probe)
    hooks = [hook]

    scenario_a = run_scenario(
        _scenario("state_probe_scenario_a"),
        _StateRuntime(probe, reset_clears=False),
        hooks=hooks,
    )
    scenario_b = run_scenario(
        _scenario("state_probe_scenario_b"),
        _StateRuntime(probe, reset_clears=False),
        runs_per_scenario=2,
        hooks=hooks,
    )

    assert scenario_a.runs[0].trace.worker_warnings == []
    assert scenario_a.runs[0].hook_artifacts == []

    first_b, second_b = scenario_b.runs
    assert first_b.trace.worker_warnings == []
    assert first_b.hook_artifacts == []
    assert any(
        warning.startswith("hook:state_probe: post-reset state differs")
        for warning in second_b.trace.worker_warnings
    )

    payload = second_b.hook_artifacts[0].payload
    assert payload["baseline_run_id"] == first_b.trace.run_id
    assert payload["baseline_run_id"] != scenario_a.runs[0].trace.run_id
    assert payload["previous_run_end"]["run_id"] == first_b.trace.run_id


def test_state_probe_violation_artifact_bounds_long_diff_keys() -> None:
    long_key = "k" * 5_000

    class LongKeyProbe:
        def __init__(self) -> None:
            self.state: dict[str, Any] = {}

        def capture(self) -> dict[str, Any]:
            return dict(self.state)

    class LongKeyHandle:
        def __init__(self, probe: LongKeyProbe) -> None:
            self.probe = probe

        def reset_state(self) -> None:
            pass

        def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
            self.probe.state[long_key] = "leaked"
            return {"content": "ok"}

        def teardown(self) -> None:
            pass

    class LongKeyRuntime:
        def __init__(self, probe: LongKeyProbe) -> None:
            self.probe = probe

        def provision(self, config: AgentConfig, mcps: list | None = None) -> LongKeyHandle:
            return LongKeyHandle(self.probe)

    probe = LongKeyProbe()
    result = run_scenario(
        _scenario(),
        LongKeyRuntime(probe),
        runs_per_scenario=2,
        hooks=[_ProbeBackedHook(probe)],
    )

    payload = result.runs[1].hook_artifacts[0].payload
    encoded = json.dumps(payload, ensure_ascii=False)
    added_key = payload["difference"]["added_keys"][0]

    assert long_key not in encoded
    assert len(encoded) < 4_000
    assert len(added_key) < 460
    assert added_key.endswith(" chars>")
    assert payload["difference"]["summary"] == f"added keys {added_key}"


def test_state_probe_exception_is_contained_like_hook_exception() -> None:
    class ExplodingProbe(_MemoryProbe):
        def capture(self) -> dict[str, Any]:
            raise RuntimeError("capture exploded")

    probe = ExplodingProbe()
    result = run_scenario(
        _scenario(),
        _StateRuntime(probe, reset_clears=True),
        hooks=[_ProbeBackedHook(probe)],
    )

    assert result.aggregate.verdict == "PASS"
    assert any(
        warning == "hook:state_probe: capture exploded"
        for warning in result.runs[0].trace.worker_warnings
    )
    assert result.runs[0].hook_artifacts == []
