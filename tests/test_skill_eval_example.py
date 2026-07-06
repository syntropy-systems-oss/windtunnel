"""CI-safe tests for the skill-eval example pack."""
from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from windtunnel.api.evaluators import evaluate_outcome, evaluate_trajectory
from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, compute_hash

ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = ROOT / "examples" / "skill-eval" / "pack.py"
PREPARE_PATH = ROOT / "examples" / "skill-eval" / "prepare.py"


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pack_module() -> ModuleType:
    return _load_module(PACK_PATH, "skill_eval_pack_under_test")


def _trace(
    *,
    content: str = "done",
    tool_calls: list[dict[str, Any]] | None = None,
    observations: dict[str, Any] | None = None,
) -> Trace:
    now = datetime.now(UTC)
    return Trace(
        scenario_id="skill-eval",
        agent_id="test",
        variant_id="test",
        model="test-model",
        quant="unknown",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=[
            Turn(role="user", content="task", tool_calls=[], tool_results=[], latency_ms=0.0),
            Turn(
                role="assistant",
                content=content,
                tool_calls=tool_calls or [],
                tool_results=[],
                latency_ms=0.0,
            ),
        ],
        tool_schema_hash=compute_hash("skill-eval"),
        observations=observations or {},
    )


def _terminal_call(command: str) -> dict[str, Any]:
    return {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": command}),
        },
    }


class TestSkillEvalPack:
    def test_pack_source_loads_and_scenarios_are_well_formed(self) -> None:
        from windtunnel.cli import _discover_scenario_packs

        packs = _discover_scenario_packs([f"{PACK_PATH}:PACK"])
        pack = packs[-1]

        assert isinstance(pack, ScenarioPack)
        assert pack.name == "skill_eval"
        assert [scenario.name for scenario in pack.scenarios] == [
            "cli-lookup",
            "build-envelope",
            "import-and-author",
        ]
        for scenario in pack.scenarios:
            assert "dim:skill_eval" in scenario.tags
            assert scenario.requires_files
            assert scenario.outcome_fn is not None
            assert scenario.trajectory_checks
            assert pack.state_probe_factory is not None
            assert pack.state_probe_factory(scenario) is not None

    def test_prepare_builds_all_templates_and_is_idempotent(self, tmp_path: Path) -> None:
        prepare = _load_module(PREPARE_PATH, "skill_eval_prepare_under_test")
        templates = tmp_path / "templates"

        prepare.build_templates(templates_dir=templates, bootstrap_venv=False)
        first = _snapshot(templates)
        prepare.build_templates(templates_dir=templates, bootstrap_venv=False)
        second = _snapshot(templates)

        assert first == second
        assert (templates / "skill" / "AGENTS.md").is_file()
        assert (templates / "skill" / ".agents" / "skills" / "windtunnel" / "SKILL.md").is_file()
        assert (templates / "agents-md" / "AGENTS.md").is_file()
        assert not (templates / "agents-md" / ".agents").exists()
        assert not (templates / "bare" / "AGENTS.md").exists()
        assert not (templates / "bare" / ".agents").exists()
        for arm in ("skill", "agents-md", "bare"):
            assert (templates / arm / "transcript.json").is_file()
            assert (templates / arm / "incident.wtin.json").is_file()
            assert (templates / arm / "runs" / "saved_trace.json").is_file()
            assert (templates / arm / "pyproject.toml").is_file()
            assert (templates / arm / ".windtunnel" / "terminus-bootstrap.sh").is_file()

    def test_workspace_probe_records_command_exit_codes(self, tmp_path: Path) -> None:
        module = _pack_module()
        py = shlex.quote(sys.executable)
        probe = module.WorkspaceCheckProbe(
            "scratch",
            [
                module.VerificationCommand(f"{py} -c \"print('passed')\""),
                module.VerificationCommand(f"{py} -c \"import sys; print('failed'); sys.exit(7)\""),
            ],
        )

        probe.bind_workspace(tmp_path)
        observed = probe.capture()[module.OBS_KEY]

        assert observed["passed"] is False
        assert [record["exit_code"] for record in observed["commands"]] == [0, 7]
        assert "passed" in observed["commands"][0]["stdout_tail"]
        assert "failed" in observed["commands"][1]["stdout_tail"]

    def test_consultation_check_annotates_doc_reads_and_never_fails(self) -> None:
        module = _pack_module()
        scenario = Scenario(
            name="docs",
            prompt="task",
            target_facts=[["done"]],
            trajectory_checks=[module.DocumentationConsultationCheck()],
        )

        read_trace = _trace(tool_calls=[_terminal_call("cat AGENTS.md")])
        read_result = evaluate_trajectory(read_trace, scenario)

        miss_trace = _trace(tool_calls=[_terminal_call("printf done")])
        miss_result = evaluate_trajectory(miss_trace, scenario)

        assert read_result.passed is True
        assert "docs_read=True" in read_result.detail
        assert miss_result.passed is True
        assert "docs_read=False" in miss_result.detail

    def test_workspace_outcomes_score_from_synthetic_traces(self) -> None:
        module = _pack_module()
        scenario = module.cli_lookup
        passing = _trace(
            observations={
                module.OBS_KEY: {
                    "scenario": "cli-lookup",
                    "passed": True,
                    "commands": [{"command": "verify", "exit_code": 0}],
                }
            }
        )
        failing = _trace(
            observations={
                module.OBS_KEY: {
                    "scenario": "cli-lookup",
                    "passed": False,
                    "commands": [{"command": "verify", "exit_code": 1}],
                }
            }
        )

        assert evaluate_outcome(passing, scenario).passed is True
        failed = evaluate_outcome(failing, scenario)
        assert failed.passed is False
        assert "verify -> 1" in failed.detail


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
