"""`wt batch FILE`: queue `wt run` specs back to back with one command."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario


class _Runtime:
    accepts_runner_managed_mcps = False

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply

    def provision(self, config: Any, mcps: list[Any] | None = None) -> _Handle:
        return _Handle(self.reply)


class _Handle:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        return {"content": self.reply}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _Plugin:
    max_concurrency = None


def _wire(monkeypatch: pytest.MonkeyPatch, *, built: list[str] | None = None) -> None:
    """Two scenarios; runtime 'failing' answers wrongly so its sweeps exit 1."""
    import windtunnel.cli as cli

    scenarios = [
        Scenario(name="alpha", prompt="q", target_facts=[["ok"]]),
        Scenario(name="beta", prompt="q", target_facts=[["ok"]]),
    ]
    monkeypatch.setattr(
        cli, "_discover_scenario_packs", lambda: [ScenarioPack(name="local", scenarios=scenarios)]
    )
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: _Plugin())

    def _build(runtime_name: str, label: str, soul_path: str | None, **_kw: Any) -> _Runtime:
        if built is not None:
            built.append(label)
        return _Runtime("wrong" if runtime_name == "failing" else "ok")

    monkeypatch.setattr(cli, "_build_runtime", _build)


def _ledger_labels(runs_dir: Path) -> list[str]:
    ledger = runs_dir / "ledger.ndjsonl"
    return [json.loads(line)["label"] for line in ledger.read_text(encoding="utf-8").splitlines()]


def _batch(tmp_path: Path, content: str, *argv: str) -> int:
    import windtunnel.cli as cli

    spec_file = tmp_path / "rounds.txt"
    spec_file.write_text(content, encoding="utf-8")
    return cli.main(["batch", str(spec_file), *argv])


class TestWtBatch:
    def test_runs_every_spec_in_file_order_as_its_own_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        built: list[str] = []
        _wire(monkeypatch, built=built)
        runs_dir = tmp_path / "runs"

        rc = _batch(
            tmp_path,
            "# round one\n"
            "--label first --runs 2\n"
            "\n"
            "wt run --label second --scenario alpha   # a trailing comment\n",
            "--runs-dir", str(runs_dir),
        )

        err = capsys.readouterr().err
        assert rc == 0
        assert built == ["first", "second"]
        assert _ledger_labels(runs_dir) == ["first", "first", "second"]
        assert "[1/2]" in err and "rounds.txt:2: --label first --runs 2" in err
        assert "[2/2]" in err and "rounds.txt:4: --label second --scenario alpha" in err
        started = [
            json.loads(line)
            for line in (runs_dir / "events.ndjsonl").read_text(encoding="utf-8").splitlines()
            if '"sweep_started"' in line
        ]
        assert [event["label"] for event in started] == ["first", "second"]
        assert started[0]["sweep_id"] != started[1]["sweep_id"]

    def test_every_line_is_validated_before_anything_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        built: list[str] = []
        _wire(monkeypatch, built=built)

        rc = _batch(
            tmp_path,
            "--label fine\n--label typo --lable oops\n--runs 0\n--label 'unterminated\n",
            "--runs-dir", str(tmp_path / "runs"),
        )

        err = capsys.readouterr().err
        assert rc == 2
        assert built == []
        assert "rounds.txt:2: unrecognized arguments: --lable oops" in err
        assert "rounds.txt:3: argument --runs: must be at least 1" in err
        assert "rounds.txt:4: No closing quotation" in err
        assert "no spec was run" in err

    def test_a_failing_spec_does_not_stop_the_queue_and_sets_the_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _wire(monkeypatch)
        runs_dir = tmp_path / "runs"

        rc = _batch(
            tmp_path,
            "--label bad --runtime failing\n--label good\n",
            "--runs-dir", str(runs_dir),
        )

        err = capsys.readouterr().err
        assert rc == 1
        assert _ledger_labels(runs_dir) == ["bad", "bad", "good", "good"]
        assert "line 1    label bad" in err and "exit 1" in err
        assert "line 2    label good" in err

    def test_a_spec_that_exits_early_is_recorded_and_the_queue_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _wire(monkeypatch)
        runs_dir = tmp_path / "runs"

        rc = _batch(
            tmp_path,
            "--label empty --scenario no_such_scenario\n--label good\n",
            "--runs-dir", str(runs_dir),
        )

        assert rc == 2  # "no scenarios found" is a usage error for that spec
        assert _ledger_labels(runs_dir) == ["good", "good"]

    def test_batch_defaults_apply_and_a_specs_own_value_wins(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _wire(monkeypatch)
        shared, own = tmp_path / "shared", tmp_path / "own"

        rc = _batch(
            tmp_path,
            f"--label a\n--label b --runs-dir {own}\n",
            "--runs-dir", str(shared),
            "--scheduler", "concurrent",
        )

        assert rc == 0
        assert _ledger_labels(shared) == ["a", "a"]
        assert _ledger_labels(own) == ["b", "b"]
        started = [
            json.loads(line)
            for runs_dir in (shared, own)
            for line in (runs_dir / "events.ndjsonl").read_text(encoding="utf-8").splitlines()
            if '"sweep_started"' in line
        ]
        assert [event["scheduler"] for event in started] == ["concurrent", "concurrent"]

    def test_specs_can_come_from_standard_input(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        _wire(monkeypatch)
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr("sys.stdin", io.StringIO("--label piped\n"))

        rc = cli.main(["batch", "-", "--runs-dir", str(runs_dir)])

        assert rc == 0
        assert _ledger_labels(runs_dir) == ["piped", "piped"]

    def test_a_file_without_specs_or_a_missing_file_is_a_usage_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        assert _batch(tmp_path, "# nothing to do\n\n") == 2
        assert "contains no run specs" in capsys.readouterr().err
        assert cli.main(["batch", str(tmp_path / "missing.txt")]) == 2
        assert "cannot read" in capsys.readouterr().err
