"""`wt results` summaries and `wt compare` metric deltas over saved runs.

Runs are produced by the real scoring pipeline (run_scenario against the
in-memory runtime) and persisted with the same sidecar writer `wt run` uses,
so these tests read exactly what a sweep leaves on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtunnel.api.aggregate import MetricSummary
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import LayerResult
from windtunnel.api.trace import Trace


def _graded(trace: Trace) -> LayerResult:
    answer = trace.turns[-1].content
    correct = "yes" in answer
    return LayerResult(
        passed=correct,
        detail=f"answer={answer!r}",
        metrics={
            "final_correct": correct,
            "words": len(answer.split()),
            "path": answer.split()[0],
        },
    )


def _scenario(name: str = "lookup_order") -> Scenario:
    return Scenario(name=name, prompt="question", outcome_fn=_graded)


def _save_run(runs_dir: Path, scenario: Scenario, label: str, answer: str) -> tuple[Path, str]:
    """Run once with a scripted answer and persist it like `wt run` does."""
    from windtunnel._cli.storage import _write_score_sidecar
    from windtunnel.api.runner import run_scenario
    from windtunnel.api.trace import save_trace, storage_path
    from windtunnel.runtimes.in_memory import InMemoryRuntime
    from windtunnel.spi.agent_runtime import AgentConfig

    result = run_scenario(
        scenario,
        InMemoryRuntime(scripted_responses=[answer]),
        config=AgentConfig(agent_id="wt-cli", variant_id=label),
    )
    run = result.runs[0]
    path = storage_path(run.trace, base_dir=runs_dir)
    save_trace(run.trace, path)
    _write_score_sidecar(path, run.score, scenario)
    return path, run.trace.run_id


def _results(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    import windtunnel.cli as cli

    rc = cli.main(["results", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


class TestWtResults:
    def test_json_reports_pass_counts_metric_aggregates_and_trace_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        scenario = _scenario()
        paths = [
            _save_run(runs_dir, scenario, "candidate", answer)[0]
            for answer in ["yes direct", "yes retry again", "no direct"]
        ]

        rc, out, _err = _results(
            capsys, "--runs", str(runs_dir), "--label", "candidate", "--json"
        )

        assert rc == 0
        document = json.loads(out)
        assert document["windtunnel_results"] == 1
        assert document["labels"] == ["candidate"]
        (row,) = document["results"]
        assert row["scenario_id"] == "lookup_order"
        assert row["label"] == "candidate"
        assert (row["runs"], row["passed"], row["failed"], row["invalid"]) == (3, 2, 1, 0)
        assert row["verdict"] == "FAIL"
        assert row["selection"] == "all_traces"
        assert row["metrics"]["outcome.final_correct"] == {
            "kind": "bool",
            "count": 3,
            "true_count": 2,
            "rate": pytest.approx(2 / 3),
        }
        assert row["metrics"]["outcome.words"]["mean"] == pytest.approx(7 / 3)
        assert row["metrics"]["outcome.words"]["min"] == 2
        assert row["metrics"]["outcome.words"]["max"] == 3
        assert row["metrics"]["outcome.path"]["counts"] == {"yes": 2, "no": 1}
        assert sorted(trace["path"] for trace in row["traces"]) == sorted(map(str, paths))
        assert {trace["verdict"] for trace in row["traces"]} == {"PASS", "FAIL"}
        assert all("outcome.words" in trace["metrics"] for trace in row["traces"])

    def test_text_output_lists_each_scenario_and_metric(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        _save_run(runs_dir, _scenario("alpha"), "candidate", "yes direct")
        _save_run(runs_dir, _scenario("beta"), "candidate", "no direct")

        rc, out, _err = _results(capsys, "--runs", str(runs_dir), "--label", "candidate")

        assert rc == 0
        assert "label candidate: 2 scenario(s), 2 run(s)" in out
        assert "alpha" in out and "1/1 pass (100%)" in out
        assert "beta" in out and "0/1 pass (0%)" in out
        assert "outcome.final_correct  100% true (1/1)" in out
        assert "outcome.words" in out and "mean 2  min 2  max 2  (n=1)" in out

    def test_a_ledger_aggregate_selects_the_latest_sweep_of_a_reused_label(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from windtunnel._cli.storage import _append_ledger_records

        runs_dir = tmp_path / "runs"
        scenario = _scenario()
        _save_run(runs_dir, scenario, "candidate", "no first sweep")
        _path, latest_run_id = _save_run(runs_dir, scenario, "candidate", "yes second sweep")
        _append_ledger_records(
            runs_dir,
            [
                {
                    "windtunnel_ledger": 1,
                    "scenario_id": scenario.name,
                    "label": "candidate",
                    "verdict": "PASS",
                    "run_ids": [latest_run_id],
                }
            ],
        )

        rc, out, _err = _results(capsys, "--runs", str(runs_dir), "--json")

        (row,) = json.loads(out)["results"]
        assert rc == 0
        assert row["selection"] == "latest_aggregate"
        assert (row["runs"], row["passed"], row["verdict"]) == (1, 1, "PASS")
        assert [trace["run_id"] for trace in row["traces"]] == [latest_run_id]

    def test_without_label_every_label_is_summarized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        _save_run(runs_dir, _scenario(), "baseline", "no direct")
        _save_run(runs_dir, _scenario(), "candidate", "yes direct")

        rc, out, _err = _results(capsys, "--runs", str(runs_dir), "--json")

        document = json.loads(out)
        assert rc == 0
        assert document["labels"] == ["baseline", "candidate"]
        assert [row["label"] for row in document["results"]] == ["baseline", "candidate"]

    def test_a_repeated_label_is_summarized_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        _save_run(runs_dir, _scenario(), "candidate", "yes")

        rc, out, _err = _results(
            capsys, "--runs", str(runs_dir), "--label", "candidate", "--label", "candidate", "--json"
        )

        assert rc == 0
        assert len(json.loads(out)["results"]) == 1

    def test_scenario_filter_narrows_the_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        _save_run(runs_dir, _scenario("lookup_a"), "candidate", "yes")
        _save_run(runs_dir, _scenario("refund_b"), "candidate", "yes")

        rc, out, _err = _results(
            capsys, "--runs", str(runs_dir), "--scenario", "lookup_*", "--json"
        )

        assert rc == 0
        assert [row["scenario_id"] for row in json.loads(out)["results"]] == ["lookup_a"]

    def test_unknown_label_exits_two_and_names_the_labels_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = tmp_path / "runs"
        _save_run(runs_dir, _scenario(), "baseline", "yes")

        rc, _out, err = _results(capsys, "--runs", str(runs_dir), "--label", "typo")

        assert rc == 2
        assert "no runs with label(s): typo" in err
        assert "labels present: baseline" in err

    def test_missing_runs_dir_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _out, err = _results(capsys, "--runs", str(tmp_path / "nope"))

        assert rc == 2
        assert "runs directory not found" in err


class TestCompareMetricDeltas:
    def _two_labels(self, runs_dir: Path) -> None:
        scenario = _scenario()
        for answer in ["no direct", "yes retry again"]:
            _save_run(runs_dir, scenario, "baseline", answer)
        for answer in ["yes direct", "yes direct"]:
            _save_run(runs_dir, scenario, "candidate", answer)

    def test_text_output_adds_metric_deltas_after_the_verdict_table(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        self._two_labels(runs_dir)

        rc = cli.main(["compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "Metric deltas (vs baseline):" in out
        assert "outcome.final_correct  rate 50% -> 100% (+50pp)" in out
        assert "outcome.words" in out and "mean 2.5 -> 2 (-0.5)" in out

    def test_json_output_carries_verdicts_changes_and_metric_deltas(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        self._two_labels(runs_dir)

        rc = cli.main([
            "compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir), "--json",
        ])

        document = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert document["windtunnel_compare"] == 1
        assert document["baseline"] == "baseline"
        assert document["regression"] is False
        assert document["scenarios"][0]["scenario_id"] == "lookup_order"
        deltas = {entry["metric"]: entry for entry in document["metric_deltas"]}
        assert deltas["outcome.final_correct"]["kind"] == "bool"
        assert deltas["outcome.final_correct"]["delta"] == pytest.approx(0.5)
        assert deltas["outcome.words"]["delta"] == pytest.approx(-0.5)
        assert deltas["outcome.path"]["kind"] == "string"
        assert deltas["outcome.path"]["label"] == "candidate"
        assert deltas["outcome.path"]["baseline_label"] == "baseline"

    def test_metric_movement_never_changes_the_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only verdict regressions gate; a worse metric is reported, not failed."""
        import windtunnel.cli as cli

        runs_dir = tmp_path / "runs"
        scenario = _scenario()
        _save_run(runs_dir, scenario, "baseline", "yes a")
        _save_run(runs_dir, scenario, "candidate", "yes a b c d")

        rc = cli.main(["compare", "--labels", "baseline", "candidate", "--runs", str(runs_dir)])

        assert rc == 0
        assert "mean 2 -> 5 (+3)" in capsys.readouterr().out


class TestMetricDelta:
    def test_number_delta_is_the_difference_of_means(self) -> None:
        from windtunnel._report.model import metric_delta

        entry = metric_delta(
            MetricSummary(kind="number", count=2, mean=4.0, min=3, max=5),
            MetricSummary(kind="number", count=2, mean=6.5, min=6, max=7),
        )
        assert entry["kind"] == "number"
        assert entry["delta"] == pytest.approx(2.5)

    def test_string_delta_is_a_per_value_count_difference(self) -> None:
        from windtunnel._report.model import metric_delta

        entry = metric_delta(
            MetricSummary(kind="string", count=3, counts={"a": 2, "b": 1}),
            MetricSummary(kind="string", count=3, counts={"a": 1, "c": 2}),
        )
        assert entry["delta"] == {"a": -1, "b": -1, "c": 2}

    def test_missing_side_or_kind_mismatch_has_no_delta(self) -> None:
        from windtunnel._report.model import metric_delta

        only_candidate = metric_delta(None, MetricSummary(kind="bool", count=1, rate=1.0))
        assert only_candidate["baseline"] is None
        assert only_candidate["delta"] is None
        mismatch = metric_delta(
            MetricSummary(kind="bool", count=1, true_count=1, rate=1.0),
            MetricSummary(kind="number", count=1, mean=1.0, min=1, max=1),
        )
        assert mismatch["kind"] == "mixed"
        assert mismatch["delta"] is None
