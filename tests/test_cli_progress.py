"""`wt run` writes each run as it completes and narrates the sweep as events.

These drive the real run_scenario through `cli.main` with a probe runtime
whose handle looks at the runs directory from inside `send()`: what is on
disk while a later run is executing is exactly what an operator (or
`wt watch` / `wt results`) could see mid-sweep.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import LayerResult
from windtunnel.spi.agent_runtime import AgentConfig


class _ProbeHandle:
    def __init__(self, runtime: _ProbeRuntime) -> None:
        self._runtime = runtime

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        runs_dir = self._runtime.runs_dir
        ledger = runs_dir / "ledger.ndjsonl"
        self._runtime.seen.append(
            {
                "sidecars": len(list(runs_dir.rglob("*.score.json"))),
                "ledger_rows": (
                    len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
                ),
            }
        )
        if self._runtime.fail_send:
            raise RuntimeError("endpoint unreachable")
        return {"content": "ok"}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _ProbeRuntime:
    accepts_runner_managed_mcps = False

    def __init__(self, runs_dir: Path, *, fail_send: bool = False, fail_provision: bool = False):
        self.runs_dir = runs_dir
        self.fail_send = fail_send
        self.fail_provision = fail_provision
        self.seen: list[dict[str, int]] = []

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> _ProbeHandle:
        if self.fail_provision:
            raise RuntimeError("provision failed")
        return _ProbeHandle(self)


def _graded(_trace: Any) -> LayerResult:
    return LayerResult(True, "graded", metrics={"revisions": 2})


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    runtime: object,
    scenarios: list[Scenario],
    *,
    plugin: object | None = None,
) -> None:
    import windtunnel.cli as cli

    monkeypatch.setattr(
        cli, "_discover_scenario_packs", lambda: [ScenarioPack(name="local", scenarios=scenarios)]
    )
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: plugin or object())
    monkeypatch.setattr(
        cli, "_build_runtime", lambda runtime_name, label, soul_path, **_kwargs: runtime
    )


def _events(runs_dir: Path) -> list[dict[str, Any]]:
    path = runs_dir / "events.ndjsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestPerRunPersistence:
    def test_each_run_is_on_disk_before_the_next_run_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        runtime = _ProbeRuntime(runs_dir)
        _wire(monkeypatch, runtime, [Scenario(name="streamed", prompt="q", target_facts=[["ok"]])])

        rc = cli.main(["run", "--runs", "3", "--runs-dir", str(runs_dir)])

        assert rc == 0
        assert [seen["sidecars"] for seen in runtime.seen] == [0, 1, 2]
        traces = [p for p in runs_dir.rglob("*.json") if "." not in p.stem]
        assert len(traces) == 3
        assert all(p.with_suffix(".score.json").is_file() for p in traces)

    def test_a_scenarios_ledger_row_lands_before_the_next_scenario_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        runtime = _ProbeRuntime(runs_dir)
        scenarios = [
            Scenario(name="first", prompt="q", target_facts=[["ok"]]),
            Scenario(name="second", prompt="q", target_facts=[["ok"]]),
        ]
        _wire(monkeypatch, runtime, scenarios)

        rc = cli.main(["run", "--runs-dir", str(runs_dir)])

        assert rc == 0
        assert [seen["ledger_rows"] for seen in runtime.seen] == [0, 1]
        assert len((runs_dir / "ledger.ndjsonl").read_text(encoding="utf-8").splitlines()) == 2

    def test_runs_from_a_replacement_run_scenario_are_still_saved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run_scenario stand-in that ignores the per-run callbacks still
        gets every run persisted, to the same layout, after it returns."""
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        scenario = Scenario(name="replaced", prompt="q", target_facts=[["ok"]])
        finished = runner.run_scenario(
            scenario, InMemoryRuntime(scripted_responses=["ok"]), runs_per_scenario=2
        )
        _wire(monkeypatch, object(), [scenario])
        monkeypatch.setattr(runner, "run_scenario", lambda *_a, **_k: finished)
        runs_dir = tmp_path / "runs"

        rc = cli.main(["run", "--runs-dir", str(runs_dir)])

        assert rc == 0
        assert len(list(runs_dir.rglob("*.score.json"))) == 2
        finished_events = [e for e in _events(runs_dir) if e["event"] == "run_finished"]
        assert [e["run"] for e in finished_events] == [1, 2]

    def test_a_persistence_failure_mid_scenario_still_escapes_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        runtime = _ProbeRuntime(runs_dir)
        post_runs: list[str] = []

        class Plugin:
            def post_run(self, runtime: object, scenarios: list, runtime_name: str) -> None:
                post_runs.append(runtime_name)

        _wire(
            monkeypatch,
            runtime,
            [Scenario(name="unsaveable", prompt="q", target_facts=[["ok"]])],
            plugin=Plugin(),
        )

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("sidecar write exploded")

        monkeypatch.setattr(cli, "_write_score_sidecar", _boom)

        with pytest.raises(RuntimeError, match="sidecar write exploded"):
            cli.main(["run", "--runs", "3", "--runs-dir", str(runs_dir)])

        assert len(runtime.seen) == 1  # the remaining runs never started
        assert post_runs == ["in_memory"]
        (finished,) = [e for e in _events(runs_dir) if e["event"] == "sweep_finished"]
        assert finished["status"] == "error"
        assert finished["exit_code"] is None


class TestSweepEvents:
    def test_a_sweep_narrates_start_runs_scenario_and_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        runtime = _ProbeRuntime(runs_dir)
        _wire(monkeypatch, runtime, [Scenario(name="told", prompt="q", outcome_fn=_graded)])

        rc = cli.main(["run", "--runs", "2", "--label", "candidate", "--runs-dir", str(runs_dir)])

        events = _events(runs_dir)
        assert rc == 0
        assert [e["event"] for e in events] == [
            "sweep_started",
            "run_started",
            "run_finished",
            "run_started",
            "run_finished",
            "scenario_finished",
            "sweep_finished",
        ]
        sweep_id = events[0]["sweep_id"]
        assert all(e["sweep_id"] == sweep_id for e in events)
        assert all(e["label"] == "candidate" and e["windtunnel_event"] == 1 for e in events)
        assert events[0]["scenarios"] == ["told"]
        assert events[0]["runs_per_scenario"] == 2
        assert isinstance(events[0]["pid"], int)
        run = events[2]
        assert (run["scenario_id"], run["run"], run["runs"]) == ("told", 1, 2)
        assert run["verdict"] == "PASS"
        assert run["layers"]["outcome"] is True
        assert run["metrics"] == {"outcome.revisions": 2}
        assert Path(run["trace"]).is_file()
        assert events[5]["verdict"] == "PASS"
        assert (events[5]["passed"], events[5]["total"]) == (2, 2)
        assert events[-1]["exit_code"] == 0
        assert events[-1]["status"] == "completed"
        assert f"--sweep {sweep_id}" in capsys.readouterr().err

    def test_scenario_errors_and_breaker_abort_are_narrated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        runtime = _ProbeRuntime(runs_dir, fail_provision=True)
        scenarios = [Scenario(name=f"broken_{i}", prompt="q") for i in range(4)]
        _wire(monkeypatch, runtime, scenarios)

        rc = cli.main(["run", "--runs-dir", str(runs_dir)])

        events = _events(runs_dir)
        assert rc == 1
        errors = [e for e in events if e["event"] == "scenario_error"]
        assert [e["scenario_id"] for e in errors] == ["broken_0", "broken_1", "broken_2"]
        assert errors[0]["error"] == "RuntimeError: provision failed"
        assert events[-1]["event"] == "sweep_finished"
        assert events[-1]["status"] == "aborted"
        assert (events[-1]["exit_code"], events[-1]["errors"]) == (1, 3)
