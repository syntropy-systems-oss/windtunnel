"""CLI orchestration and machine output for reference self-tests."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from windtunnel._cli.runtime_discovery import _build_runtime, _resolve_runtime_plugin
from windtunnel._cli.scenario_discovery import (
    _discover_scenario_packs,
    _print_selection_warnings,
    _select_scenarios,
)
from windtunnel._cli.storage import _write_score_sidecar
from windtunnel.api.scenario import Scenario
from windtunnel.api.selftest import (
    SelfTestCaseResult,
    SelfTestVerdict,
    run_reference_case,
    selftest_case_to_dict,
)
from windtunnel.api.trace import save_trace, storage_path
from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.reference import ReferenceCapableAgentRuntime, ReferenceCase

SELFTEST_OUTPUT_VERSION = 1


@dataclass(frozen=True)
class _CompletedSelfTest:
    """One CLI reference result plus its pack and persisted trace path."""

    pack_name: str
    pack_owner: str | None
    result: SelfTestCaseResult
    trace_path: Path | None = None


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Certify scenario gates with live golden and poison references."""
    output_format = getattr(args, "format", None)
    output_path = getattr(args, "out", None)
    if bool(output_format) != bool(output_path):
        print("wt selftest: --format and --out must be provided together.", file=sys.stderr)
        return 2

    runtime_name: str = args.runtime
    label: str = args.label or "selftest"
    plugin = _resolve_runtime_plugin(runtime_name)
    runtime = _build_runtime(runtime_name, label, soul_path=args.soul, _plugin=plugin)

    pack_sources = args.pack_source or []
    packs = _discover_scenario_packs(pack_sources) if pack_sources else _discover_scenario_packs()
    selection = _select_scenarios(
        scenario_patterns=args.scenario or [],
        tag_filters=args.tag or [],
        pack_filters=args.pack or [],
        owner_filters=args.owner or [],
        packs=packs,
    )
    _print_selection_warnings(selection, command="wt selftest")
    selected = [entry for entry in selection.entries if entry.scenario.reference_cases]
    if not selected:
        print(
            "wt selftest: no reference cases found in the selected scenarios.",
            file=sys.stderr,
        )
        return 2

    cases = [
        (entry.pack, entry.scenario, case)
        for entry in selected
        for case in entry.scenario.reference_cases
    ]

    if not isinstance(runtime, ReferenceCapableAgentRuntime):
        completed = [
            _CompletedSelfTest(
                pack_name=pack.name,
                pack_owner=pack.owner,
                result=SelfTestCaseResult(
                    scenario_id=scenario.name,
                    case=case,
                    verdict=SelfTestVerdict.UNSUPPORTED,
                    detail=(
                        f"runtime {type(runtime).__name__} does not implement "
                        "provision_reference()"
                    ),
                ),
            )
            for pack, scenario, case in cases
        ]
        _print_results(completed)
        return _finish_output(
            completed,
            runtime_name=runtime_name,
            output_format=output_format,
            output_path=output_path,
            default_exit=2,
        )

    system_prompt = _read_optional_text(args.soul, flag="--soul")
    if system_prompt is _MISSING:
        return 2
    persona_doc = _read_optional_path(args.agents, flag="--agents")
    if persona_doc is _MISSING:
        return 2

    # Plugins prepare their runtime-wide inference seam and live fixtures once.
    # Per-case probe and MCP factories are deliberately read only afterward.
    pre_run = getattr(plugin, "pre_run", None)
    if pre_run is not None:
        pre_run(runtime, [entry.scenario for entry in selected], runtime_name)

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    completed = []
    for pack, scenario, case in cases:
        result = _run_cli_case(
            runtime=runtime,
            scenario=scenario,
            case=case,
            pack=pack,
            label=label,
            system_prompt=system_prompt,
            persona_doc=persona_doc,
        )
        trace_path = None
        if result.trace is not None and result.score is not None:
            trace_path = storage_path(result.trace, base_dir=runs_dir)
            save_trace(result.trace, trace_path)
            _write_score_sidecar(
                trace_path,
                result.score,
                scenario,
                origin={
                    "kind": "selftest",
                    "reference_case": case.name,
                    "reference_kind": case.kind,
                    "selftest_verdict": result.verdict.value,
                },
            )
        completed.append(
            _CompletedSelfTest(
                pack_name=pack.name,
                pack_owner=pack.owner,
                result=result,
                trace_path=trace_path,
            )
        )

    _print_results(completed)
    return _finish_output(
        completed,
        runtime_name=runtime_name,
        output_format=output_format,
        output_path=output_path,
        default_exit=0 if all(item.result.passed for item in completed) else 1,
    )


_MISSING = object()


def _read_optional_text(raw: str | None, *, flag: str) -> str | None | object:
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_file():
        print(f"wt selftest: {flag} file not found: {raw}", file=sys.stderr)
        return _MISSING
    return path.read_text(encoding="utf-8")


def _read_optional_path(raw: str | None, *, flag: str) -> Path | None | object:
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_file():
        print(f"wt selftest: {flag} file not found: {raw}", file=sys.stderr)
        return _MISSING
    return path


def _run_cli_case(
    *,
    runtime: ReferenceCapableAgentRuntime,
    scenario: Scenario,
    case: ReferenceCase,
    pack: Any,
    label: str,
    system_prompt: str | None | object,
    persona_doc: Path | None | object,
) -> SelfTestCaseResult:
    """Resolve fresh pack wiring and execute one isolated reference case."""
    try:
        scenario_mcps = None
        if getattr(runtime, "accepts_runner_managed_mcps", True) and pack.mcp_factory is not None:
            scenario_mcps = [pack.mcp_factory(scenario)]
        scenario_probe = (
            pack.state_probe_factory(scenario)
            if pack.state_probe_factory is not None
            else None
        )
        config = AgentConfig(
            agent_id="wt-selftest",
            variant_id=f"{label}-{case.kind}-{case.name}",
            system_prompt=system_prompt if isinstance(system_prompt, str) else None,
            persona_doc=persona_doc if isinstance(persona_doc, Path) else None,
        )
        return run_reference_case(
            scenario,
            runtime,
            case,
            mcps=scenario_mcps,
            config=config,
            state_probe=scenario_probe,
        )
    except Exception as exc:  # noqa: BLE001 - one broken case must not hide the rest
        return SelfTestCaseResult(
            scenario_id=scenario.name,
            case=case,
            verdict=SelfTestVerdict.ERROR,
            detail=f"reference case setup failed: {type(exc).__name__}: {exc}",
        )


def _print_results(completed: list[_CompletedSelfTest]) -> None:
    for item in completed:
        result = item.result
        identity = f"{result.scenario_id}::{result.case.name}"
        print(f"  {result.verdict.value:<14} {identity:<50} {result.detail}")
    counts = _verdict_counts(completed)
    print(
        "summary: "
        f"cases={len(completed)} passed={counts[SelfTestVerdict.PASS.value]} "
        f"failed={sum(counts[name] for name in _FAILURE_VERDICTS)} "
        f"unsupported={counts[SelfTestVerdict.UNSUPPORTED.value]}"
    )


_FAILURE_VERDICTS = {
    SelfTestVerdict.GOLDEN_FAILED.value,
    SelfTestVerdict.POISON_PASSED.value,
    SelfTestVerdict.ERROR.value,
}


def _verdict_counts(completed: list[_CompletedSelfTest]) -> dict[str, int]:
    counts = {verdict.value: 0 for verdict in SelfTestVerdict}
    for item in completed:
        counts[item.result.verdict.value] += 1
    return counts


def _finish_output(
    completed: list[_CompletedSelfTest],
    *,
    runtime_name: str,
    output_format: str | None,
    output_path: str | None,
    default_exit: int,
) -> int:
    if output_format is None or output_path is None:
        return default_exit
    try:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "json":
            _write_json(path, runtime_name, completed)
        else:
            _write_junit(path, runtime_name, completed)
    except OSError as exc:
        print(f"wt selftest: could not write {output_format} output: {exc}", file=sys.stderr)
        return 1
    return default_exit


def _case_payload(item: _CompletedSelfTest) -> dict[str, Any]:
    payload = selftest_case_to_dict(item.result)
    payload["pack"] = item.pack_name
    payload["owner"] = item.pack_owner
    if item.trace_path is not None:
        payload["trace_path"] = str(item.trace_path)
    return payload


def _write_json(path: Path, runtime_name: str, completed: list[_CompletedSelfTest]) -> None:
    counts = _verdict_counts(completed)
    payload = {
        "windtunnel_selftest": SELFTEST_OUTPUT_VERSION,
        "runtime": runtime_name,
        "summary": {
            "cases": len(completed),
            "passed": counts[SelfTestVerdict.PASS.value],
            "failed": sum(counts[name] for name in _FAILURE_VERDICTS),
            "unsupported": counts[SelfTestVerdict.UNSUPPORTED.value],
            "verdicts": counts,
        },
        "cases": [_case_payload(item) for item in completed],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_junit(path: Path, runtime_name: str, completed: list[_CompletedSelfTest]) -> None:
    counts = _verdict_counts(completed)
    suite = ET.Element(
        "testsuite",
        {
            "name": f"windtunnel.selftest.{runtime_name}",
            "tests": str(len(completed)),
            "failures": str(sum(counts[name] for name in _FAILURE_VERDICTS)),
            "errors": "0",
            "skipped": str(counts[SelfTestVerdict.UNSUPPORTED.value]),
        },
    )
    for item in completed:
        result = item.result
        case_node = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"{item.pack_name}.{result.scenario_id}",
                "name": result.case.name,
            },
        )
        if result.verdict is SelfTestVerdict.UNSUPPORTED:
            ET.SubElement(case_node, "skipped", {"message": result.detail})
        elif result.verdict.value in _FAILURE_VERDICTS:
            failure = ET.SubElement(
                case_node,
                "failure",
                {"type": result.verdict.value, "message": result.detail},
            )
            failure.text = result.detail
        ET.SubElement(case_node, "system-out").text = json.dumps(_case_payload(item))
    ET.indent(suite, space="  ")
    path.write_text(ET.tostring(suite, encoding="unicode") + "\n", encoding="utf-8")
