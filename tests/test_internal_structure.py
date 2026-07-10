"""Regression tests for internal module ownership and compatibility facades."""

from __future__ import annotations

import ast
from pathlib import Path

from windtunnel import cli, report
from windtunnel._cli import hooks as cli_hooks
from windtunnel._cli import models as cli_models
from windtunnel._cli import output as cli_output
from windtunnel._cli import runtime_discovery, scenario_discovery
from windtunnel._cli import storage as cli_storage
from windtunnel._report import load as report_load
from windtunnel._report import model as report_model
from windtunnel._report import text as report_text
from windtunnel.api import runner
from windtunnel.api._runner import evidence, messages

_PACKAGE_ROOT = Path(__file__).parent.parent / "windtunnel"


def _imports(path: Path) -> list[tuple[str, str | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.extend((node.module, alias.name) for alias in node.names)
    return imports


def test_runner_preserves_historical_helper_attributes() -> None:
    assert runner._capture_surface is evidence.capture_surface
    assert runner._extract_reply is messages.extract_reply
    assert runner._tool_schema_hash is evidence.tool_schema_hash


def test_cli_preserves_discovery_and_hook_attributes() -> None:
    assert cli._CompletedAggregate is cli_models._CompletedAggregate
    assert cli._discover_scenario_packs is scenario_discovery._discover_scenario_packs
    assert cli._select_scenarios is scenario_discovery._select_scenarios
    assert cli._resolve_runtime_plugin is runtime_discovery._resolve_runtime_plugin
    assert cli._build_runtime is runtime_discovery._build_runtime
    assert cli._resolve_hooks is cli_hooks._resolve_hooks
    assert cli._dispatch_pack_end_hooks is cli_hooks._dispatch_pack_end_hooks
    assert cli._write_run_output is cli_output._write_run_output
    assert cli._write_score_sidecar is cli_storage._write_score_sidecar
    assert cli._ledger_record is cli_storage._ledger_record


def test_report_preserves_loader_model_and_renderer_attributes() -> None:
    assert report.load_runs is report_load.load_runs
    assert report._cell_from_run is report_model._cell_from_run
    assert report._build_report_data is report_model._build_report_data
    assert report.compute_diff is report_model.compute_diff
    assert report.generate_markdown is report_text.generate_markdown
    assert report.generate_json is report_text.generate_json


def test_private_implementation_packages_do_not_import_their_facades() -> None:
    facade_by_package = {
        "api/_runner": "windtunnel.api.runner",
        "_cli": "windtunnel.cli",
        "_report": "windtunnel.report",
    }
    violations: list[str] = []
    for relative_dir, forbidden_module in facade_by_package.items():
        for path in (_PACKAGE_ROOT / relative_dir).rglob("*.py"):
            for module, _name in _imports(path):
                if module == forbidden_module:
                    violations.append(f"{path.relative_to(_PACKAGE_ROOT)} imports {module}")
    assert violations == []


def test_production_code_does_not_import_private_evaluator_helpers() -> None:
    violations: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        if path.name == "evaluators.py":
            continue
        for module, name in _imports(path):
            if module == "windtunnel.api.evaluators" and name is not None and name.startswith("_"):
                violations.append(f"{path.relative_to(_PACKAGE_ROOT)} imports evaluators.{name}")
    assert violations == []
