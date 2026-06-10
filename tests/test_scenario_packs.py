"""Scenario-pack discovery tests — the CLI's pluggable-scenarios seam.

The mirror of test_runtime_plugins.py for the "windtunnel.scenario_packs"
entry-point group:

  1. built-ins — windtunnel.scenarios.builtin_packs(), explicit ordered list
  2. entry points — each value is a ScenarioPack INSTANCE or a ZERO-ARG
     callable returning one
  3. error — a broken/wrong-typed entry point exits 2, naming the entry point

Entry-point discovery is pinned here (unlike the runtimes group, whose leg 2
lives in driver-package suites) by monkeypatching importlib.metadata
.entry_points — the CLI resolves the function at call time
(`from importlib.metadata import entry_points` inside the function), so
patching the module attribute is sufficient and no synthetic package needs
installing.
"""
from __future__ import annotations

import importlib.metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario

# Canonical bench order — pins the pre-pack _load_scenarios flattening order
# (an alphabetical pkgutil discovery would silently reorder the sweep).
_EXPECTED_BUILTIN_ORDER = [
    "tool_affordance",
    "clarify_vs_guess",
    "memory_conflict",
    "policy_pressure",
    "recovery",
    "sampler_sensitivity",
    "side_effect_safety",
    "silent_failure",
    "icl_poisoning",
    "multi_turn_drift",
]


# ─── fixture packs for the entry-point leg ───────────────────────────────────

EXTERNAL_PACK = ScenarioPack(
    name="acme_custom",
    scenarios=[
        Scenario(
            name="acme_custom_scenario",
            prompt="say ok",
            target_facts=[["ok"]],
            tags=["dim:acme_custom"],
        )
    ],
)

# A transport-only pack whose scenario FAILS outcome under the in_memory
# runtime (scripted reply "ok" never contains "zebra") — used to prove the
# flag flows from the pack into the run loop's exit-code exemption.
TRANSPORT_ONLY_PACK = ScenarioPack(
    name="transport_probe_dim",
    scenarios=[
        Scenario(
            name="transport_probe",
            prompt="irrelevant",
            target_facts=[["zebra"]],
            tags=["dim:transport_probe_dim"],
        )
    ],
    transport_only=True,
)

# Same failing scenario WITHOUT transport_only — the control for the test
# above: the verdict must then flip the exit code.
GATING_PACK = ScenarioPack(
    name="gating_probe_dim",
    scenarios=[
        Scenario(
            name="gating_probe",
            prompt="irrelevant",
            target_facts=[["zebra"]],
            tags=["dim:gating_probe_dim"],
        )
    ],
)


def make_external_pack() -> ScenarioPack:
    """Zero-arg callable entry-point target (the other allowed shape)."""
    return ScenarioPack(
        name="acme_factory_built",
        scenarios=[
            Scenario(
                name="acme_factory_scenario",
                prompt="hi",
                target_facts=[["hi"]],
                tags=["dim:acme_factory_built"],
            )
        ],
    )


NOT_A_PACK = object()  # neither a ScenarioPack nor callable → must exit 2


def _patch_scenario_pack_eps(monkeypatch: pytest.MonkeyPatch, attrs: list[str]) -> None:
    """Make entry_points(group="windtunnel.scenario_packs") yield this module's attrs."""
    eps = [
        EntryPoint(name=attr, value=f"{__name__}:{attr}", group="windtunnel.scenario_packs")
        for attr in attrs
    ]
    real = importlib.metadata.entry_points

    def fake_entry_points(**kwargs):
        if kwargs.get("group") == "windtunnel.scenario_packs":
            return eps
        return real(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


# ─── leg 1: built-in discovery ───────────────────────────────────────────────


class TestBuiltinPackDiscovery:
    def test_all_ten_builtin_packs_discovered_in_canonical_order(self) -> None:
        from windtunnel.scenarios import builtin_packs

        assert [p.name for p in builtin_packs()] == _EXPECTED_BUILTIN_ORDER

    def test_every_builtin_pack_carries_scenarios_tagged_with_its_dim(self) -> None:
        from windtunnel.scenarios import builtin_packs

        for pack in builtin_packs():
            assert pack.scenarios, f"pack {pack.name!r} has no scenarios"
            for sc in pack.scenarios:
                assert f"dim:{pack.name}" in sc.tags, (
                    f"{sc.name} in pack {pack.name!r} is missing tag dim:{pack.name}"
                )

    def test_icl_poisoning_aliases_tool_affordance_factory(self) -> None:
        """icl_poisoning has no mock of its own — the pack must reuse the
        tool_affordance factory object (same tools, same port 8091)."""
        from windtunnel.scenarios.dim_icl_poisoning import PACK as icl
        from windtunnel.scenarios.dim_tool_affordance import PACK as ta

        assert icl.mcp_factory is ta.mcp_factory

    def test_memory_conflict_is_the_only_transport_only_builtin(self) -> None:
        from windtunnel.scenarios import builtin_packs

        assert [p.name for p in builtin_packs() if p.transport_only] == ["memory_conflict"]

    def test_multi_turn_pack_attaches_user_turns_to_inner_scenarios(self) -> None:
        """The MultiTurnScenario unwrapping (formerly in cli._load_scenarios)
        must happen at pack build — without it the runner only drives the
        final turn and the dim never exercises drift."""
        from windtunnel.scenarios.dim_multi_turn_drift import PACK
        from windtunnel.scenarios.dim_multi_turn_drift.scenarios import (
            MULTI_TURN_DRIFT_SCENARIOS,
        )

        assert len(PACK.scenarios) == len(MULTI_TURN_DRIFT_SCENARIOS)
        for sc in PACK.scenarios:
            assert getattr(sc, "user_turns", None), f"{sc.name} lost its user_turns"

    def test_load_scenarios_flattens_in_pack_order(self) -> None:
        from windtunnel.cli import _discover_scenario_packs, _load_scenarios

        packs = _discover_scenario_packs()
        scenarios = _load_scenarios([], packs)
        # First pack is tool_affordance, so its scenarios lead the sweep —
        # the same order the pre-pack hardcoded flattening produced.
        assert scenarios[0].name == "lookup_before_action"
        expected_total = sum(len(p.scenarios) for p in packs)
        assert len(scenarios) == expected_total

    def test_silent_failure_factory_is_scenario_aware(self) -> None:
        """The pack's factory derives MOCK_MCP_FAILURE_MODE from the selected
        scenario's perturbation — otherwise the scenario passes vacuously."""
        from windtunnel.scenarios.dim_silent_failure import PACK

        by_name = {sc.name: sc for sc in PACK.scenarios}
        server = PACK.mcp_factory(by_name["tool_timeout"])
        env = server._config.extra_env
        assert env["TOOL_PREFIX"] == ""
        assert env["MOCK_MCP_FAILURE_MODE"] == "timeout"
        assert "MOCK_MCP_TIMEOUT_SECONDS" in env


# ─── leg 2: entry-point packs ────────────────────────────────────────────────


class TestEntryPointPackDiscovery:
    def test_instance_entry_point_pack_is_appended_after_builtins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_scenario_pack_eps(monkeypatch, ["EXTERNAL_PACK"])
        from windtunnel.cli import _discover_scenario_packs

        packs = _discover_scenario_packs()
        assert [p.name for p in packs] == [*_EXPECTED_BUILTIN_ORDER, "acme_custom"]

    def test_external_pack_scenario_is_selectable_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_scenario_pack_eps(monkeypatch, ["EXTERNAL_PACK"])
        from windtunnel.cli import _discover_scenario_packs, _load_scenarios

        packs = _discover_scenario_packs()
        matched = _load_scenarios(["acme_custom_scenario"], packs)
        assert [s.name for s in matched] == ["acme_custom_scenario"]

    def test_zero_arg_callable_entry_point_is_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_scenario_pack_eps(monkeypatch, ["make_external_pack"])
        from windtunnel.cli import _discover_scenario_packs

        packs = _discover_scenario_packs()
        assert packs[-1].name == "acme_factory_built"

    def test_non_pack_target_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _patch_scenario_pack_eps(monkeypatch, ["NOT_A_PACK"])
        from windtunnel.cli import _discover_scenario_packs

        with pytest.raises(SystemExit) as exc:
            _discover_scenario_packs()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "NOT_A_PACK" in err
        assert "ScenarioPack" in err

    def test_unloadable_entry_point_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _patch_scenario_pack_eps(monkeypatch, ["NoSuchAttrXyz"])
        from windtunnel.cli import _discover_scenario_packs

        with pytest.raises(SystemExit) as exc:
            _discover_scenario_packs()
        assert exc.value.code == 2
        assert "could not load scenario pack" in capsys.readouterr().err


# ─── transport_only: pack → run loop ─────────────────────────────────────────


class TestTransportOnlyFlowsToRunLoop:
    """transport_only is read off the PACK and exempts the dim's MODEL verdict
    from the exit code (the run still executes, prints, and saves traces)."""

    def _run(self, monkeypatch, tmp_path: Path, attr: str, scenario: str) -> int:
        _patch_scenario_pack_eps(monkeypatch, [attr])
        from windtunnel.cli import main

        return main([
            "run",
            "--runtime", "in_memory",
            "--scenario", scenario,
            "--runs-dir", str(tmp_path / "runs"),
            "--label", "pack_test",
        ])

    def test_failing_transport_only_scenario_does_not_flip_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = self._run(monkeypatch, tmp_path, "TRANSPORT_ONLY_PACK", "transport_probe")
        out = capsys.readouterr().out
        assert "FAIL" in out  # the verdict itself is still reported...
        assert "transport-only" in out  # ...with the counterfactual warning...
        assert rc == 0  # ...but it doesn't gate the sweep

    def test_same_failure_without_transport_only_fails_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = self._run(monkeypatch, tmp_path, "GATING_PACK", "gating_probe")
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "transport-only" not in out
        assert rc == 1
