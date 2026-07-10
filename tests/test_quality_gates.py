"""Regression tests for repository-level quality gate wiring."""
from pathlib import Path


def test_ci_enforces_strict_mypy_configuration() -> None:
    """The strict MyPy config must not become aspirational/dead configuration."""
    repo_root = Path(__file__).resolve().parent.parent
    workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "run: uv run mypy windtunnel" in workflow
