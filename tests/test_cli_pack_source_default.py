"""`--pack-source` without `--pack` runs only the sourced pack(s).

Regression: an unfiltered `wt run --pack-source mine.py:PACK --runtime X`
used to sweep every registered scenario — all built-in dimensions — under a
runtime they were never written for, burying the one loaded pack under a
wall of meaningless failures.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario

_PACK_SOURCE = """
from windtunnel.api import Scenario, ScenarioPack

PACK = ScenarioPack(
    name="local_pack",
    scenarios=[
        Scenario(name="local_one", prompt="say ok", target_facts=[["ok"]]),
        Scenario(name="local_two", prompt="say ok", target_facts=[["ok"]]),
    ],
)
"""


def _ledger(runs_dir: Path) -> list[dict[str, Any]]:
    path = runs_dir / "ledger.ndjsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_sourced_pack_runs_alone_with_real_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import windtunnel.cli as cli

    source = tmp_path / "local_pack.py"
    source.write_text(_PACK_SOURCE, encoding="utf-8")
    runs_dir = tmp_path / "runs"

    rc = cli.main([
        "run", "--runtime", "in_memory",
        "--pack-source", f"{source}:PACK",
        "--runs-dir", str(runs_dir),
    ])

    err = capsys.readouterr().err
    assert rc == 0
    assert {row["scenario_id"] for row in _ledger(runs_dir)} == {"local_one", "local_two"}
    assert {row["pack"] for row in _ledger(runs_dir)} == {"local_pack"}
    assert (
        "wt run: --pack-source without --pack: running only local_pack "
        "(pass --all-packs to sweep every registered pack)"
    ) in err


class TestDefaultFilterSelection:
    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
        from windtunnel.api.runner import ScenarioResult
        from windtunnel.api.score import LayerResult, Score
        from windtunnel.api.trace import Trace

        builtin = ScenarioPack(
            name="builtin_dim",
            scenarios=[Scenario(name="builtin_scenario", prompt="q", target_facts=[["ok"]])],
        )
        sourced = ScenarioPack(
            name="local_pack",
            scenarios=[Scenario(name="local_scenario", prompt="q", target_facts=[["ok"]])],
        )
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda sources=None: [builtin, sourced])
        monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: object())
        monkeypatch.setattr(cli, "_build_runtime", lambda *_a, **_k: object())
        ran: list[str] = []

        def _fake(scenario: Scenario, *_args: object, **_kwargs: object) -> ScenarioResult:
            from datetime import UTC, datetime

            ran.append(scenario.name)
            now = datetime.now(UTC)
            run = ScenarioRunResult(
                score=Score(
                    outcome=LayerResult(True, "ok"),
                    trajectory=LayerResult(True, "ok"),
                    constraint=LayerResult(True, "ok"),
                    integrity=LayerResult(True, "ok"),
                ),
                trace=Trace(
                    scenario_id=scenario.name, agent_id="a", variant_id="v", model="m",
                    quant="q", sampler={}, started_at=now, finished_at=now, turns=[],
                    tool_schema_hash=None,
                ),
            )
            return ScenarioResult(aggregate=aggregate_runs([run]), runs=[run])

        monkeypatch.setattr(runner, "run_scenario", _fake)
        return ran

    def test_without_pack_only_the_sourced_pack_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        ran = self._wire(monkeypatch)
        rc = cli.main([
            "run", "--pack-source", "local.py:PACK", "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0
        assert ran == ["local_scenario"]

    def test_all_packs_opts_back_into_every_registered_pack(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        ran = self._wire(monkeypatch)
        rc = cli.main([
            "run", "--pack-source", "local.py:PACK", "--all-packs",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0
        assert ran == ["builtin_scenario", "local_scenario"]
        assert "running only" not in capsys.readouterr().err

    def test_an_explicit_pack_selection_wins_without_a_notice(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        ran = self._wire(monkeypatch)
        rc = cli.main([
            "run", "--pack-source", "local.py:PACK", "--pack", "builtin_dim",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0
        assert ran == ["builtin_scenario"]
        assert "running only" not in capsys.readouterr().err

    def test_a_scenario_outside_the_sourced_pack_is_explained_not_called_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        ran = self._wire(monkeypatch)
        rc = cli.main([
            "run", "--pack-source", "local.py:PACK", "--scenario", "builtin_scenario",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        err = capsys.readouterr().err
        assert rc == 2
        assert ran == []
        assert "running only local_pack (pass --all-packs" in err
        assert "unknown scenario" not in err
        assert "no scenarios found" in err

    def test_runs_without_pack_source_are_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        ran = self._wire(monkeypatch)
        rc = cli.main(["run", "--runs-dir", str(tmp_path / "runs")])
        assert rc == 0
        assert ran == ["builtin_scenario", "local_scenario"]
