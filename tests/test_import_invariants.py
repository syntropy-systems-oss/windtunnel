"""Import-invariant lint test — the hard architectural invariant.

The invariant:
  - Anything in windtunnel/api/ must NOT import from windtunnel/runtimes/*,
    windtunnel/mcp/*, or platform-specific external packages (docker,
    paramiko, etc.)
  - Anything in windtunnel/scenarios/ must NOT import from
    windtunnel/runtimes/* or windtunnel/mcp/*

This is enforced by AST-walking each source file and checking its import
statements. The test must FAIL if a violation is introduced — this is
the load-bearing contract documented in README.md.

Mechanism: stdlib ast.parse() + walk for Import and ImportFrom nodes.
No ruff plugin dependency — pure stdlib, zero extra deps.

Why this matters: the API/SPI split is only useful if scenario authors
can write scenarios that run against ANY runtime. The moment a scenario
imports from windtunnel.runtimes.*, it becomes platform-specific
and the portability guarantee is lost. This test is the tripwire.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repo root: windtunnel/ is two levels up from tests/
_WINDTUNNEL_ROOT = Path(__file__).parent.parent / "windtunnel"

# Modules forbidden in api/ and scenarios/
_FORBIDDEN_RUNTIME_PREFIXES = (
    "windtunnel.runtimes",
    "windtunnel.mcp",
)

# Platform-specific packages also forbidden in api/ and scenarios/
_FORBIDDEN_EXTERNAL = (
    "docker",
    "paramiko",
    "boto3",
    "botocore",
)


def _collect_imports(path: Path) -> list[str]:
    """Return all module names imported by a Python file (AST walk)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _py_files_under(directory: Path) -> list[Path]:
    """Recursively collect .py files under a directory."""
    return [p for p in directory.rglob("*.py") if "__pycache__" not in str(p)]


def _check_no_forbidden(
    py_files: list[Path],
    forbidden_prefixes: tuple[str, ...],
    forbidden_external: tuple[str, ...],
    context: str,
) -> list[str]:
    """Return list of violation strings (empty = all clean)."""
    violations: list[str] = []
    for path in py_files:
        imports = _collect_imports(path)
        # Use relative path when possible, fall back to str(path)
        try:
            display_path = path.relative_to(_WINDTUNNEL_ROOT.parent)
        except ValueError:
            display_path = path  # type: ignore[assignment]
        for imp in imports:
            for prefix in forbidden_prefixes:
                if imp == prefix or imp.startswith(prefix + "."):
                    violations.append(
                        f"{context}: {display_path} "
                        f"imports forbidden module {imp!r}"
                    )
            for ext in forbidden_external:
                if imp == ext or imp.startswith(ext + "."):
                    violations.append(
                        f"{context}: {display_path} "
                        f"imports platform-specific package {imp!r}"
                    )
    return violations


class TestApiLayerInvariants:
    """windtunnel/api/ must never import from runtimes/ or mcp/ or platform packages."""

    def test_api_does_not_import_runtimes_or_mcp(self) -> None:
        api_dir = _WINDTUNNEL_ROOT / "api"
        if not api_dir.exists():
            pytest.skip("windtunnel/api/ not found")

        py_files = _py_files_under(api_dir)
        violations = _check_no_forbidden(
            py_files,
            _FORBIDDEN_RUNTIME_PREFIXES,
            _FORBIDDEN_EXTERNAL,
            "api/",
        )
        assert violations == [], (
            "api/ layer imports platform-specific modules — "
            "this breaks the API/SPI portability contract:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestScenariosLayerInvariants:
    """windtunnel/scenarios/ must never import from runtimes/ or mcp/."""

    def test_scenarios_do_not_import_runtimes_or_mcp(self) -> None:
        scenarios_dir = _WINDTUNNEL_ROOT / "scenarios"
        if not scenarios_dir.exists():
            pytest.skip("windtunnel/scenarios/ not found")

        py_files = _py_files_under(scenarios_dir)
        violations = _check_no_forbidden(
            py_files,
            _FORBIDDEN_RUNTIME_PREFIXES,
            (),  # scenarios may use external packages (mcp, fastmcp, etc.)
            "scenarios/",
        )
        assert violations == [], (
            "scenarios/ layer imports platform-specific runtime modules — "
            "scenarios must be runtime-agnostic:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestInvariantEnforcement:
    """Prove the invariant check actually catches violations (self-test)."""

    def test_synthetic_violation_is_detected(self, tmp_path: Path) -> None:
        """A file that imports windtunnel.runtimes.* must produce a violation."""
        bad_file = tmp_path / "bad_scenario.py"
        bad_file.write_text(
            "from windtunnel.runtimes.in_memory import InMemoryRuntime\n"
        )
        violations = _check_no_forbidden(
            [bad_file],
            _FORBIDDEN_RUNTIME_PREFIXES,
            (),
            "test/",
        )
        assert len(violations) == 1
        assert "windtunnel.runtimes.in_memory" in violations[0]

    def test_clean_file_produces_no_violations(self, tmp_path: Path) -> None:
        """A file that only imports windtunnel.api.* is clean."""
        good_file = tmp_path / "good_scenario.py"
        good_file.write_text(
            "from windtunnel.api.scenario import Scenario\n"
            "from windtunnel.api.trace import Trace\n"
        )
        violations = _check_no_forbidden(
            [good_file],
            _FORBIDDEN_RUNTIME_PREFIXES,
            _FORBIDDEN_EXTERNAL,
            "test/",
        )
        assert violations == []

    def test_external_platform_package_detected(self, tmp_path: Path) -> None:
        """A file that imports docker or paramiko must produce a violation."""
        bad_file = tmp_path / "bad_api.py"
        bad_file.write_text("import paramiko\nimport docker\n")
        violations = _check_no_forbidden(
            [bad_file],
            (),
            _FORBIDDEN_EXTERNAL,
            "api/",
        )
        assert len(violations) == 2
