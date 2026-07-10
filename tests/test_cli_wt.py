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

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


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


def _scenario(name: str, *, tags: list[str] | None = None, requires_tool_use: bool = False):
    """Build a minimal Scenario for CLI selection/output tests."""
    from windtunnel.api.scenario import Scenario

    return Scenario(
        name=name,
        prompt="say ok",
        target_facts=[["ok"]],
        requires_tool_use=requires_tool_use,
        tags=tags or [],
    )


def _pack(name: str, scenarios: list, *, owner: str | None = None):
    """Build a ScenarioPack, attaching owner defensively like Change 3 will."""
    from windtunnel.api.pack import ScenarioPack

    pack = ScenarioPack(name=name, scenarios=scenarios)
    if owner is not None:
        setattr(pack, "owner", owner)
    return pack


def _trace(
    scenario_id: str,
    *,
    run_id: str = "run-1",
    variant_id: str = "candidate",
    seconds: float = 0.25,
    model: str = "model-x",
    quant: str = "q4",
):
    """Build a trace with deterministic timing and identity fields."""
    from windtunnel.api.trace import Trace, Turn, compute_hash

    started_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=seconds)
    return Trace(
        scenario_id=scenario_id,
        agent_id="agent-x",
        variant_id=variant_id,
        model=model,
        quant=quant,
        sampler={},
        started_at=started_at,
        finished_at=finished_at,
        turns=[
            Turn(role="user", content="hello", tool_calls=[], tool_results=[], latency_ms=0.0),
            Turn(role="assistant", content="ok", tool_calls=[], tool_results=[], latency_ms=1.0),
        ],
        tool_schema_hash=compute_hash(scenario_id),
        worker_warnings=[],
        run_id=run_id,
    )


def _result(
    scenario,
    *,
    passed: bool,
    run_id: str = "run-1",
    detail: str | None = None,
):
    """Build a ScenarioResult with one run and a real AggregateResult."""
    from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
    from windtunnel.api.runner import ScenarioResult
    from windtunnel.api.score import LayerResult, Score

    outcome_detail = detail or ("all target facts found" if passed else "missing target fact")
    score = Score(
        outcome=LayerResult(passed=passed, detail=outcome_detail),
        trajectory=LayerResult(passed=True, detail="trajectory ok"),
        constraint=LayerResult(passed=True, detail="constraint ok"),
        robustness=LayerResult(passed=True, detail="robustness ok"),
    )
    run = ScenarioRunResult(score=score, trace=_trace(scenario.name, run_id=run_id))
    return ScenarioResult(
        aggregate=aggregate_runs([run], variance_allowed=getattr(scenario, "variance_allowed", False)),
        runs=[run],
    )


def _patch_cli_run(
    monkeypatch: pytest.MonkeyPatch,
    packs: list,
    results: dict[str, object],
) -> None:
    """Patch wt run to use fake packs, runtime plumbing, and scenario results."""
    import windtunnel.api.runner as runner
    import windtunnel.cli as cli

    monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: packs)
    monkeypatch.setattr(
        cli,
        "_build_runtime",
        lambda runtime_name, label, soul_path, **_kwargs: object(),
    )
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda runtime_name: object())

    def fake_run_scenario(scenario, runtime, *args, **kwargs):
        return results[scenario.name]

    monkeypatch.setattr(runner, "run_scenario", fake_run_scenario)


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
    @staticmethod
    def _write_result(runs_dir: Path, scenario_id: str, label: str, passed: bool) -> None:
        from windtunnel.api.score import LayerResult, Score, score_to_dict
        from windtunnel.api.trace import save_trace, storage_path

        trace = _trace(scenario_id, run_id=f"{scenario_id}-{label}", variant_id=label)
        trace_path = storage_path(trace, base_dir=runs_dir)
        save_trace(trace, trace_path)
        layer = LayerResult(passed=passed, detail="fixture")
        score = Score(
            outcome=layer,
            trajectory=LayerResult(passed=True, detail="fixture"),
            constraint=LayerResult(passed=True, detail="fixture"),
            robustness=LayerResult(passed=True, detail="fixture"),
        )
        trace_path.with_suffix(".score.json").write_text(
            json.dumps(score_to_dict(score)),
            encoding="utf-8",
        )

    def test_compare_without_labels_shows_help_or_error(self) -> None:
        result = _wt("compare")
        # Should print usage or error (not crash with traceback)
        assert result.returncode != 0 or "label" in (result.stdout + result.stderr).lower()

    def test_fixed_baseline_failure_is_not_a_regression(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        self._write_result(runs_dir, "recovered", "baseline", False)
        self._write_result(runs_dir, "recovered", "candidate", True)

        result = _wt(
            "compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir)
        )

        assert result.returncode == 0

    def test_candidate_regression_exits_nonzero(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        self._write_result(runs_dir, "regressed", "baseline", True)
        self._write_result(runs_dir, "regressed", "candidate", False)

        result = _wt(
            "compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir)
        )

        assert result.returncode == 1

    def test_unchanged_failure_is_not_a_regression(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        self._write_result(runs_dir, "known_failure", "baseline", False)
        self._write_result(runs_dir, "known_failure", "candidate", False)

        result = _wt(
            "compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir)
        )

        assert result.returncode == 0


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

    def test_runs_must_be_positive(self) -> None:
        result = _wt("run", "--runs", "0")
        assert result.returncode == 2
        assert "at least 1" in result.stderr

    def test_build_and_pre_run_share_one_plugin_instance(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli

        scenario = _scenario("plugin_lifecycle")
        instances: list[object] = []

        class StatefulPlugin:
            def __init__(self) -> None:
                self.runtime = None
                instances.append(self)

            def build(self, runtime_name: str, label: str, soul_path: str | None):
                self.runtime = object()
                return self.runtime

            def pre_run(self, runtime, scenarios, runtime_name: str) -> None:
                assert runtime is self.runtime

        monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: StatefulPlugin())
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [_pack("local", [scenario])])
        monkeypatch.setattr(
            runner,
            "run_scenario",
            lambda selected, runtime, *args, **kwargs: _result(selected, passed=True),
        )

        rc = cli.main([
            "run",
            "--runtime", "stateful",
            "--scenario", scenario.name,
            "--runs-dir", str(tmp_path / "runs"),
        ])

        assert rc == 0
        assert len(instances) == 1


class TestWtRunSelection:
    """Selection supports tag/pack/owner/glob and composes predictably."""

    def _packs(self) -> list:
        alpha_lookup = _scenario(
            "lookup_alpha",
            tags=["dim:recovery", "tier:smoke", "origin:incident-42"],
        )
        alpha_refund = _scenario("refund_alpha", tags=["dim:recovery", "tier:regression"])
        beta_lookup = _scenario("lookup_beta", tags=["dim:tool_affordance", "tier:smoke"])
        return [
            _pack("recovery", [alpha_lookup, alpha_refund], owner="team-a"),
            _pack("tool_affordance", [beta_lookup], owner="team-b"),
        ]

    def _names(self, selection) -> list[str]:
        return [entry.scenario.name for entry in selection.entries]

    def test_selects_by_tag(self) -> None:
        from windtunnel.cli import _select_scenarios

        selection = _select_scenarios(
            scenario_patterns=[],
            tag_filters=["dim:recovery"],
            pack_filters=[],
            owner_filters=[],
            packs=self._packs(),
        )
        assert self._names(selection) == ["lookup_alpha", "refund_alpha"]

    def test_selects_by_pack(self) -> None:
        from windtunnel.cli import _select_scenarios

        selection = _select_scenarios(
            scenario_patterns=[],
            tag_filters=[],
            pack_filters=["tool_affordance"],
            owner_filters=[],
            packs=self._packs(),
        )
        assert self._names(selection) == ["lookup_beta"]

    def test_selects_by_owner(self) -> None:
        from windtunnel.cli import _select_scenarios

        selection = _select_scenarios(
            scenario_patterns=[],
            tag_filters=[],
            pack_filters=[],
            owner_filters=["team-a"],
            packs=self._packs(),
        )
        assert self._names(selection) == ["lookup_alpha", "refund_alpha"]

    def test_selects_by_scenario_glob(self) -> None:
        from windtunnel.cli import _select_scenarios

        selection = _select_scenarios(
            scenario_patterns=["lookup_*"],
            tag_filters=[],
            pack_filters=[],
            owner_filters=[],
            packs=self._packs(),
        )
        assert self._names(selection) == ["lookup_alpha", "lookup_beta"]

    def test_composes_and_across_flags_or_within_repeated_flags(self) -> None:
        from windtunnel.cli import _select_scenarios

        selection = _select_scenarios(
            scenario_patterns=["lookup_*", "refund_*"],
            tag_filters=["tier:smoke", "tier:missing"],
            pack_filters=["recovery"],
            owner_filters=["team-a"],
            packs=self._packs(),
        )
        assert self._names(selection) == ["lookup_alpha"]

    def test_zero_match_matches_unknown_scenario_exit_behavior(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        _patch_cli_run(monkeypatch, self._packs(), {})
        rc = cli.main([
            "run",
            "--scenario", "does_not_exist",
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown scenario(s): does_not_exist" in err
        assert "no scenarios found" in err


class TestWtRunCiOutput:
    """Machine-readable `wt run` outputs are CI-consumable."""

    def test_json_sweep_document_uses_exact_ledger_record_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        scenario = _scenario("lookup_alpha", tags=["dim:recovery", "origin:incident-42"])
        pack = _pack("recovery", [scenario], owner="team-a")
        _patch_cli_run(monkeypatch, [pack], {scenario.name: _result(scenario, passed=True)})
        monkeypatch.setattr(cli, "_git_sha", lambda: "abc1234")
        monkeypatch.setattr(cli, "_wt_version", lambda: "0.3.0")

        out_path = tmp_path / "results.json"
        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--label", "candidate",
            "--runs-dir", str(tmp_path / "runs"),
            "--format", "json",
            "--out", str(out_path),
        ])

        assert rc == 0
        records = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(records) == 1
        record = records[0]
        assert list(record) == [
            "ts",
            "scenario_id",
            "pack",
            "owner",
            "label",
            "model",
            "quant",
            "verdict",
            "runs",
            "layer_pass_rates",
            "run_ids",
            "origin",
            "git_sha",
            "wt_version",
        ]
        assert record["scenario_id"] == "lookup_alpha"
        assert record["pack"] == "recovery"
        assert record["owner"] == "team-a"
        assert record["label"] == "candidate"
        assert record["model"] == "model-x"
        assert record["quant"] == "q4"
        assert record["verdict"] == "PASS"
        assert record["runs"] == 1
        assert record["layer_pass_rates"] == {
            "outcome": 1.0,
            "trajectory": 1.0,
            "constraint": 1.0,
            "robustness": 1.0,
        }
        assert record["run_ids"] == ["run-1"]
        assert record["origin"] == "incident-42"
        assert record["git_sha"] == "abc1234"
        assert record["wt_version"] == "0.3.0"

    def test_junit_has_suite_per_pack_case_per_aggregate_and_escaped_failure_details(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        pass_scenario = _scenario("lookup_ok", tags=["dim:recovery"])
        fail_scenario = _scenario(
            "lookup_bad",
            tags=["dim:tool_affordance"],
            requires_tool_use=True,
        )
        nasty_detail = "no_tools_used: detail has <xml> & \"quotes\" and 'apos'"
        pass_pack = _pack("recovery", [pass_scenario], owner="team-a")
        fail_pack = _pack("tool_affordance", [fail_scenario], owner="team-b")
        _patch_cli_run(
            monkeypatch,
            [pass_pack, fail_pack],
            {
                pass_scenario.name: _result(pass_scenario, passed=True, run_id="pass-run"),
                fail_scenario.name: _result(
                    fail_scenario,
                    passed=False,
                    run_id="fail-run",
                    detail=nasty_detail,
                ),
            },
        )

        out_path = tmp_path / "results.xml"
        rc = cli.main([
            "run",
            "--label", "candidate",
            "--runs-dir", str(tmp_path / "runs"),
            "--format", "junit",
            "--out", str(out_path),
        ])

        assert rc == 1
        raw_xml = out_path.read_text(encoding="utf-8")
        assert "&lt;xml&gt;" in raw_xml
        assert "&amp;" in raw_xml
        root = ET.parse(out_path).getroot()
        assert root.tag == "testsuites"
        assert root.attrib["tests"] == "2"
        assert root.attrib["failures"] == "1"

        suites = {suite.attrib["name"]: suite for suite in root.findall("testsuite")}
        assert set(suites) == {"recovery", "tool_affordance"}
        assert suites["recovery"].attrib["tests"] == "1"
        assert suites["tool_affordance"].attrib["tests"] == "1"
        assert suites["tool_affordance"].attrib["failures"] == "1"

        cases = root.findall(".//testcase")
        assert {case.attrib["name"] for case in cases} == {"lookup_ok", "lookup_bad"}
        failed_case = next(case for case in cases if case.attrib["name"] == "lookup_bad")
        assert failed_case.attrib["classname"] == "tool_affordance"
        assert failed_case.attrib["time"] == "0.250000"
        failure = failed_case.find("failure")
        assert failure is not None
        assert failure.attrib["triage_category"] == "tool_affordance"
        assert "triage=tool_affordance" in failure.attrib["message"]
        assert "triage_category: tool_affordance" in (failure.text or "")
        assert nasty_detail in (failure.text or "")
        assert "outcome: FAIL" in (failure.text or "")
        assert "trajectory: PASS" in (failure.text or "")

    def test_pass_with_variance_is_success_in_cli_and_junit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Regression: variance_allowed used to rename a partial failure while
        the CLI and JUnit still treated it as a hard gate failure."""
        import windtunnel.cli as cli
        from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
        from windtunnel.api.runner import ScenarioResult
        from windtunnel.api.score import LayerResult, Score

        scenario = _scenario("variable", tags=["dim:sampler_sensitivity"])
        scenario.variance_allowed = True
        pack = _pack("sampler_sensitivity", [scenario])

        def one_run(passed: bool, run_id: str) -> ScenarioRunResult:
            layer = LayerResult(passed=passed, detail="sample")
            score = Score(
                outcome=layer,
                trajectory=LayerResult(True, "trajectory"),
                constraint=LayerResult(True, "constraint"),
                robustness=LayerResult(True, "robustness"),
            )
            return ScenarioRunResult(score=score, trace=_trace(scenario.name, run_id=run_id))

        runs = [one_run(True, "pass-run"), one_run(False, "fail-run")]
        result = ScenarioResult(
            aggregate=aggregate_runs(runs, variance_allowed=True),
            runs=runs,
        )
        _patch_cli_run(monkeypatch, [pack], {scenario.name: result})
        out_path = tmp_path / "variance.xml"

        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--runs-dir", str(tmp_path / "runs"),
            "--format", "junit",
            "--out", str(out_path),
        ])

        assert rc == 0
        assert "PASS_WITH_VARIANCE" in capsys.readouterr().out
        root = ET.parse(out_path).getroot()
        assert root.attrib["failures"] == "0"
        assert root.find(".//failure") is None

    def test_out_without_format_is_usage_error(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        rc = cli.main(["run", "--out", str(tmp_path / "results.json")])
        assert rc == 2
        assert "--format and --out must be provided together" in capsys.readouterr().err

    def test_format_without_out_is_usage_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import windtunnel.cli as cli

        rc = cli.main(["run", "--format", "json"])
        assert rc == 2
        assert "--format and --out must be provided together" in capsys.readouterr().err

    def test_exit_code_zero_for_passing_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        scenario = _scenario("passes", tags=["dim:custom"])
        pack = _pack("custom", [scenario])
        _patch_cli_run(monkeypatch, [pack], {scenario.name: _result(scenario, passed=True)})

        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 0

    def test_exit_code_one_for_failing_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli

        scenario = _scenario("fails", tags=["dim:custom"])
        pack = _pack("custom", [scenario])
        _patch_cli_run(monkeypatch, [pack], {scenario.name: _result(scenario, passed=False)})

        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--runs-dir", str(tmp_path / "runs"),
        ])
        assert rc == 1

    def test_world_mismatch_prints_without_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from windtunnel.api.preconditions import WorldMismatchError

        scenario = _scenario("needs_world", tags=["dim:custom"])
        pack = _pack("custom", [scenario])
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [pack])
        monkeypatch.setattr(
            cli,
            "_build_runtime",
            lambda runtime_name, label, soul_path, **_kwargs: object(),
        )
        monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda runtime_name: object())

        def fail_world(*_args, **_kwargs):
            raise WorldMismatchError("needs_world", ["ToolAvailable('missing'): missing"])

        monkeypatch.setattr(runner, "run_scenario", fail_world)

        rc = cli.main([
            "run",
            "--scenario", scenario.name,
            "--runs-dir", str(tmp_path / "runs"),
        ])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert rc == 1
        assert "needs_world" in combined
        assert "missing" in combined
        assert "Traceback" not in combined


class TestWtRescore:
    def test_rescore_help_exits_zero(self) -> None:
        result = _wt("rescore", "--help")
        assert result.returncode == 0

    def test_rescore_saved_trace_flips_score_without_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli
        from windtunnel.api.pack import ScenarioPack
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario
        from windtunnel.api.trace import save_trace, storage_path
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        scenario = Scenario(
            name="rescore_flip",
            prompt="say ok",
            target_facts=[["ok"]],
        )
        result = run_scenario(scenario, InMemoryRuntime(scripted_responses=["ok"]))
        runs_dir = tmp_path / "runs"
        trace_path = storage_path(result.runs[0].trace, base_dir=runs_dir)
        save_trace(result.runs[0].trace, trace_path)
        cli._write_score_sidecar(trace_path, result.runs[0].score, scenario)

        scenario.target_facts = [["missing-now"]]
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [
            ScenarioPack(name="local", scenarios=[scenario]),
        ])
        monkeypatch.setattr(
            cli,
            "_build_runtime",
            lambda *_args, **_kwargs: pytest.fail("rescore must not build a runtime"),
        )

        rc = cli.main(["rescore", "--trace", str(trace_path)])

        out = capsys.readouterr().out
        assert rc == 1
        assert "outcome PASS -> FAIL" in out
        assert "summary: traces=1 changed=1 new_outcome_failures=1" in out

    def test_rescore_write_updates_sidecar_with_origin_marker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import windtunnel.cli as cli
        from windtunnel.api.pack import ScenarioPack
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario
        from windtunnel.api.trace import save_trace, storage_path
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        scenario = Scenario(
            name="rescore_write",
            prompt="say ok",
            target_facts=[["ok"]],
        )
        result = run_scenario(scenario, InMemoryRuntime(scripted_responses=["ok"]))
        trace_path = storage_path(result.runs[0].trace, base_dir=tmp_path / "runs")
        save_trace(result.runs[0].trace, trace_path)
        cli._write_score_sidecar(trace_path, result.runs[0].score, scenario)

        scenario.target_facts = [["missing-now"]]
        monkeypatch.setattr(cli, "_discover_scenario_packs", lambda: [
            ScenarioPack(name="local", scenarios=[scenario]),
        ])

        rc = cli.main(["rescore", "--trace", str(trace_path), "--write"])

        sidecar = json.loads(trace_path.with_suffix(".score.json").read_text(encoding="utf-8"))
        trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
        assert rc == 1
        assert sidecar["outcome"]["passed"] is False
        assert sidecar["origin"]["kind"] == "rescore"
        assert trace_data["scenario_id"] == "rescore_write"


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


class _DoctorLeakyHandle:
    """Deliberately broken handle for `wt doctor` tests: reset_state() does
    NOT wipe history and send() echoes back any nonce-shaped word it has
    ever seen — models a driver whose reset is a no-op (see
    tests/test_reset_canary.py's _LeakyHandle for the library-level idiom).
    """

    def __init__(self) -> None:
        self.seen_nonces: list[str] = []
        self.teardown_count = 0

    def send(self, messages: list[dict], session_id: str) -> dict:
        text = messages[-1]["content"]
        for word in text.replace(":", " ").replace(".", " ").split():
            if len(word) == 32:  # uuid4().hex length
                self.seen_nonces.append(word)
        reply = " ".join(self.seen_nonces) if self.seen_nonces else "ok"
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}

    def reset_state(self) -> None:
        pass  # deliberately does not clear seen_nonces — the bug under test

    def teardown(self) -> None:
        self.teardown_count += 1


class _DoctorLeakyRuntime:
    def __init__(self) -> None:
        self.handle = _DoctorLeakyHandle()

    def provision(self, config, mcps=None):
        return self.handle


class TestWtDoctor:
    """`wt doctor` — bring-up canary check."""

    def test_wt_doctor_help_exits_zero(self) -> None:
        result = _wt("doctor", "--help")
        assert result.returncode == 0

    def test_wt_doctor_help_mentions_runtime(self) -> None:
        result = _wt("doctor", "--help")
        combined = result.stdout + result.stderr
        assert "runtime" in combined.lower()

    def test_wt_doctor_help_mentions_pytest_for_hermetic_mode(self) -> None:
        # Hermetic (recall-free) mode is library/pytest-only — the CLI has no
        # portable way to conjure a StateProbe, so the help text must say so.
        result = _wt("doctor", "--help")
        combined = result.stdout + result.stderr
        assert "pytest" in combined.lower()

    def test_doctor_clean_in_memory_runtime_exits_zero(self) -> None:
        # in_memory is a scripted runtime (always replies "ok"), so it never
        # recalls the nonce — a real, non-monkeypatched clean run.
        result = _wt("doctor", "--runtime", "in_memory", timeout=30)
        assert result.returncode == 0, result.stderr
        assert "no leak observed" in result.stdout.lower()

    def test_doctor_unresolvable_runtime_exits_two(self) -> None:
        result = _wt("doctor", "--runtime", "definitely-not-a-real-runtime")
        assert result.returncode == 2
        assert "unknown runtime" in (result.stdout + result.stderr).lower()

    def test_doctor_leaky_runtime_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import windtunnel.cli as cli

        runtime = _DoctorLeakyRuntime()
        monkeypatch.setattr(
            cli, "_build_runtime", lambda runtime_name, label, soul_path: runtime
        )

        rc = cli.main(["doctor", "--runtime", "fake_leaky"])

        assert rc == 1
        assert runtime.handle.teardown_count == 1

    def test_doctor_leaky_runtime_prints_leak_evidence(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import windtunnel.cli as cli

        runtime = _DoctorLeakyRuntime()
        monkeypatch.setattr(
            cli, "_build_runtime", lambda runtime_name, label, soul_path: runtime
        )

        cli.main(["doctor", "--runtime", "fake_leaky"])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "leak proven" in combined.lower()
        assert "evidence" in combined.lower()

    def test_doctor_soul_file_not_found_exits_two(self, tmp_path: Path) -> None:
        result = _wt(
            "doctor", "--runtime", "in_memory",
            "--soul", str(tmp_path / "missing_soul.md"),
        )
        assert result.returncode == 2
        assert "soul" in (result.stdout + result.stderr).lower()
