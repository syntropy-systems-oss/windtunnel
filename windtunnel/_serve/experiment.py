"""Opt-in experiment mode for `wt serve` — scoped scenario reruns.

The viewer stays read-only by construction unless the operator passes
--experiment. Even then the server itself never writes under runs/: a rerun
is a SUBPROCESS running the ordinary `wt run` pipeline against exactly one
scenario, with a caller-supplied knob-overrides mapping (--knob) and an
experiment label that links the new ledger row back to the run it varies:

    exp-<parent run_id[:8]>-<UTC HHMMSS>

so before/after verdicts sit side by side on the dashboard and the run
screen can resolve the parent from the label alone.

One rerun in flight at a time — concurrent requests are refused (409). The
subprocess's combined stdout/stderr is buffered line by line for the SSE
progress stream and the status endpoint.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from windtunnel.spi.agent_runtime import KnobSpec, normalize_knob_overrides

EXPERIMENT_LABEL_PREFIX = "exp-"


@dataclass
class ExperimentJob:
    """One scoped rerun: its identity, live process, and buffered output."""

    label: str
    parent_run_id: str
    scenario_id: str
    knobs: dict[str, Any]
    started_at: str
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    output_lines: list[str] = field(default_factory=list)
    returncode: int | None = None

    def status_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "parent_run_id": self.parent_run_id,
            "scenario_id": self.scenario_id,
            "knobs": self.knobs,
            "started_at": self.started_at,
            "running": self.returncode is None,
            "returncode": self.returncode,
            "output_lines": len(self.output_lines),
        }


class ExperimentRunner:
    """Spawns and tracks scoped `wt run` reruns for the experiment endpoints.

    Knows nothing about any particular runtime: the runtime name, pack
    sources, and declared KnobSpecs are whatever `wt serve` was started
    with; knob values are validated against the declaration and passed to
    the subprocess as --knob flags, opaque all the way down.
    """

    def __init__(
        self,
        *,
        runtime_name: str,
        runs_dir: Path,
        pack_sources: list[str],
        knob_specs: list[KnobSpec] | None,
        knob_error: str | None = None,
    ) -> None:
        self.runtime_name = runtime_name
        self.runs_dir = Path(runs_dir)
        self.pack_sources = list(pack_sources)
        # None = runtime not knob-introspectable (or not constructible);
        # [] = introspectable but currently declaring nothing.
        self.knob_specs = knob_specs
        self.knob_error = knob_error
        self._lock = threading.Lock()
        self._job: ExperimentJob | None = None

    # ── introspection ────────────────────────────────────────────────────────

    def knobs_payload(self) -> dict[str, Any]:
        """The knob declaration as plain data for /api/meta."""
        if self.knob_specs is None:
            return {
                "declared": False,
                "knobs": [],
                "detail": self.knob_error
                or "runtime does not declare knobs (no describe_knobs())",
            }
        return {
            "declared": True,
            "knobs": [
                {
                    "name": spec.name,
                    "kind": spec.kind,
                    "value": spec.value,
                    "choices": list(spec.choices) if spec.choices else None,
                    "description": spec.description,
                    "scope": spec.scope,
                }
                for spec in self.knob_specs
            ],
            "detail": None,
        }

    def status(self) -> dict[str, Any]:
        job = self._job
        return {"job": job.status_dict() if job else None}

    def output_lines(self) -> list[str]:
        """Snapshot of the current/last job's buffered output."""
        job = self._job
        return list(job.output_lines) if job else []

    def job_finished(self) -> bool:
        job = self._job
        return job is None or job.returncode is not None

    # ── rerun ────────────────────────────────────────────────────────────────

    def start(
        self,
        *,
        parent_run_id: str,
        scenario_id: str,
        pack_name: str,
        overrides: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Validate and spawn one scoped rerun. Returns (http_status, payload).

        Strict validation: overrides must name declared knobs with
        shape-valid values — an experiment that silently dropped an
        override would falsify its own before/after comparison. A runtime
        that declares nothing accepts no overrides at all.
        """
        if overrides:
            if self.knob_specs is None:
                return 400, {
                    "error": "runtime declares no knobs; overrides are not accepted",
                    "detail": self.knob_error,
                }
            normalized, errors = normalize_knob_overrides(self.knob_specs, overrides)
            if errors:
                return 400, {"error": "invalid knob overrides", "detail": errors}
            overrides = normalized

        with self._lock:
            job = self._job
            if job is not None and job.returncode is None:
                return 409, {
                    "error": "a rerun is already in flight; wait for it to finish",
                    "job": job.status_dict(),
                }

            label = (
                f"{EXPERIMENT_LABEL_PREFIX}{parent_run_id[:8]}-"
                f"{datetime.now(UTC).strftime('%H%M%S')}"
            )
            argv = [
                sys.executable,
                "-m",
                "windtunnel.cli",
                "run",
                "--runtime",
                self.runtime_name,
                "--scenario",
                scenario_id,
                "--pack",
                pack_name,
                "--label",
                label,
                "--runs-dir",
                str(self.runs_dir),
            ]
            for source in self.pack_sources:
                argv += ["--pack-source", source]
            for name, value in overrides.items():
                argv += ["--knob", f"{name}={_knob_flag_value(value)}"]

            new_job = ExperimentJob(
                label=label,
                parent_run_id=parent_run_id,
                scenario_id=scenario_id,
                knobs=overrides,
                started_at=datetime.now(UTC).replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )
            try:
                new_job.process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                return 500, {"error": f"could not spawn rerun: {exc}"}
            self._job = new_job

        threading.Thread(target=self._pump, args=(new_job,), daemon=True).start()
        return 202, {"started": True, "job": new_job.status_dict()}

    def _pump(self, job: ExperimentJob) -> None:
        """Buffer the subprocess's output and record its exit code."""
        process = job.process
        assert process is not None and process.stdout is not None
        for raw in process.stdout:
            job.output_lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
        process.wait()
        job.returncode = process.returncode


def _knob_flag_value(value: Any) -> str:
    """Format a normalized knob value for a --knob NAME=VALUE flag."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
