from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from scripts import build_agent_skill

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
PACKAGE_SKILL_DIR = ROOT / "windtunnel" / "skill"
TOP_LEVEL_SKILL_DIR = ROOT / "skills" / "windtunnel"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} missing frontmatter"
    end = text.find("\n---\n", 4)
    assert end != -1, f"{path} has unterminated frontmatter"
    parsed = yaml.safe_load(text[4:end]) or {}
    assert isinstance(parsed, dict)
    return parsed


def _included_docs() -> list[Path]:
    paths: list[Path] = []
    for path in sorted(DOCS_DIR.rglob("*.md"), key=lambda item: item.relative_to(DOCS_DIR).as_posix()):
        meta = _frontmatter(path)
        agent_meta = meta.get("agent") or {}
        include = not (isinstance(agent_meta, dict) and agent_meta.get("include") is False)
        if include:
            paths.append(path)
    return paths


def _generated_bytes() -> dict[str, bytes]:
    roots = [
        ROOT / "AGENTS.md",
        ROOT / "docs" / "cli-reference.md",
        PACKAGE_SKILL_DIR,
        TOP_LEVEL_SKILL_DIR,
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(path for path in root.rglob("*") if path.is_file())
    return {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in sorted(files)}


def test_builder_is_deterministic() -> None:
    assert build_agent_skill.main() == 0
    first = _generated_bytes()
    assert build_agent_skill.main() == 0
    assert _generated_bytes() == first


def test_every_docs_page_has_frontmatter_description() -> None:
    for path in DOCS_DIR.rglob("*.md"):
        meta = _frontmatter(path)
        description = meta.get("description")
        assert isinstance(description, str) and description.strip(), path


def test_reference_files_have_provenance_headers() -> None:
    for source in _included_docs():
        rel = source.relative_to(DOCS_DIR)
        reference = PACKAGE_SKILL_DIR / "references" / rel
        assert reference.is_file(), rel
        first_line = reference.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith(f"<!-- GENERATED from docs/{rel.as_posix()} at "), first_line
        assert "do not edit; edit" in first_line


def test_generated_cli_reference_names_all_subcommands() -> None:
    text = (DOCS_DIR / "cli-reference.md").read_text(encoding="utf-8")
    for name in [
        "run",
        "report",
        "compare",
        "replay",
        "doctor",
        "import",
        "validate",
        "triage",
        "skill",
    ]:
        assert f"`wt {name}`" in text
    assert "The `wt` command ships 9 subcommands." in text


def test_skill_index_has_one_description_per_reference() -> None:
    text = (PACKAGE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("<!-- BEGIN GENERATED REFERENCE INDEX -->")
    end = text.index("<!-- END GENERATED REFERENCE INDEX -->")
    lines = [line for line in text[start:end].splitlines() if line.startswith("- `references/")]
    references = sorted(path for path in (PACKAGE_SKILL_DIR / "references").rglob("*.md"))
    assert len(lines) == len(references)
    for line in lines:
        path_text, description = line.removeprefix("- `").split("` - ", 1)
        assert (PACKAGE_SKILL_DIR / path_text).is_file()
        assert description.strip()


def test_wt_skill_path_prints_existing_directory() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "windtunnel.cli", "skill", "path"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    skill_path = Path(result.stdout.strip())
    assert skill_path.is_dir()
    assert (skill_path / "SKILL.md").is_file()


def test_wt_skill_install_symlink_and_copy(tmp_path: Path) -> None:
    symlink_dest = tmp_path / "symlink-skills"
    copy_dest = tmp_path / "copy-skills"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "windtunnel.cli",
            "skill",
            "install",
            "--dest",
            str(symlink_dest),
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    symlink_path = symlink_dest / "windtunnel"
    assert Path(result.stdout.strip()) == symlink_path.resolve()
    assert symlink_path.is_symlink()
    assert (symlink_path / "SKILL.md").is_file()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "windtunnel.cli",
            "skill",
            "install",
            "--dest",
            str(copy_dest),
            "--copy",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    copy_path = copy_dest / "windtunnel"
    assert Path(result.stdout.strip()) == copy_path.resolve()
    assert not copy_path.is_symlink()
    assert (copy_path / "SKILL.md").is_file()


def test_agent_pages_are_references_not_nav() -> None:
    assert (PACKAGE_SKILL_DIR / "references" / "agents" / "anti-patterns.md").is_file()
    assert (PACKAGE_SKILL_DIR / "references" / "agents" / "integration-checklist.md").is_file()

    mkdocs = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    assert "agents/**" in mkdocs.get("not_in_nav", "")
    nav_text = repr(mkdocs.get("nav", []))
    assert "agents/" not in nav_text


def test_top_level_skill_copy_matches_package_skill() -> None:
    package_files = {
        path.relative_to(PACKAGE_SKILL_DIR).as_posix(): path.read_bytes()
        for path in PACKAGE_SKILL_DIR.rglob("*")
        if path.is_file()
    }
    top_level_files = {
        path.relative_to(TOP_LEVEL_SKILL_DIR).as_posix(): path.read_bytes()
        for path in TOP_LEVEL_SKILL_DIR.rglob("*")
        if path.is_file()
    }
    assert top_level_files == package_files
