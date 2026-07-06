"""Wind Tunnel skill-evaluation example pack.

This pack benches whether the generated Wind Tunnel agent skill helps a
terminal agent use Wind Tunnel itself. The three experiment arms are selected
outside the pack by changing ``WT_TERMINUS_WORKSPACE_TEMPLATE``; the scenarios
and scoring are identical for every arm.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario, TrajectoryCheck
from windtunnel.api.score import FailureCost, LayerResult
from windtunnel.api.trace import Trace

DIM_TAG = "dim:skill_eval"
OBS_KEY = "workspace_check"
TODO_PLACEHOLDER = "TODO_REPLACE_WITH_REVIEWED_OUTCOME_FACT"

CLI_LOOKUP_VERIFY = "test -f answer.txt && grep -q 'wt rescore' answer.txt"
BUILD_ENVELOPE_VERIFY = "uv run wt validate --strict out.wtin.json"
IMPORT_VERIFY = [
    "test -f imported/scenario.py",
    "test -f imported/fixture.universe.json",
    "test -f imported/scorer.py",
    "test -f imported/IMPORTED.md",
    f"! grep -q '{TODO_PLACEHOLDER}' imported/scenario.py",
    "uv run wt validate incident.wtin.json",
]


@dataclass(frozen=True)
class VerificationCommand:
    command: str
    timeout_sec: int = 30


class WorkspaceCheckProbe:
    """Run deterministic workspace verification and freeze the result."""

    def __init__(self, scenario_name: str, commands: list[VerificationCommand]) -> None:
        self.scenario_name = scenario_name
        self.commands = list(commands)
        self.workspace_dir: Path | None = None

    def bind_workspace(self, workspace_dir: Path) -> None:
        self.workspace_dir = Path(workspace_dir)

    def reset(self) -> None:
        pass

    def capture(self) -> dict[str, Any]:
        if self.workspace_dir is None:
            return {
                OBS_KEY: {
                    "scenario": self.scenario_name,
                    "passed": False,
                    "error": "workspace not bound",
                    "commands": [],
                }
            }

        records = [_run_verification_command(self.workspace_dir, spec) for spec in self.commands]
        return {
            OBS_KEY: {
                "scenario": self.scenario_name,
                "passed": all(record["exit_code"] == 0 for record in records),
                "commands": records,
            }
        }


class WorkspaceOutcome:
    """Outcome scorer that consumes WorkspaceCheckProbe observations."""

    def __init__(self, scenario_name: str) -> None:
        self.scenario_name = scenario_name

    def __call__(self, trace: Trace) -> LayerResult:
        observed = trace.observations.get(OBS_KEY)
        if not isinstance(observed, dict):
            return LayerResult(False, f"missing {OBS_KEY} observation")
        if observed.get("scenario") != self.scenario_name:
            return LayerResult(
                False,
                f"workspace check scenario mismatch: {observed.get('scenario')!r}",
            )

        commands = observed.get("commands") or []
        if observed.get("passed") is True:
            return LayerResult(True, _command_summary(commands))
        return LayerResult(False, _command_summary(commands))


class DocumentationConsultationCheck(TrajectoryCheck):
    """Annotate whether terminal commands read the injected documentation."""

    def check(self, calls: list[str]) -> tuple[bool, str]:
        del calls
        return True, "docs_read=unknown"

    def check_trace(self, trace: Trace, calls: list[str]) -> tuple[bool, str]:
        del calls
        commands = _terminal_commands(trace)
        reads = [command for command in commands if _looks_like_doc_read(command)]
        return True, f"docs_read={bool(reads)} doc_read_commands={len(reads)}"


def _run_verification_command(workspace_dir: Path, spec: VerificationCommand) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            spec.command,
            cwd=workspace_dir,
            shell=True,
            text=True,
            capture_output=True,
            timeout=spec.timeout_sec,
            check=False,
        )
        return {
            "command": spec.command,
            "exit_code": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        return {
            "command": spec.command,
            "exit_code": 124,
            "stdout_tail": _tail(stdout or ""),
            "stderr_tail": _tail(stderr or f"timed out after {spec.timeout_sec}s"),
        }


def _tail(text: str, limit: int = 2000) -> str:
    return text[-limit:]


def _command_summary(commands: Any) -> str:
    if not isinstance(commands, list):
        return "workspace check malformed: commands is not a list"
    parts = [
        f"{item.get('command', '<unknown>')} -> {item.get('exit_code')}"
        for item in commands
        if isinstance(item, dict)
    ]
    if not parts:
        return "workspace check recorded no commands"
    if all(part.endswith("-> 0") for part in parts):
        return "workspace checks passed: " + "; ".join(parts)
    return "workspace checks failed: " + "; ".join(parts)


def _terminal_commands(trace: Trace) -> list[str]:
    commands: list[str] = []
    for turn in trace.turns:
        for call in turn.tool_calls:
            name, args = _tool_call_name_args(call)
            if name != "terminal":
                continue
            command = args.get("command") or args.get("keystrokes")
            if isinstance(command, str):
                commands.append(command)
    return commands


def _tool_call_name_args(call: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    function = call.get("function")
    if isinstance(function, dict):
        return _coerce_name_args(function.get("name"), function.get("arguments"))
    return _coerce_name_args(call.get("name"), call.get("args") or call.get("arguments"))


def _coerce_name_args(raw_name: Any, raw_args: Any) -> tuple[str | None, dict[str, Any]]:
    name = raw_name if isinstance(raw_name, str) else None
    if isinstance(raw_args, dict):
        return name, dict(raw_args)
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return name, {}
        if isinstance(parsed, dict):
            return name, parsed
    return name, {}


def _looks_like_doc_read(command: str) -> bool:
    lowered = command.lower()
    if ".agents/skills/" not in lowered and "agents.md" not in lowered:
        return False
    return bool(
        re.search(
            r"(^|[\s;&|()])"
            r"(cat|less|more|head|tail|sed|awk|grep|rg|find|open|ls)"
            r"($|[\s;&|()])",
            lowered,
        )
    )


def _probe_for(scenario: Scenario) -> WorkspaceCheckProbe | None:
    specs = {
        "cli-lookup": [VerificationCommand(CLI_LOOKUP_VERIFY)],
        "build-envelope": [VerificationCommand(BUILD_ENVELOPE_VERIFY, timeout_sec=60)],
        "import-and-author": [VerificationCommand(command, timeout_sec=60) for command in IMPORT_VERIFY],
    }.get(scenario.name)
    if specs is None:
        return None
    return WorkspaceCheckProbe(scenario.name, specs)


cli_lookup = Scenario(
    name="cli-lookup",
    prompt=(
        "The scenario definitions in this project changed. Without re-running "
        "anything, determine the exact wt command that re-scores the saved traces "
        "in ./runs against current definitions, and write that exact command as "
        "a single line to answer.txt."
    ),
    target_facts=[],
    outcome_fn=WorkspaceOutcome("cli-lookup"),
    trajectory_checks=[DocumentationConsultationCheck()],
    requires_files=["runs/saved_trace.json"],
    failure_cost=FailureCost(severity="low", customer_visible=False),
    tags=[DIM_TAG],
)

build_envelope = Scenario(
    name="build-envelope",
    prompt=(
        "Using the windtunnel library installed in this environment, produce a "
        "Contract A interchange envelope from transcript.json and write it to "
        "out.wtin.json so that `uv run wt validate --strict out.wtin.json` exits 0."
    ),
    target_facts=[],
    outcome_fn=WorkspaceOutcome("build-envelope"),
    trajectory_checks=[DocumentationConsultationCheck()],
    requires_files=["transcript.json"],
    failure_cost=FailureCost(severity="medium", customer_visible=False),
    tags=[DIM_TAG],
)

import_and_author = Scenario(
    name="import-and-author",
    prompt=(
        "Import it with wt import into ./imported, then edit the generated "
        "scenario so it expresses the correct outcome fact for this incident "
        "(replace the TODO placeholder), and verify your work runs."
    ),
    target_facts=[],
    outcome_fn=WorkspaceOutcome("import-and-author"),
    trajectory_checks=[DocumentationConsultationCheck()],
    requires_files=["incident.wtin.json"],
    failure_cost=FailureCost(severity="high", customer_visible=False),
    tags=[DIM_TAG, "slow"],
)

SCENARIOS = [cli_lookup, build_envelope, import_and_author]

PACK = ScenarioPack(
    name="skill_eval",
    scenarios=list(SCENARIOS),
    state_probe_factory=_probe_for,
    owner="examples",
    metadata={
        "experiment": "generated-skill discovery and usage",
        "runtime": "terminus",
    },
)
