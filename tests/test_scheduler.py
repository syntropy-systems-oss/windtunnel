"""Pluggable scheduling: the Scheduler SPI and how `wt run` drives it.

Unit tests pin the scheduler contract (order, concurrency bound, stop,
first-error propagation). CLI tests run the real run_scenario against a
probe runtime that records how many sends are in flight at once, which is
the property a runtime's declared ``max_concurrency`` protects.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario
from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.scheduler import (
    ConcurrentScheduler,
    RunJob,
    Scheduler,
    SequentialScheduler,
)

# ─── Scheduler contract ───────────────────────────────────────────────────────


class _Recorder:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started: list[int] = []

    def job(self, index: int, action: Any = None) -> RunJob:
        def _fn() -> None:
            with self.lock:
                self.started.append(index)
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(self.delay_s)
                if action is not None:
                    action()
            finally:
                with self.lock:
                    self.active -= 1

        return RunJob(index=index, name=f"s{index}", runs=1, fn=_fn)


class TestSequentialScheduler:
    def test_runs_every_job_once_in_order_one_at_a_time(self) -> None:
        recorder = _Recorder()
        SequentialScheduler().execute([recorder.job(i) for i in range(4)], threading.Event())
        assert recorder.started == [0, 1, 2, 3]
        assert recorder.peak == 1

    def test_ignores_a_requested_concurrency(self) -> None:
        assert SequentialScheduler(max_concurrency=8).max_concurrency == 1

    def test_starts_nothing_after_stop_is_set(self) -> None:
        recorder = _Recorder()
        stop = threading.Event()
        jobs = [recorder.job(0), recorder.job(1, stop.set), recorder.job(2)]
        SequentialScheduler().execute(jobs, stop)
        assert recorder.started == [0, 1]

    def test_a_job_exception_propagates_and_later_jobs_never_start(self) -> None:
        recorder = _Recorder()

        def _explode() -> None:
            raise OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            SequentialScheduler().execute(
                [recorder.job(0, _explode), recorder.job(1)], threading.Event()
            )
        assert recorder.started == [0]


class TestConcurrentScheduler:
    def test_runs_up_to_max_concurrency_jobs_at_once(self) -> None:
        recorder = _Recorder(delay_s=0.05)
        ConcurrentScheduler(max_concurrency=3).execute(
            [recorder.job(i) for i in range(7)], threading.Event()
        )
        assert sorted(recorder.started) == list(range(7))
        assert recorder.peak == 3

    def test_starts_jobs_in_order_and_none_after_stop(self) -> None:
        recorder = _Recorder()
        stop = threading.Event()
        jobs = [recorder.job(0, stop.set), *(recorder.job(i) for i in range(1, 5))]
        ConcurrentScheduler(max_concurrency=1).execute(jobs, stop)
        assert recorder.started == [0]

    def test_first_job_error_is_reraised_after_running_jobs_finish(self) -> None:
        recorder = _Recorder(delay_s=0.02)
        finished: list[int] = []

        def _explode() -> None:
            raise RuntimeError("persist failed")

        jobs = [
            recorder.job(0, _explode),
            recorder.job(1, lambda: finished.append(1)),
            *(recorder.job(i) for i in range(2, 8)),
        ]
        with pytest.raises(RuntimeError, match="persist failed"):
            ConcurrentScheduler(max_concurrency=2).execute(jobs, threading.Event())
        assert finished == [1]  # the job already running was allowed to finish
        assert len(recorder.started) < 8

    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_rejects_invalid_max_concurrency(self, value: object) -> None:
        with pytest.raises(ValueError, match="max_concurrency"):
            ConcurrentScheduler(max_concurrency=value)  # type: ignore[arg-type]


# ─── wt run integration ───────────────────────────────────────────────────────


class _ProbeRuntime:
    """Records in-flight sends; a scenario's prompt names its send delay."""

    accepts_runner_managed_mcps = False

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.provisions = 0

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> _ProbeHandle:
        with self.lock:
            self.provisions += 1
        return _ProbeHandle(self)


class _ProbeHandle:
    def __init__(self, runtime: _ProbeRuntime) -> None:
        self.runtime = runtime

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        runtime = self.runtime
        with runtime.lock:
            runtime.active += 1
            runtime.peak = max(runtime.peak, runtime.active)
        try:
            time.sleep(float(messages[-1]["content"]))
        finally:
            with runtime.lock:
                runtime.active -= 1
        return {"content": "ok"}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _Plugin:
    def __init__(self, max_concurrency: object = "absent") -> None:
        if max_concurrency != "absent":
            self.max_concurrency = max_concurrency


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delays: list[float],
    *argv: str,
    plugin: object | None = None,
) -> tuple[int, _ProbeRuntime]:
    import windtunnel.cli as cli

    runtime = _ProbeRuntime()
    scenarios = [
        Scenario(name=f"job_{i}", prompt=str(delay), target_facts=[["ok"]])
        for i, delay in enumerate(delays)
    ]
    monkeypatch.setattr(
        cli, "_discover_scenario_packs", lambda: [ScenarioPack(name="local", scenarios=scenarios)]
    )
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: plugin or _Plugin())
    monkeypatch.setattr(
        cli, "_build_runtime", lambda runtime_name, label, soul_path, **_kwargs: runtime
    )
    rc = cli.main(["run", "--runs-dir", str(tmp_path / "runs"), *argv])
    return rc, runtime


class TestWtRunScheduling:
    def test_sequential_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc, runtime = _run(monkeypatch, tmp_path, [0.02] * 3, plugin=_Plugin(None))
        assert rc == 0
        assert runtime.peak == 1

    def test_concurrent_runs_jobs_in_parallel_and_reports_in_selection_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out_path = tmp_path / "sweep.json"
        rc, runtime = _run(
            monkeypatch,
            tmp_path,
            [0.4, 0.15, 0.15],
            "--scheduler", "concurrent",
            "--max-concurrency", "3",
            "--format", "json",
            "--out", str(out_path),
            plugin=_Plugin(None),
        )
        captured = capsys.readouterr()
        summary = [line.split()[1] for line in captured.out.splitlines() if "pass, rate=" in line]
        assert rc == 0
        assert runtime.peak == 3
        assert runtime.provisions == 3
        assert summary[-1] == "job_0"  # the slow job finished last...
        records = json.loads(out_path.read_text(encoding="utf-8"))
        assert [r["scenario_id"] for r in records] == ["job_0", "job_1", "job_2"]  # ...but reports first
        assert "running up to 3 scenario jobs at once (concurrent scheduler)" in captured.err

    def test_a_plugin_that_declares_nothing_is_never_run_concurrently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, runtime = _run(
            monkeypatch,
            tmp_path,
            [0.02] * 3,
            "--scheduler", "concurrent",
            "--max-concurrency", "4",
        )
        assert rc == 0
        assert runtime.peak == 1
        assert "tolerates at most 1 concurrent job(s); running 1 at a time (requested 4)" in (
            capsys.readouterr().err
        )

    def test_the_declared_limit_caps_the_requested_concurrency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc, runtime = _run(
            monkeypatch,
            tmp_path,
            [0.05] * 5,
            "--scheduler", "concurrent",
            "--max-concurrency", "4",
            plugin=_Plugin(2),
        )
        assert rc == 0
        assert runtime.peak == 2

    def test_the_declared_limit_is_the_default_concurrency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc, runtime = _run(
            monkeypatch, tmp_path, [0.05] * 4, "--scheduler", "concurrent", plugin=_Plugin(2)
        )
        assert rc == 0
        assert runtime.peak == 2

    def test_max_concurrency_with_the_sequential_scheduler_is_called_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, runtime = _run(
            monkeypatch, tmp_path, [0.0], "--max-concurrency", "3", plugin=_Plugin(None)
        )
        assert rc == 0
        assert runtime.peak == 1
        assert "--max-concurrency 3 has no effect with the sequential scheduler" in (
            capsys.readouterr().err
        )

    def test_active_hooks_force_one_job_at_a_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, runtime = _run(
            monkeypatch,
            tmp_path,
            [0.02] * 3,
            "--scheduler", "concurrent",
            "--hook", "debrief",
            plugin=_Plugin(None),
        )
        assert rc == 0
        assert runtime.peak == 1
        assert "lifecycle hooks are active" in capsys.readouterr().err

    def test_a_custom_scheduler_class_is_loaded_from_a_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source = tmp_path / "reverse_scheduler.py"
        order_log = tmp_path / "order.log"
        source.write_text(
            "from pathlib import Path\n"
            "from windtunnel.spi import Scheduler\n"
            "class Reverse(Scheduler):\n"
            "    def execute(self, jobs, stop):\n"
            "        for job in reversed(list(jobs)):\n"
            f"            with Path({str(order_log)!r}).open('a') as log:\n"
            "                log.write(job.name + '\\n')\n"
            "            job()\n",
            encoding="utf-8",
        )
        out_path = tmp_path / "sweep.json"
        rc, _runtime = _run(
            monkeypatch,
            tmp_path,
            [0.0, 0.0, 0.0],
            "--scheduler", f"{source}:Reverse",
            "--format", "json",
            "--out", str(out_path),
            plugin=_Plugin(None),
        )
        assert rc == 0
        assert order_log.read_text().split() == ["job_2", "job_1", "job_0"]
        records = json.loads(out_path.read_text(encoding="utf-8"))
        assert [r["scenario_id"] for r in records] == ["job_0", "job_1", "job_2"]

    @pytest.mark.parametrize(
        ("spec", "message"),
        [
            ("fastest", "unknown scheduler 'fastest'"),
            ("json:dumps", "must name a windtunnel.spi.Scheduler subclass or instance"),
            ("no_such_module_xyz:Thing", "could not load scheduler"),
        ],
    )
    def test_an_unusable_scheduler_is_a_usage_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        spec: str,
        message: str,
    ) -> None:
        rc, runtime = _run(monkeypatch, tmp_path, [0.0], "--scheduler", spec)
        assert rc == 2
        assert message in capsys.readouterr().err
        assert runtime.provisions == 0

    def test_an_invalid_plugin_declaration_is_a_usage_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, _runtime = _run(monkeypatch, tmp_path, [0.0], plugin=_Plugin(0))
        assert rc == 2
        assert "declares max_concurrency=0" in capsys.readouterr().err

    def test_the_circuit_breaker_stops_starting_jobs_under_concurrency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.api.runner as runner

        started: list[str] = []
        lock = threading.Lock()

        def _failing(scenario: Scenario, *_args: object, **_kwargs: object) -> None:
            with lock:
                started.append(scenario.name)
            time.sleep(0.02)
            raise RuntimeError("inference worker down")

        monkeypatch.setattr(runner, "run_scenario", _failing)
        rc, _runtime = _run(
            monkeypatch,
            tmp_path,
            [0.0] * 8,
            "--scheduler", "concurrent",
            "--max-concurrency", "2",
            plugin=_Plugin(None),
        )
        assert rc == 1
        assert 3 <= len(started) < 8


def test_scheduler_is_an_abstract_contract() -> None:
    with pytest.raises(TypeError):
        Scheduler()  # type: ignore[abstract]
