"""`wt watch` follows one sweep's progress events and exits with its code."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from windtunnel._cli.events import EVENTS_FILENAME, SweepEvents


def _watch(capsys: pytest.CaptureFixture[str], runs_dir: Path, *argv: str) -> tuple[int, str, str]:
    import windtunnel.cli as cli

    rc = cli.main(["watch", "--runs", str(runs_dir), "--poll", "0.02", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _full_sweep(events: SweepEvents, *, verdict: str = "PASS", exit_code: int = 0) -> None:
    events.started(runtime="in_memory", scenarios=["lookup"], runs_per_scenario=1)
    _finish_sweep(events, verdict=verdict, exit_code=exit_code)


def _finish_sweep(events: SweepEvents, *, verdict: str = "PASS", exit_code: int = 0) -> None:
    events.emit("run_started", scenario_id="lookup", run=1, runs=1)
    events.emit(
        "run_finished",
        scenario_id="lookup",
        run=1,
        runs=1,
        run_id="r1",
        verdict=verdict,
        layers={"outcome": verdict == "PASS", "trajectory": True},
        metrics={"outcome.revisions": 3},
        trace="runs/lookup/x.json",
        duration_s=1.25,
    )
    events.emit(
        "scenario_finished",
        scenario_id="lookup",
        verdict=verdict,
        passed=int(verdict == "PASS"),
        total=1,
        pass_rate=float(verdict == "PASS"),
        gate_failure=verdict != "PASS",
    )
    events.emit(
        "sweep_finished",
        exit_code=exit_code,
        status="completed",
        scenarios=1,
        completed=1,
        errors=0,
    )


def _later(delay_s: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay_s, action)
    timer.start()
    return timer


def _append_raw(runs_dir: Path, event: dict[str, Any]) -> None:
    with (runs_dir / EVENTS_FILENAME).open("a", encoding="utf-8") as output:
        output.write(json.dumps(event) + "\n")


class TestWtWatch:
    def test_replays_a_finished_sweep_by_id_and_exits_with_its_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tmp_path.mkdir(exist_ok=True)
        events = SweepEvents(tmp_path, label="candidate")
        _full_sweep(events, verdict="FAIL", exit_code=1)

        rc, out, _err = _watch(capsys, tmp_path, "--sweep", events.sweep_id)

        lines = out.splitlines()
        assert rc == 1
        assert len(lines) == 5
        assert f"sweep {events.sweep_id} started: label candidate" in lines[0]
        assert "lookup run 1/1 started" in lines[1]
        assert "lookup run 1/1 FAIL (1.2s) failed: outcome outcome.revisions=3" in lines[2]
        assert "lookup FAIL 0/1 pass" in lines[3]
        assert "finished: exit 1 (completed; 1/1 scenario(s), 0 error(s))" in lines[4]

    def test_follows_an_active_sweep_until_it_finishes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        events = SweepEvents(tmp_path, label="candidate")
        events.started(runtime="in_memory", scenarios=["lookup"], runs_per_scenario=1)
        timer = _later(0.2, lambda: _finish_sweep(events))

        rc, out, _err = _watch(capsys, tmp_path, "--label", "candidate", "--timeout", "20")
        timer.join()

        assert rc == 0
        assert "started: label candidate" in out
        assert "lookup run 1/1 PASS" in out
        assert out.rstrip().endswith("(completed; 1/1 scenario(s), 0 error(s))")

    def test_waits_for_the_next_sweep_with_the_requested_label(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        other = SweepEvents(tmp_path, label="other")
        other.started(runtime="in_memory", scenarios=["lookup"], runs_per_scenario=1)
        wanted = SweepEvents(tmp_path, label="candidate")
        timer = _later(0.2, lambda: _full_sweep(wanted, verdict="FAIL", exit_code=1))

        rc, out, err = _watch(capsys, tmp_path, "--label", "candidate", "--timeout", "20")
        timer.join()

        assert rc == 1
        assert "waiting for a sweep with label 'candidate'" in err
        assert wanted.sweep_id in out
        assert other.sweep_id not in out

    def test_an_old_finished_sweep_is_not_reported_as_the_current_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = {"windtunnel_event": 1, "sweep_id": "0123456789ab", "label": "candidate"}
        _append_raw(tmp_path, {**base, "ts": "2020-01-01T00:00:00Z", "event": "sweep_started"})
        _append_raw(
            tmp_path,
            {**base, "ts": "2020-01-01T00:01:00Z", "event": "sweep_finished", "exit_code": 0},
        )

        rc, out, err = _watch(capsys, tmp_path, "--label", "candidate", "--timeout", "0.3")

        assert rc == 124
        assert out == ""
        assert "timed out after 0.3s (no sweep started)" in err

    def test_a_sweep_that_just_finished_is_still_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The race of a fast sweep finishing before `wt watch` starts."""
        events = SweepEvents(tmp_path, label="candidate")
        _full_sweep(events, exit_code=0)

        rc, out, _err = _watch(capsys, tmp_path, "--label", "candidate", "--timeout", "5")

        assert rc == 0
        assert "lookup run 1/1 PASS" in out

    @pytest.mark.skipif(os.name != "posix", reason="liveness probing is POSIX-only")
    def test_a_writer_that_died_mid_sweep_ends_the_watch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        _append_raw(
            tmp_path,
            {
                "windtunnel_event": 1,
                "ts": "2099-01-01T00:00:00Z",
                "event": "sweep_started",
                "sweep_id": "deadbeef0000",
                "label": "candidate",
                "pid": exited.pid,
                "host": socket.gethostname(),
            },
        )

        rc, _out, err = _watch(capsys, tmp_path, "--sweep", "deadbeef0000", "--timeout", "20")

        assert rc == 1
        assert "exited without finishing" in err

    @pytest.mark.skipif(os.name != "posix", reason="liveness probing is POSIX-only")
    def test_an_old_crashed_sweep_is_skipped_in_favor_of_waiting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        _append_raw(
            tmp_path,
            {
                "windtunnel_event": 1,
                "ts": "2020-01-01T00:00:00Z",
                "event": "sweep_started",
                "sweep_id": "crashed00000",
                "label": "candidate",
                "pid": exited.pid,
                "host": socket.gethostname(),
            },
        )

        rc, out, err = _watch(capsys, tmp_path, "--label", "candidate", "--timeout", "0.3")

        assert rc == 124
        assert out == ""
        assert "waiting for a sweep with label 'candidate'" in err

    def test_json_mode_prints_each_raw_event(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        events = SweepEvents(tmp_path, label="candidate")
        _full_sweep(events)

        rc, out, _err = _watch(capsys, tmp_path, "--sweep", events.sweep_id, "--json")

        parsed = [json.loads(line) for line in out.splitlines()]
        assert rc == 0
        assert [event["event"] for event in parsed] == [
            "sweep_started",
            "run_started",
            "run_finished",
            "scenario_finished",
            "sweep_finished",
        ]

    def test_lock_events_render_as_waiting_and_acquired_lines(self) -> None:
        from windtunnel._cli.watch import format_event

        base = {"windtunnel_event": 1, "ts": "2026-01-02T03:04:05Z", "sweep_id": "abc", "label": "x"}
        waiting = format_event(
            {**base, "event": "lock_waiting", "lock": "rt", "holder": {"pid": 42}}
        )
        acquired = format_event({**base, "event": "lock_acquired", "lock": "rt", "waited_s": 3.25})
        assert waiting.endswith("sweep abc waiting for runtime 'rt' held by pid 42")
        assert acquired.endswith("sweep abc acquired runtime 'rt' after 3.2s")

    def test_follows_a_real_background_wt_run_by_label(self, tmp_path: Path) -> None:
        """The agent workflow: start `wt run` in the background, then block on
        `wt watch`, which streams the sweep and returns its exit code."""
        runs_dir = tmp_path / "runs"
        command = [sys.executable, "-m", "windtunnel.cli"]
        run = subprocess.Popen(
            [
                *command, "run",
                "--runtime", "in_memory",
                "--scenario", "lookup_before_action",
                "--runs", "2",
                "--label", "background",
                "--runs-dir", str(runs_dir),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            watch = subprocess.run(
                [
                    *command, "watch",
                    "--runs", str(runs_dir),
                    "--label", "background",
                    "--poll", "0.05",
                    "--timeout", "60",
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
        finally:
            run_rc = run.wait(timeout=90)

        assert watch.returncode == run_rc == 1
        assert "lookup_before_action run 1/2 started" in watch.stdout
        assert "lookup_before_action run 2/2 FAIL" in watch.stdout
        assert "finished: exit 1" in watch.stdout
