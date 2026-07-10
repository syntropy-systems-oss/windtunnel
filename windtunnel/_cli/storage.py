"""Trace sidecars, hook artifacts, and append-only sweep ledger storage."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.runner import ScenarioResult
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score, score_to_dict
from windtunnel.spi.hooks import HookArtifact


def _write_score_sidecar(
    trace_path: Path,
    score: Score,
    scenario: Scenario,
    *,
    origin: dict[str, Any] | None = None,
) -> Path:
    """Write the union report/triage score sidecar beside a trace."""
    flat = score_to_dict(score)
    sidecar = {
        **flat,
        "score": flat,
        "scenario": {
            "name": getattr(scenario, "name", ""),
            "prompt": getattr(scenario, "prompt", ""),
            "target_facts": getattr(scenario, "target_facts", []),
            "requires_tool_use": getattr(scenario, "requires_tool_use", False),
            "tags": list(getattr(scenario, "tags", []) or []),
            "must_call": getattr(scenario, "must_call", []),
            "forbidden_calls": getattr(scenario, "forbidden_calls", []),
        },
    }
    if origin is not None:
        sidecar["origin"] = origin
    score_path = trace_path.with_suffix(".score.json")
    score_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return score_path


def _write_hook_artifact_sidecar(trace_path: Path, artifact: HookArtifact) -> Path:
    """Write one run-scoped hook artifact beside its trace."""
    suffix = f".{_artifact_component(artifact.hook_name)}"
    if artifact.label:
        suffix += f".{_artifact_component(artifact.label)}"
    suffix += ".json"
    artifact_path = trace_path.with_suffix(suffix)
    artifact_path.write_text(
        json.dumps(artifact.payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return artifact_path


def _write_pack_hook_artifact(runs_dir: Path, sweep_timestamp: str, artifact: HookArtifact) -> Path:
    """Write one pack-scoped hook artifact at the runs directory root."""
    return _write_sweep_hook_artifact(runs_dir, sweep_timestamp, artifact)


def _write_scenario_hook_artifact(
    runs_dir: Path,
    sweep_timestamp: str,
    artifact: HookArtifact,
    scenario_id: str,
) -> Path:
    """Write one scenario-scoped hook artifact at the runs directory root."""
    return _write_sweep_hook_artifact(
        runs_dir,
        sweep_timestamp,
        artifact,
        scenario_id=scenario_id,
    )


def _write_sweep_hook_artifact(
    runs_dir: Path,
    sweep_timestamp: str,
    artifact: HookArtifact,
    *,
    scenario_id: str | None = None,
) -> Path:
    """Write one scenario- or pack-scoped hook artifact without overwriting."""
    filename = f"{sweep_timestamp}.{_artifact_component(artifact.hook_name)}"
    if scenario_id:
        filename += f".{_artifact_component(scenario_id)}"
    if artifact.label:
        filename += f".{_artifact_component(artifact.label)}"
    filename += ".pack.json"
    artifact_path = _collision_safe_artifact_path(Path(runs_dir) / filename)
    artifact_path.write_text(
        json.dumps(artifact.payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return artifact_path


def _collision_safe_artifact_path(path: Path) -> Path:
    if not path.exists():
        return path

    suffix = ".pack.json"
    name = path.name
    stem = name[: -len(suffix)] if name.endswith(suffix) else path.stem
    for counter in range(2, 10000):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            print(
                f"wt run: warning: hook artifact target exists: {path}; "
                f"writing {candidate.name} instead",
                file=sys.stderr,
            )
            return candidate
    raise OSError(f"could not find non-colliding hook artifact path for {path}")


def _artifact_component(value: str) -> str:
    component = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value))
    return component or "artifact"


def _sweep_artifact_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _ledger_timestamp() -> str:
    """Return the UTC timestamp format used in append-only ledger rows."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _origin_from_tags(tags: list[str] | None) -> str | None:
    """Extract the first best-effort ``origin:<ref>`` scenario tag."""
    for tag in tags or []:
        if tag.startswith("origin:") and tag != "origin:":
            return tag.removeprefix("origin:")
    return None


def _git_sha() -> str | None:
    """Return the current short git SHA, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _wt_version() -> str:
    """Return installed package metadata with a source-tree fallback."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("windtunnel-bench")
    except PackageNotFoundError:
        pass

    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, TypeError, ValueError):
        return "0+unknown"


def _ledger_record(
    *,
    scenario: Scenario,
    pack: ScenarioPack,
    result: ScenarioResult,
    label: str,
    git_sha: str | None,
    wt_version: str,
) -> dict[str, Any]:
    """Build one mechanism-only ledger row for a scenario aggregate."""
    aggregate = result.aggregate
    first_trace = result.runs[0].trace if result.runs else None
    return {
        "ts": _ledger_timestamp(),
        "scenario_id": scenario.name,
        "pack": getattr(pack, "name", None),
        "owner": getattr(pack, "owner", None),
        "label": label,
        "model": getattr(first_trace, "model", None),
        "quant": getattr(first_trace, "quant", None),
        "verdict": aggregate.verdict,
        "runs": aggregate.total,
        "layer_pass_rates": {
            "outcome": aggregate.outcome_pass_rate,
            "trajectory": aggregate.trajectory_pass_rate,
            "constraint": aggregate.constraint_pass_rate,
            "robustness": aggregate.robustness_pass_rate,
        },
        "run_ids": [run.trace.run_id for run in result.runs],
        "origin": _origin_from_tags(getattr(scenario, "tags", []) or []),
        "git_sha": git_sha,
        "wt_version": wt_version,
    }


def _append_ledger_records(runs_dir: Path, records: list[dict[str, Any]]) -> None:
    """Append aggregate rows to the ledger, degrading I/O errors to warnings."""
    if not records:
        return

    ledger_path = Path(runs_dir) / "ledger.ndjsonl"
    try:
        with ledger_path.open("a", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                output.write("\n")
    except OSError as exc:
        print(f"wt run: warning: could not write ledger {ledger_path}: {exc}", file=sys.stderr)
