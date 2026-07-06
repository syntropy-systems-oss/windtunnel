"""Build the skill-eval Terminus workspace templates.

The committed fixture lives in ``base/``. This script materializes three
generated templates under ``templates/``:

* ``skill`` gets the current generated skill copied from ``skills/windtunnel``.
* ``agents-md`` gets only the root ``AGENTS.md`` index.
* ``bare`` gets no documentation.

Templates are intentionally ignored by git because the skill is generated and
must be copied fresh from the current checkout.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BASE_DIR = HERE / "base"
TEMPLATES_DIR = HERE / "templates"
SKILL_REL = Path(".agents") / "skills" / "windtunnel"


def build_templates(
    *,
    repo_root: Path = REPO_ROOT,
    base_dir: Path = BASE_DIR,
    templates_dir: Path = TEMPLATES_DIR,
    bootstrap_venv: bool = True,
) -> None:
    repo_root = repo_root.resolve()
    base_dir = base_dir.resolve()
    templates_dir = templates_dir.resolve()
    _validate_inputs(repo_root, base_dir)

    if templates_dir.exists():
        shutil.rmtree(templates_dir)
    templates_dir.mkdir(parents=True)

    for arm in ("skill", "agents-md", "bare"):
        target = templates_dir / arm
        shutil.copytree(base_dir, target)
        _write_workspace_pyproject(target)
        if arm in {"skill", "agents-md"}:
            _write_agents_index(target / "AGENTS.md", repo_root)
        if arm == "skill":
            skill_target = target / SKILL_REL
            skill_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(repo_root / "skills" / "windtunnel", skill_target)
        if bootstrap_venv:
            _bootstrap_venv(target, repo_root)


def _validate_inputs(repo_root: Path, base_dir: Path) -> None:
    if not base_dir.is_dir():
        raise SystemExit(f"base fixture not found: {base_dir}")
    skill_dir = repo_root / "skills" / "windtunnel"
    if not (skill_dir / "SKILL.md").is_file():
        raise SystemExit(f"generated skill not found: {skill_dir}")
    if not (repo_root / "AGENTS.md").is_file():
        raise SystemExit(f"AGENTS.md not found under repo root: {repo_root}")


def _write_agents_index(path: Path, repo_root: Path) -> None:
    text = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    text = text.replace("Installed skill: `wt skill path`", f"Installed skill: `{SKILL_REL}`")
    path.write_text(text, encoding="utf-8")


def _write_workspace_pyproject(workspace: Path) -> None:
    (workspace / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "windtunnel-skill-eval-workspace"',
                'version = "0.0.0"',
                'requires-python = ">=3.11"',
                "dependencies = []",
                "",
                "[tool.uv]",
                "package = false",
                'cache-dir = ".uv-cache"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _bootstrap_venv(workspace: Path, repo_root: Path) -> None:
    env_dir = workspace / ".venv"
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("prepare.py requires uv on PATH to bootstrap template .venv")
    subprocess.run(
        [
            uv,
            "--cache-dir",
            str(workspace / ".uv-cache"),
            "venv",
            "--python",
            "3.12",
            str(env_dir),
        ],
        cwd=workspace,
        check=True,
    )
    site_packages = _site_packages(env_dir)
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "windtunnel_repo.pth").write_text(f"{repo_root}\n", encoding="utf-8")
    _write_wt_script(env_dir)


def _site_packages(env_dir: Path) -> Path:
    completed = subprocess.run(
        [
            str(_venv_python(env_dir)),
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip())


def _venv_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _write_wt_script(env_dir: Path) -> None:
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / ("wt.cmd" if os.name == "nt" else "wt")
    if os.name == "nt":
        script.write_text(
            "@echo off\r\n"
            f"\"{_venv_python(env_dir)}\" -m windtunnel.cli %*\r\n",
            encoding="utf-8",
        )
        return

    script.write_text(
        "#!/bin/sh\n"
        'exec "$(dirname "$0")/python" -m windtunnel.cli "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--templates-dir", type=Path, default=TEMPLATES_DIR)
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="Skip .venv bootstrap. Intended for fast tests of template layout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    build_templates(
        repo_root=args.repo_root,
        base_dir=args.base_dir,
        templates_dir=args.templates_dir,
        bootstrap_venv=not args.no_venv,
    )
    print(f"wrote templates: {args.templates_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
