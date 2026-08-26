"""Knob SPI conformance — the generic experiment surface.

Covers:
  1. KnobSpec validation (name, kind, enum choices)
  2. normalize_knob_overrides: strict validation + CLI-string coercion
  3. A declaring runtime (InMemoryRuntime is the conformance reference):
     declaration shape, override honored through AgentConfig.knobs
  4. A non-declaring runtime behaves exactly as today (no attr, no checks)
  5. `wt run --knob NAME=VALUE`: parse, strict validation against a
     declaring runtime, opaque pass-through for a non-declaring one, and
     the end-to-end verdict flip through the real pipeline
"""
from __future__ import annotations

from pathlib import Path

import pytest

from windtunnel.spi.agent_runtime import (
    AgentConfig,
    KnobIntrospectableRuntime,
    KnobSpec,
    normalize_knob_overrides,
)


class TestKnobSpec:
    def test_valid_kinds(self) -> None:
        for kind in ("text", "enum", "number", "flag"):
            choices = ("a", "b") if kind == "enum" else None
            spec = KnobSpec(name="k", kind=kind, choices=choices)
            assert spec.kind == kind

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            KnobSpec(name="  ", kind="text")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            KnobSpec(name="k", kind="dial")  # type: ignore[arg-type]

    def test_enum_requires_choices(self) -> None:
        with pytest.raises(ValueError):
            KnobSpec(name="k", kind="enum")

    def test_choices_only_for_enum(self) -> None:
        with pytest.raises(ValueError):
            KnobSpec(name="k", kind="text", choices=("a",))


class TestNormalizeKnobOverrides:
    _SPECS = [
        KnobSpec(name="steering", kind="text", value="baseline"),
        KnobSpec(name="mode", kind="enum", choices=("fast", "careful")),
        KnobSpec(name="retries", kind="number", value=2),
        KnobSpec(name="verbose", kind="flag", value=False),
    ]

    def test_valid_typed_values_pass_through(self) -> None:
        normalized, errors = normalize_knob_overrides(
            self._SPECS,
            {"steering": "hi", "mode": "careful", "retries": 3, "verbose": True},
        )
        assert errors == []
        assert normalized == {"steering": "hi", "mode": "careful", "retries": 3, "verbose": True}

    def test_cli_strings_are_coerced(self) -> None:
        normalized, errors = normalize_knob_overrides(
            self._SPECS, {"retries": "3.5", "verbose": "true"}
        )
        assert errors == []
        assert normalized == {"retries": 3.5, "verbose": True}

    def test_unknown_name_is_an_error(self) -> None:
        _, errors = normalize_knob_overrides(self._SPECS, {"unlisted": "x"})
        assert len(errors) == 1
        assert "unknown knob" in errors[0]

    def test_enum_value_must_be_a_choice(self) -> None:
        _, errors = normalize_knob_overrides(self._SPECS, {"mode": "reckless"})
        assert len(errors) == 1

    def test_uncoercible_number_and_flag_are_errors(self) -> None:
        _, errors = normalize_knob_overrides(
            self._SPECS, {"retries": "many", "verbose": "maybe", "steering": 7}
        )
        assert len(errors) == 3

    def test_no_specs_means_every_override_is_unknown(self) -> None:
        _, errors = normalize_knob_overrides([], {"anything": "x"})
        assert errors and "declared: none" in errors[0]


class TestDeclaringRuntimeConformance:
    def test_in_memory_declares_and_honors_the_knob(self) -> None:
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        runtime = InMemoryRuntime(scripted_responses=["ok"])
        assert isinstance(runtime, KnobIntrospectableRuntime)
        specs = runtime.describe_knobs()
        assert [spec.name for spec in specs] == ["scripted_response"]
        assert specs[0].kind == "text"
        assert specs[0].value == "ok"

        handle = runtime.provision(AgentConfig(knobs={"scripted_response": "changed"}))
        response = handle.send([{"role": "user", "content": "say ok"}], "sid")
        assert response["choices"][0]["message"]["content"] == "changed"

    def test_no_override_preserves_behavior_exactly(self) -> None:
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        runtime = InMemoryRuntime(scripted_responses=["ok"])
        handle = runtime.provision(AgentConfig())
        response = handle.send([{"role": "user", "content": "say ok"}], "sid")
        assert response["choices"][0]["message"]["content"] == "ok"


class TestNonDeclaringRuntime:
    class _BareRuntime:
        def provision(self, config: AgentConfig, mcps: list | None = None):
            self.config = config
            raise NotImplementedError  # never provisioned in these tests

    def test_absent_capability_is_not_knob_introspectable(self) -> None:
        runtime = self._BareRuntime()
        assert not isinstance(runtime, KnobIntrospectableRuntime)
        assert getattr(runtime, "describe_knobs", None) is None


class TestWtRunKnobFlag:
    def test_knob_flag_reaches_agent_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from tests.test_cli_wt import _pack, _result, _scenario

        scenario = _scenario("knob_flow")
        captured: dict[str, object] = {}

        def fake_run_scenario(selected, runtime, *args, **kwargs):
            captured["knobs"] = kwargs["config"].knobs
            return _result(selected, passed=True)

        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [_pack("local", [scenario])])
        monkeypatch.setattr(runner, "run_scenario", fake_run_scenario)
        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--knob", "scripted_response=say this instead",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0
        assert captured["knobs"] == {"scripted_response": "say this instead"}

    def test_unknown_knob_against_declaring_runtime_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        import windtunnel.cli as cli
        from tests.test_cli_wt import _pack, _scenario

        scenario = _scenario("knob_flow")
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [_pack("local", [scenario])])
        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--knob", "unlisted=x",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 2
        assert "unknown knob" in capsys.readouterr().err

    def test_malformed_knob_flag_exits_2(self, capsys: pytest.CaptureFixture) -> None:
        import windtunnel.cli as cli

        rc = cli.main(["run", "--knob", "no-equals-sign"])
        assert rc == 2
        assert "NAME=VALUE" in capsys.readouterr().err

    def test_non_declaring_runtime_passes_overrides_through_opaquely(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from tests.test_cli_wt import _pack, _result, _scenario

        class BareRuntime:
            accepts_runner_managed_mcps = False

        class BarePlugin:
            def build(self, runtime_name: str, label: str, soul_path: str | None):
                return BareRuntime()

        scenario = _scenario("knob_flow")
        captured: dict[str, object] = {}

        def fake_run_scenario(selected, runtime, *args, **kwargs):
            captured["knobs"] = kwargs["config"].knobs
            return _result(selected, passed=True)

        monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: BarePlugin())
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [_pack("local", [scenario])])
        monkeypatch.setattr(runner, "run_scenario", fake_run_scenario)
        rc = cli.main([
            "run",
            "--runtime", "bare",
            "--scenario", scenario.name,
            "--knob", "anything=goes",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0
        # No declaration -> no validation; the raw string mapping passes through.
        assert captured["knobs"] == {"anything": "goes"}

    def test_knob_flips_a_verdict_through_the_real_pipeline(self, tmp_path: Path) -> None:
        """End to end: the scripted_response knob turns a PASS into a FAIL."""
        import json

        import windtunnel.cli as cli

        pack_path = tmp_path / "knob_pack.py"
        pack_path.write_text(
            "from windtunnel.api.pack import ScenarioPack\n"
            "from windtunnel.api.scenario import Scenario\n"
            "PACK = ScenarioPack(name='knob_pack', scenarios=[\n"
            "    Scenario(name='acknowledge_ok', prompt='say ok', target_facts=[['ok']]),\n"
            "])\n",
            encoding="utf-8",
        )
        runs_dir = tmp_path / "runs"
        common = ["run", "--pack-source", f"{pack_path}:PACK", "--pack", "knob_pack",
                  "--runs-dir", str(runs_dir)]
        assert cli.main([*common, "--label", "baseline"]) == 0
        assert cli.main([*common, "--label", "knobbed", "--knob", "scripted_response=nope"]) == 1
        verdicts = {
            (row["label"], row["verdict"])
            for row in (
                json.loads(line)
                for line in (runs_dir / "ledger.ndjsonl").read_text().splitlines()
            )
        }
        assert verdicts == {("baseline", "PASS"), ("knobbed", "FAIL")}
