"""Machine-readable JSON and JUnit sweep output."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from windtunnel._cli.models import _CompletedAggregate


def _counts_as_gate_failure(completed: _CompletedAggregate) -> bool:
    """Return the same per-scenario gate decision as the run loop."""
    if completed.had_runner_error:
        return True
    if completed.transport_only:
        return False
    return completed.result.aggregate.verdict == "FAIL"


def _write_run_output(
    output_format: str,
    out_path: Path,
    completed: list[_CompletedAggregate],
    records: list[dict[str, Any]],
) -> None:
    """Write the requested machine-readable sweep output file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        _write_run_json(out_path, records)
        return
    if output_format == "junit":
        _write_run_junit(out_path, completed)
        return
    raise ValueError(f"unknown run output format: {output_format!r}")


def _write_run_json(out_path: Path, records: list[dict[str, Any]]) -> None:
    """Write the exact aggregate records also sent to the ledger."""
    out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_run_junit(out_path: Path, completed: list[_CompletedAggregate]) -> None:
    """Write one JUnit testsuite per pack and testcase per aggregate."""
    root = ET.Element("testsuites")
    total_failures = sum(1 for result in completed if _counts_as_gate_failure(result))
    total_time = sum(_aggregate_time_seconds(result) for result in completed)
    root.set("tests", str(len(completed)))
    root.set("failures", str(total_failures))
    root.set("errors", "0")
    root.set("time", _format_seconds(total_time))

    by_pack: dict[str, list[_CompletedAggregate]] = {}
    for result in completed:
        by_pack.setdefault(str(getattr(result.pack, "name", "")), []).append(result)

    for pack_name, pack_results in by_pack.items():
        suite_failures = sum(1 for result in pack_results if _counts_as_gate_failure(result))
        suite_time = sum(_aggregate_time_seconds(result) for result in pack_results)
        suite = ET.SubElement(
            root,
            "testsuite",
            {
                "name": pack_name,
                "tests": str(len(pack_results)),
                "failures": str(suite_failures),
                "errors": "0",
                "time": _format_seconds(suite_time),
            },
        )
        for result in pack_results:
            testcase = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": pack_name,
                    "name": str(getattr(result.scenario, "name", "")),
                    "time": _format_seconds(_aggregate_time_seconds(result)),
                },
            )
            if _counts_as_gate_failure(result):
                categories = _triage_categories(result)
                failure_attrs = {
                    "message": _junit_failure_message(result, categories),
                    "type": f"windtunnel.{result.result.aggregate.verdict}",
                }
                if categories:
                    failure_attrs["triage_category"] = ", ".join(categories)
                failure = ET.SubElement(testcase, "failure", failure_attrs)
                failure.text = _junit_failure_text(result, categories)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)


def _junit_failure_message(completed: _CompletedAggregate, categories: list[str]) -> str:
    """Return a compact failure summary for the JUnit failure attribute."""
    aggregate = completed.result.aggregate
    category = f" triage={', '.join(categories)}" if categories else ""
    return f"{aggregate.verdict}: {aggregate.passed}/{aggregate.total} outcome pass{category}"


def _junit_failure_text(completed: _CompletedAggregate, categories: list[str]) -> str:
    """Return the escaped-by-ElementTree multiline failure payload."""
    aggregate = completed.result.aggregate
    lines = [
        f"scenario_id: {getattr(completed.scenario, 'name', '')}",
        f"pack: {getattr(completed.pack, 'name', '')}",
        f"verdict: {aggregate.verdict}",
        f"outcome_pass_rate: {aggregate.outcome_pass_rate}",
        f"trajectory_pass_rate: {aggregate.trajectory_pass_rate}",
        f"constraint_pass_rate: {aggregate.constraint_pass_rate}",
        f"robustness_pass_rate: {aggregate.robustness_pass_rate}",
    ]
    if categories:
        lines.append(f"triage_category: {', '.join(categories)}")

    for index, run_result in enumerate(completed.result.runs, start=1):
        lines.append(f"run {index}: {getattr(run_result.trace, 'run_id', '')}")
        for layer_name in ("outcome", "trajectory", "constraint", "robustness"):
            layer = getattr(run_result.score, layer_name)
            status = "PASS" if layer.passed else "FAIL"
            lines.append(f"  {layer_name}: {status} - {layer.detail}")
    return "\n".join(lines)


def _triage_categories(completed: _CompletedAggregate) -> list[str]:
    """Return rule-based triage categories for failed runs when available."""
    attached = getattr(completed.result, "triage_category", None)
    if attached is None:
        attached = getattr(completed.result.aggregate, "triage_category", None)
    if attached:
        if isinstance(attached, str):
            return [attached]
        return [str(category) for category in attached]

    try:
        from windtunnel.triage.rule_based import RuleBasedClassifier
    except Exception:
        return []

    classifier = RuleBasedClassifier()
    categories: list[str] = []
    for run_result in completed.result.runs:
        if run_result.score.outcome.passed:
            continue
        try:
            classification = classifier.classify(
                completed.scenario,
                run_result.trace,
                run_result.score,
            )
        except Exception:
            continue
        category = getattr(classification, "category", None)
        if category and category not in categories:
            categories.append(str(category))
    return categories


def _aggregate_time_seconds(completed: _CompletedAggregate) -> float:
    """Return total elapsed run time for one aggregate, in seconds."""
    total = 0.0
    for run_result in completed.result.runs:
        started_at = getattr(run_result.trace, "started_at", None)
        finished_at = getattr(run_result.trace, "finished_at", None)
        if started_at is None or finished_at is None:
            continue
        total += max(0.0, (finished_at - started_at).total_seconds())
    return total


def _format_seconds(value: float) -> str:
    """Format JUnit time attributes as seconds."""
    return f"{value:.6f}"
