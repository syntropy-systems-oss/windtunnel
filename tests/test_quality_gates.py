"""Regression tests for repository-level quality gate wiring."""
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ci_enforces_strict_mypy_configuration() -> None:
    """The strict MyPy config must not become aspirational/dead configuration."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "run: uv run mypy windtunnel" in workflow


def test_company_open_source_health_files_are_present() -> None:
    """Published stewardship promises must travel with every source checkout."""
    required = {
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "LICENSE",
        "NOTICE",
        "SECURITY.md",
        "SUPPORT.md",
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
    }

    assert {path for path in required if not (_REPO_ROOT / path).is_file()} == set()


def test_security_guidance_uses_private_reporting() -> None:
    """Security guidance must not accidentally send vulnerabilities to public issues."""
    policy = (_REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    issue_config = (
        _REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    ).read_text(encoding="utf-8")

    private_report_url = (
        "https://github.com/syntropy-systems-oss/windtunnel/security/advisories/new"
    )
    assert private_report_url in policy
    assert private_report_url in issue_config
    assert "Do not open a public issue" in policy


def test_github_housekeeping_yaml_is_parseable() -> None:
    """Malformed issue forms or dependency config should fail before merge."""
    paths = [
        *_REPO_ROOT.glob(".github/ISSUE_TEMPLATE/*.yml"),
        _REPO_ROOT / ".github" / "dependabot.yml",
    ]

    assert paths
    for path in paths:
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
