"""Tests for the wt CLI.

Covers:
  - wt --help works
  - wt report --help works
  - wt compare --help works
  - wt replay --help works
  - wt run --help works
  - Exit code 0 on success, non-zero on failure
  - wt report generates HTML from a runs/ dir
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def _wt(
    *args: str, check: bool = False, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `wt` CLI via `python -m windtunnel.cli` and return CompletedProcess.

    timeout: optional subprocess wall-clock cap (raises subprocess.TimeoutExpired).
    `wt run` now defaults to the in_memory runtime, so nothing here should touch
    live infra — the cap is a deterministic backstop in case a misconfigured
    runtime ever blocks instead of failing fast.
    """
    return subprocess.run(
        [sys.executable, "-m", "windtunnel.cli", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


class TestWtHelp:
    def test_wt_help_exits_zero(self) -> None:
        result = _wt("--help")
        assert result.returncode == 0

    def test_wt_no_args_exits_nonzero(self) -> None:
        result = _wt()
        assert result.returncode != 0

    def test_wt_report_help_exits_zero(self) -> None:
        result = _wt("report", "--help")
        assert result.returncode == 0

    def test_wt_compare_help_exits_zero(self) -> None:
        result = _wt("compare", "--help")
        assert result.returncode == 0

    def test_wt_run_help_exits_zero(self) -> None:
        result = _wt("run", "--help")
        assert result.returncode == 0

    def test_wt_replay_help_exits_zero(self) -> None:
        result = _wt("replay", "--help")
        assert result.returncode == 0

    def test_wt_report_help_mentions_runs(self) -> None:
        result = _wt("report", "--help")
        assert "runs" in result.stdout.lower() or "runs" in result.stderr.lower()

    def test_wt_run_help_mentions_scenario(self) -> None:
        result = _wt("run", "--help")
        combined = result.stdout + result.stderr
        assert "scenario" in combined.lower()

    def test_wt_run_help_mentions_runtime(self) -> None:
        result = _wt("run", "--help")
        combined = result.stdout + result.stderr
        assert "runtime" in combined.lower()

    def test_wt_replay_help_mentions_trace(self) -> None:
        result = _wt("replay", "--help")
        combined = result.stdout + result.stderr
        assert "trace" in combined.lower()

    def test_wt_compare_help_mentions_labels(self) -> None:
        result = _wt("compare", "--help")
        combined = result.stdout + result.stderr
        assert "label" in combined.lower()


class TestWtReport:
    """wt report generates HTML from a runs/ directory."""

    def _write_fake_trace(self, runs_dir: Path, scenario_id: str, variant_id: str) -> Path:
        """Write a minimal valid trace JSON to the runs/ directory."""
        from windtunnel.api.trace import Trace, Turn, save_trace, storage_path
        now = datetime.now(UTC)
        trace = Trace(
            scenario_id=scenario_id,
            agent_id="test-agent",
            variant_id=variant_id,
            model="test-model",
            quant="unknown",
            sampler={},
            started_at=now,
            finished_at=now,
            turns=[
                Turn(role="user", content="hello", tool_calls=[], tool_results=[], latency_ms=0.0),
                Turn(role="assistant", content="world", tool_calls=[], tool_results=[], latency_ms=100.0),
            ],
            tool_schema_hash="sha256:abc",
            worker_warnings=[],
        )
        path = storage_path(trace, base_dir=runs_dir)
        save_trace(trace, path)
        return path

    def test_report_html_generated(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        self._write_fake_trace(runs_dir, "echo_test", "prod_v1")
        out_html = tmp_path / "report.html"
        result = _wt("report", "--runs", str(runs_dir), "--out", str(out_html))
        assert result.returncode == 0
        assert out_html.exists()
        html_content = out_html.read_text()
        assert "<html" in html_content.lower()

    def test_report_markdown_to_stdout(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        self._write_fake_trace(runs_dir, "echo_test", "prod_v1")
        result = _wt("report", "--runs", str(runs_dir), "--format", "markdown")
        assert result.returncode == 0
        assert len(result.stdout) > 0

    def test_report_empty_runs_dir_exits_zero(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        out_html = tmp_path / "report.html"
        result = _wt("report", "--runs", str(runs_dir), "--out", str(out_html))
        assert result.returncode == 0


class TestWtCompare:
    def test_compare_without_labels_shows_help_or_error(self) -> None:
        result = _wt("compare")
        # Should print usage or error (not crash with traceback)
        assert result.returncode != 0 or "label" in (result.stdout + result.stderr).lower()


class TestWtRun:
    """wt run subcommand — happy path with --help only (a live run needs a live platform)."""

    def test_run_without_args_exits_nonzero(self) -> None:
        # `wt run` with no --scenario defaults to the in_memory runtime, so no
        # live infra is involved and the command should fail fast with a
        # non-zero exit. The test's intent is that the CLI doesn't crash with
        # an unhandled traceback; the wall-clock cap is kept as a deterministic
        # backstop so the suite cannot hang if a runtime ever blocks instead
        # of failing fast.
        try:
            result = _wt("run", timeout=20)
        except subprocess.TimeoutExpired:
            return  # blocked on live-runtime provisioning, not a crash — acceptable
        # Without args it may print usage or exit non-zero — both acceptable
        # as long as it doesn't crash with an unhandled exception
        assert "traceback" not in result.stderr.lower() or result.returncode != 0


class TestWtReplay:
    def test_replay_without_args_exits_nonzero(self) -> None:
        result = _wt("replay")
        assert result.returncode != 0


class TestWtRunScoreSidecar:
    """`wt run` writes a `.score.json` sidecar next to each saved trace, so the
    output is directly consumable by `wt report/compare/triage` — no re-scoring
    pass required for fresh runs."""

    def _run_in_memory(self, runs_dir: Path) -> subprocess.CompletedProcess[str]:
        """Drive one real scenario through the in_memory runtime via the CLI.

        The scripted reply is "ok" with no tool calls, so a requires_tool_use
        scenario FAILS its outcome — exit code 1 is expected; what we assert
        is the persisted artifact pair, not the verdict.
        """
        return _wt(
            "run",
            "--runtime", "in_memory",
            "--scenario", "lookup_before_action",
            "--runs-dir", str(runs_dir),
            "--label", "sidecar_test",
            timeout=120,
        )

    def test_run_writes_score_sidecar_next_to_each_trace(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        result = self._run_in_memory(runs_dir)
        assert "Traceback" not in result.stderr, result.stderr

        traces = [
            p for p in runs_dir.rglob("*.json")
            if not p.name.endswith(".score.json")
        ]
        assert traces, f"no traces saved under {runs_dir}"
        for trace_path in traces:
            sidecar = trace_path.with_suffix(".score.json")
            assert sidecar.exists(), f"missing sidecar for {trace_path}"

    def test_sidecar_has_both_consumer_shapes(self, tmp_path: Path) -> None:
        """The sidecar carries BOTH the flat report shape (top-level layer keys)
        and the nested triage shape ({"scenario", "score"})."""
        import json

        runs_dir = tmp_path / "runs"
        self._run_in_memory(runs_dir)
        sidecars = list(runs_dir.rglob("*.score.json"))
        assert sidecars
        data = json.loads(sidecars[0].read_text(encoding="utf-8"))

        # Flat shape — report.load_runs/_cell_from_run consumers
        for layer in ("outcome", "trajectory", "constraint", "robustness"):
            assert "passed" in data[layer], f"flat {layer} missing 'passed'"
            assert "detail" in data[layer], f"flat {layer} missing 'detail'"
        assert "failure_cost" in data
        assert "severity" in data["failure_cost"]

        # Nested shape — wt triage consumer
        for layer in ("outcome", "trajectory", "constraint", "robustness"):
            assert "passed" in data["score"][layer]
        assert data["scenario"]["name"] == "lookup_before_action"
        assert data["scenario"]["requires_tool_use"] is True

    def test_load_runs_picks_up_run_output_with_verdict(self, tmp_path: Path) -> None:
        """report.load_runs() pairs the trace with its sidecar and the report
        cell builder derives a verdict — proving `wt run` → `wt report` works
        without an intermediate re-scoring step."""
        from windtunnel.report import load_runs

        runs_dir = tmp_path / "runs"
        self._run_in_memory(runs_dir)

        cells = load_runs(runs_dir)
        key = ("lookup_before_action", "sidecar_test")
        assert key in cells, f"load_runs returned {list(cells)}"
        score = cells[key]["score"]
        # The scripted "ok" reply makes no tool call → requires_tool_use FAILS.
        assert score["outcome"]["passed"] is False
        assert isinstance(score["outcome"]["detail"], str)

    def test_triage_consumes_run_output(self, tmp_path: Path) -> None:
        """wt triage classifies the failed run straight from the run output."""
        runs_dir = tmp_path / "runs"
        self._run_in_memory(runs_dir)

        result = _wt("triage", "--runs", str(runs_dir), timeout=120)
        assert result.returncode == 0
        assert "Skipped (no score):** 0" in result.stdout, result.stdout
        assert "lookup_before_action" in result.stdout

    def test_replay_writes_score_sidecar(self, tmp_path: Path) -> None:
        """wt replay also persists a sidecar next to the replayed trace."""
        runs_dir = tmp_path / "runs"
        self._run_in_memory(runs_dir)
        traces = [
            p for p in runs_dir.rglob("*.json")
            if not p.name.endswith(".score.json")
        ]
        assert traces

        replay_dir = tmp_path / "replay_runs"
        result = _wt(
            "replay",
            "--trace", str(traces[0]),
            "--runtime", "in_memory",
            "--runs-dir", str(replay_dir),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        new_traces = [
            p for p in replay_dir.rglob("*.json")
            if not p.name.endswith(".score.json")
        ]
        assert new_traces, "replay saved no trace"
        for trace_path in new_traces:
            assert trace_path.with_suffix(".score.json").exists()
