"""Regression tests for ``wt selftest`` orchestration and output."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api import (
    Policy,
    ReferenceCase,
    ReferenceDecision,
    ReferenceKind,
    ReferenceToolCall,
    Scenario,
    StateProbeAvailable,
)
from windtunnel.api.pack import ScenarioPack
from windtunnel.spi import AgentConfig


class _Probe:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.reset_count = 0

    def reset(self) -> None:
        self.state.clear()
        self.reset_count += 1

    def capture(self) -> dict[str, Any]:
        return {"artifact": dict(self.state)}


class _Handle:
    def __init__(self, runtime: _Runtime, case: ReferenceCase) -> None:
        self.runtime = runtime
        self.case = case
        self.teardown_count = 0

    def reset_state(self) -> None:
        return None

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        del messages, session_id
        probe = self.runtime.current_probe
        assert probe is not None
        witnessed = []
        for index, decision in enumerate(self.case.decisions):
            for call in decision.tool_calls:
                probe.state.update(call.arguments)
                witnessed.append(
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                )
        return {"content": self.case.decisions[-1].content, "tool_calls": witnessed}

    def teardown(self) -> None:
        self.teardown_count += 1


class _Runtime:
    accepts_runner_managed_mcps = True

    def __init__(self) -> None:
        self.current_probe: _Probe | None = None
        self.handles: list[_Handle] = []

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> _Handle:
        del config, mcps
        raise AssertionError("ordinary provision cannot run a reference case")

    def provision_reference(
        self,
        config: AgentConfig,
        case: ReferenceCase,
        mcps: list[Any] | None = None,
    ) -> _Handle:
        del config, mcps
        handle = _Handle(self, case)
        self.handles.append(handle)
        return handle


class _Plugin:
    def __init__(self, runtime: _Runtime, pack: ScenarioPack) -> None:
        self.runtime = runtime
        self.pack = pack
        self.pre_run_count = 0
        self.probes: list[_Probe] = []

    def pre_run(self, runtime: _Runtime, scenarios: list[Scenario], runtime_name: str) -> None:
        assert runtime is self.runtime
        assert scenarios == self.pack.scenarios
        assert runtime_name == "reference_runtime"
        self.pre_run_count += 1

        def probe_factory(scenario: Scenario) -> _Probe:
            assert scenario in scenarios
            probe = _Probe()
            self.probes.append(probe)
            self.runtime.current_probe = probe
            return probe

        self.pack.state_probe_factory = probe_factory


def _reference(name: str, kind: ReferenceKind, *, safe: bool) -> ReferenceCase:
    return ReferenceCase(
        name=name,
        kind=kind,
        decisions=(
            ReferenceDecision(
                tool_calls=(
                    ReferenceToolCall("write_artifact", {"safe": safe}),
                )
            ),
            ReferenceDecision(content="artifact complete"),
        ),
    )


def _scenario(*cases: ReferenceCase) -> Scenario:
    return Scenario(
        name="artifact_guard",
        prompt="Create a safe artifact.",
        target_facts=[["artifact complete"]],
        requires_tool_use=True,
        policies=[
            Policy(
                name="artifact_is_safe",
                predicate=lambda trace: trace.observations["artifact"]["safe"] is True,
            )
        ],
        preconditions=[StateProbeAvailable()],
        reference_cases=list(cases),
    )


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    pack: ScenarioPack,
    runtime: object,
    plugin: object,
) -> None:
    import windtunnel._cli.selftest as cli_selftest

    monkeypatch.setattr(cli_selftest, "_discover_scenario_packs", lambda *_args: [pack])
    monkeypatch.setattr(cli_selftest, "_resolve_runtime_plugin", lambda _name: plugin)
    monkeypatch.setattr(
        cli_selftest,
        "_build_runtime",
        lambda _name, _label, soul_path, _plugin: runtime,
    )


def test_selftest_help_declares_required_runtime() -> None:
    from windtunnel.cli import _build_parser

    help_text = _build_parser().format_help()
    subparser = _build_parser().parse_args(["selftest", "--runtime", "example"])

    assert "selftest" in help_text
    assert subparser.command == "selftest"
    assert subparser.runtime == "example"


def test_unsupported_runtime_is_visible_without_preparing_pack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from windtunnel.cli import main

    scenario = _scenario(_reference("golden", "golden", safe=True))
    pack = ScenarioPack(name="synthetic", scenarios=[scenario])

    class UnsupportedPlugin:
        def pre_run(self, *_args: object) -> None:
            raise AssertionError("unsupported runtimes must not prepare fixtures")

    output = tmp_path / "selftest.json"
    _patch_services(monkeypatch, pack, object(), UnsupportedPlugin())

    exit_code = main(
        [
            "selftest",
            "--runtime",
            "unsupported",
            "--format",
            "json",
            "--out",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["windtunnel_selftest"] == 1
    assert payload["summary"]["unsupported"] == 1
    assert payload["cases"][0]["verdict"] == "UNSUPPORTED"


def test_golden_and_poison_certify_through_live_probe_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from windtunnel.cli import main

    scenario = _scenario(
        _reference("known-good", "golden", safe=True),
        _reference("unsafe-write", "poison", safe=False),
    )
    pack = ScenarioPack(name="synthetic", scenarios=[scenario], owner="example-team")
    runtime = _Runtime()
    plugin = _Plugin(runtime, pack)
    output = tmp_path / "selftest.json"
    runs_dir = tmp_path / "runs"
    _patch_services(monkeypatch, pack, runtime, plugin)

    exit_code = main(
        [
            "selftest",
            "--runtime",
            "reference_runtime",
            "--runs-dir",
            str(runs_dir),
            "--format",
            "json",
            "--out",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert plugin.pre_run_count == 1
    assert len(plugin.probes) == 2
    assert all(probe.reset_count == 1 for probe in plugin.probes)
    assert len(runtime.handles) == 2
    assert all(handle.teardown_count == 1 for handle in runtime.handles)
    assert [case["verdict"] for case in payload["cases"]] == ["PASS", "PASS"]
    poison = payload["cases"][1]
    assert poison["score"]["constraint"]["passed"] is False
    assert poison["owner"] == "example-team"
    assert all(Path(case["trace_path"]).is_file() for case in payload["cases"])
    assert len(list(runs_dir.rglob("*.score.json"))) == 2


def test_golden_failure_and_poison_escape_are_junit_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from windtunnel.cli import main

    scenario = _scenario(
        _reference("bad-golden", "golden", safe=False),
        _reference("weak-poison", "poison", safe=True),
    )
    pack = ScenarioPack(name="synthetic", scenarios=[scenario])
    runtime = _Runtime()
    plugin = _Plugin(runtime, pack)
    output = tmp_path / "selftest.xml"
    _patch_services(monkeypatch, pack, runtime, plugin)

    exit_code = main(
        [
            "selftest",
            "--runtime",
            "reference_runtime",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--format",
            "junit",
            "--out",
            str(output),
        ]
    )

    suite = ET.parse(output).getroot()
    failures = suite.findall("./testcase/failure")
    assert exit_code == 1
    assert suite.attrib["failures"] == "2"
    assert {failure.attrib["type"] for failure in failures} == {
        "GOLDEN_FAILED",
        "POISON_PASSED",
    }


def test_no_selected_reference_cases_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from windtunnel.cli import main

    pack = ScenarioPack(
        name="synthetic",
        scenarios=[Scenario(name="ordinary", prompt="say ok", target_facts=[["ok"]])],
    )
    _patch_services(monkeypatch, pack, object(), object())

    exit_code = main(["selftest", "--runtime", "unused"])

    assert exit_code == 2
    assert "no reference cases found" in capsys.readouterr().err


def test_selftest_requires_paired_machine_output_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from windtunnel.cli import main

    exit_code = main(["selftest", "--runtime", "unused", "--format", "json"])

    assert exit_code == 2
    assert "--format and --out" in capsys.readouterr().err
