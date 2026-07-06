"""Build the generated Wind Tunnel agent skill from docs/ and argparse."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
TEMPLATE_PATH = ROOT / "agents" / "skill-template.md"
CLI_SOURCE = ROOT / "windtunnel" / "cli.py"
PACKAGE_SKILL_DIR = ROOT / "windtunnel" / "skill"
TOP_LEVEL_SKILL_DIR = ROOT / "skills" / "windtunnel"
AGENTS_PATH = ROOT / "AGENTS.md"
CLI_REFERENCE_PATH = DOCS_DIR / "cli-reference.md"

REFERENCE_INDEX_MARKER = "<!-- GENERATED_REFERENCE_INDEX -->"
CLI_DESCRIPTION = "Generated reference for wt CLI subcommands, usage, options, and exit-code semantics."
TOP_REFERENCE_DOCS = [
    Path("agent-quickstart.md"),
    Path("agents/integration-checklist.md"),
    Path("agents/anti-patterns.md"),
    Path("writing-a-runtime.md"),
    Path("design/0002-inject-protocol.md"),
    Path("importing-a-trace.md"),
]


@dataclass(frozen=True)
class DocPage:
    source: Path
    rel: Path
    description: str
    frontmatter: dict

    @property
    def reference_rel(self) -> Path:
        return self.rel


def _run_git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _source_id(path: Path) -> str:
    value = _run_git(["hash-object", str(path)])
    if value is not None:
        return value[:12]
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


def _combined_id(paths: list[Path]) -> str:
    digest = hashlib.sha1()
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def _generated_header(source: str, source_id: str, edit_target: str) -> str:
    return f"<!-- GENERATED from {source} at {source_id} — do not edit; edit {edit_target}. -->\n"


def _split_frontmatter(text: str, path: Path) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise SystemExit(f"{path}: missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SystemExit(f"{path}: unterminated YAML frontmatter")
    raw = text[4:end]
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path}: YAML frontmatter must be a mapping")
    return parsed, text[end + 5 :]


def _format_frontmatter(data: dict) -> str:
    yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _escape_cell(text: object) -> str:
    return " ".join(str(text).replace("|", "\\|").split())


def _normalize_usage(parser: argparse.ArgumentParser) -> str:
    usage = parser.format_usage().strip()
    if usage.startswith("usage: "):
        usage = usage.removeprefix("usage: ")
    return " ".join(usage.split())


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command_rows(subparsers: argparse._SubParsersAction) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for action in subparsers._choices_actions:
        rows.append((action.dest, action.help or ""))
    return rows


def _default_text(action: argparse.Action) -> str:
    if action.default is argparse.SUPPRESS:
        return ""
    if action.default is None:
        return ""
    if action.default is False:
        return "false"
    if action.default is True:
        return "true"
    return str(action.default)


def _actions_table(parser: argparse.ArgumentParser) -> str:
    rows = ["| Name | Required | Default | Help |", "|---|---:|---|---|"]
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            continue
        name = ", ".join(action.option_strings) if action.option_strings else action.dest
        required = "yes" if getattr(action, "required", False) else "no"
        help_text = action.help or ""
        if action.choices:
            choices = ", ".join(str(choice) for choice in action.choices)
            help_text = f"{help_text} Choices: {choices}."
        rows.append(
            "| "
            + " | ".join(
                [
                    f"`{_escape_cell(name)}`",
                    required,
                    _escape_cell(_default_text(action)),
                    _escape_cell(help_text),
                ]
            )
            + " |"
        )
    if len(rows) == 2:
        rows.append("| _(none)_ | no |  |  |")
    return "\n".join(rows)


def _render_command_section(name: str, parser: argparse.ArgumentParser, help_text: str) -> str:
    lines = [
        f"## `wt {name}`",
        "",
        help_text.strip() or "No command help is registered.",
        "",
        "Usage:",
        "",
        "```bash",
        _normalize_usage(parser),
        "```",
        "",
    ]
    nested = _subparsers_action(parser)
    if nested is not None:
        lines.extend(["Subcommands:", "", "| Command | Purpose |", "|---|---|"])
        for nested_name, nested_help in _command_rows(nested):
            lines.append(f"| `wt {name} {nested_name}` | {_escape_cell(nested_help)} |")
        lines.append("")
    lines.extend(["Arguments and options:", "", _actions_table(parser), ""])
    if nested is not None:
        for nested_name, nested_help in _command_rows(nested):
            nested_parser = nested.choices[nested_name]
            lines.extend(
                [
                    f"### `wt {name} {nested_name}`",
                    "",
                    nested_help.strip() or "No command help is registered.",
                    "",
                    "Usage:",
                    "",
                    "```bash",
                    _normalize_usage(nested_parser),
                    "```",
                    "",
                    "Arguments and options:",
                    "",
                    _actions_table(nested_parser),
                    "",
                ]
            )
    return "\n".join(lines)


def _generate_cli_reference() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from windtunnel.cli import _build_parser  # noqa: PLC0415

    parser = _build_parser()
    subparsers = _subparsers_action(parser)
    if subparsers is None:
        raise SystemExit("wt parser has no subcommands")

    commands = _command_rows(subparsers)
    lines = [
        _format_frontmatter({"description": CLI_DESCRIPTION}).rstrip(),
        _generated_header(
            "windtunnel.cli argparse",
            _source_id(CLI_SOURCE),
            "windtunnel/cli.py",
        ).rstrip(),
        "# CLI reference",
        "",
        f"The `wt` command ships {len(commands)} subcommands. This page is generated from `windtunnel.cli`'s argparse tree.",
        "",
        "| Command | Purpose |",
        "|---|---|",
    ]
    for name, help_text in commands:
        lines.append(f"| `wt {name}` | {_escape_cell(help_text)} |")
    lines.extend(
        [
            "",
            "Exit code conventions: `0` means success, `1` means a runtime failure, regression, world mismatch, or newly-scored outcome failure, and `2` means usage or configuration error.",
            "",
        ]
    )
    for name, help_text in commands:
        lines.append(_render_command_section(name, subparsers.choices[name], help_text).rstrip())
        lines.append("")
    _write(CLI_REFERENCE_PATH, "\n".join(lines).rstrip() + "\n")


def _load_doc_pages() -> list[DocPage]:
    pages: list[DocPage] = []
    for source in sorted(DOCS_DIR.rglob("*.md"), key=lambda path: path.relative_to(DOCS_DIR).as_posix()):
        rel = source.relative_to(DOCS_DIR)
        frontmatter, _ = _split_frontmatter(source.read_text(encoding="utf-8"), source)
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SystemExit(f"{source}: frontmatter description is required")
        agent_meta = frontmatter.get("agent") or {}
        include = not (isinstance(agent_meta, dict) and agent_meta.get("include") is False)
        if include:
            pages.append(DocPage(source=source, rel=rel, description=description.strip(), frontmatter=frontmatter))
    return pages


def _reference_index(pages: list[DocPage]) -> str:
    return "\n".join(
        f"- `references/{page.reference_rel.as_posix()}` - {page.description}" for page in pages
    )


def _top_references(pages: list[DocPage]) -> str:
    by_rel = {page.rel: page for page in pages}
    rows: list[str] = []
    for rel in TOP_REFERENCE_DOCS:
        page = by_rel.get(rel)
        if page is not None:
            rows.append(f"- `references/{page.reference_rel.as_posix()}` - {page.description}")
    return "\n".join(rows)


def _copy_reference_pages(pages: list[DocPage]) -> None:
    references_dir = PACKAGE_SKILL_DIR / "references"
    if references_dir.exists():
        shutil.rmtree(references_dir)
    for page in pages:
        source_text = page.source.read_text(encoding="utf-8")
        header = _generated_header(
            f"docs/{page.rel.as_posix()}",
            _source_id(page.source),
            f"docs/{page.rel.as_posix()}",
        )
        _write(references_dir / page.reference_rel, header + source_text)


def _extract_agents_template(template: str) -> tuple[str, str]:
    start_marker = "<!-- BEGIN AGENTS_MD_TEMPLATE -->"
    end_marker = "<!-- END AGENTS_MD_TEMPLATE -->"
    start = template.find(start_marker)
    end = template.find(end_marker)
    if start == -1 or end == -1 or end < start:
        raise SystemExit(f"{TEMPLATE_PATH}: missing AGENTS.md template markers")
    agents_template = template[start + len(start_marker) : end].strip()
    skill_template = (template[:start] + template[end + len(end_marker) :]).strip()
    return skill_template, agents_template


def _generate_skill_and_agents(pages: list[DocPage]) -> None:
    if PACKAGE_SKILL_DIR.exists():
        shutil.rmtree(PACKAGE_SKILL_DIR)
    PACKAGE_SKILL_DIR.mkdir(parents=True)
    _copy_reference_pages(pages)

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    skill_template, agents_template = _extract_agents_template(template_text)
    input_id = _combined_id([TEMPLATE_PATH, CLI_SOURCE, *[page.source for page in pages]])
    skill_frontmatter, skill_body = _split_frontmatter(skill_template + "\n", TEMPLATE_PATH)
    skill_text = (
        _format_frontmatter(skill_frontmatter)
        + _generated_header("agents/skill-template.md + docs/", input_id, "docs/ or agents/skill-template.md")
        + skill_body.lstrip().replace("{{REFERENCE_INDEX}}", _reference_index(pages)).rstrip()
        + "\n"
    )
    _write(PACKAGE_SKILL_DIR / "SKILL.md", skill_text)

    agents_text = (
        _generated_header("agents/skill-template.md + docs/", input_id, "docs/ or agents/skill-template.md")
        + agents_template.replace("{{TOP_REFERENCES}}", _top_references(pages)).rstrip()
        + "\n"
    )
    _write(AGENTS_PATH, agents_text)

    if TOP_LEVEL_SKILL_DIR.exists():
        shutil.rmtree(TOP_LEVEL_SKILL_DIR)
    TOP_LEVEL_SKILL_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PACKAGE_SKILL_DIR, TOP_LEVEL_SKILL_DIR)


def main() -> int:
    _generate_cli_reference()
    pages = _load_doc_pages()
    _generate_skill_and_agents(pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
