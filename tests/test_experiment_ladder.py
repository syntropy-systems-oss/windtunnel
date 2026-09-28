"""The experiment ladder: derived tiers, budgets, fingerprints, and the gate.

See docs/design/0005-experiment-ladder.md. The gate has no bypass, so these
tests pin down exactly when it refuses and what satisfies it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_cli_wt import _pack, _patch_cli_run, _result, _scenario
from windtunnel._cli.ladder import (
    DEFAULT_TIER_CAPS_S,
    Budget,
    Fingerprint,
    LadderError,
    check_regression_gate,
    derive_tier,
    git_worktree_tree,
    load_tier_caps,
    prediction_line,
    resolve_budget,
    tier_priority,
)

# ─── Tiers and budgets ────────────────────────────────────────────────────────


class TestTiers:
    def test_a_replayed_trace_is_a_probe_whatever_it_selects(self) -> None:
        assert derive_tier(from_trace=True, selected_count=1) == "probe"

    def test_one_scenario_is_focused(self) -> None:
        assert derive_tier(from_trace=False, selected_count=1) == "focused"

    def test_several_scenarios_are_a_regression(self) -> None:
        assert derive_tier(from_trace=False, selected_count=2) == "regression"

    def test_probes_queue_ahead_of_focused_ahead_of_regression(self) -> None:
        assert tier_priority("probe") < tier_priority("focused") < tier_priority("regression")


class TestBudgets:
    def test_focused_defaults_to_its_cap(self) -> None:
        assert resolve_budget("focused", None, dict(DEFAULT_TIER_CAPS_S)) == 900.0

    def test_regression_is_unbounded_unless_asked(self) -> None:
        assert resolve_budget("regression", None, dict(DEFAULT_TIER_CAPS_S)) is None
        assert resolve_budget("regression", 60.0, dict(DEFAULT_TIER_CAPS_S)) == 60.0

    def test_a_budget_may_lower_the_cap(self) -> None:
        assert resolve_budget("probe", 30.0, dict(DEFAULT_TIER_CAPS_S)) == 30.0

    def test_a_budget_may_not_raise_the_cap(self) -> None:
        with pytest.raises(LadderError, match="exceeds the focused tier cap"):
            resolve_budget("focused", 3600.0, dict(DEFAULT_TIER_CAPS_S))

    def test_caps_come_from_the_nearest_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder]\nprobe_budget_s = 60\nfocused_budget_s = 1200\n"
        )
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        caps = load_tier_caps(nested)
        assert caps["probe"] == 60.0
        assert caps["focused"] == 1200.0
        assert caps["regression"] is None

    def test_an_invalid_cap_is_refused_not_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder]\nfocused_budget_s = -5\n"
        )
        with pytest.raises(LadderError, match="focused_budget_s"):
            load_tier_caps(tmp_path)

    def test_the_budget_latches_once_spent(self) -> None:
        now = [0.0]
        budget = Budget(10.0, clock=lambda: now[0])
        budget.start()
        assert budget.allows_start()
        now[0] = 10.0
        assert not budget.allows_start()
        assert budget.exhausted

    def test_an_unstarted_budget_never_blocks(self) -> None:
        """Time queued for the runtime lock is not charged to the sweep."""
        assert Budget(1.0, clock=lambda: 1e9).allows_start()

    def test_prediction_line_reports_held_and_missed(self) -> None:
        assert prediction_line("pass", True) == "wt run: prediction held: expected pass, got pass"
        assert "MISSED" in (prediction_line("fail", True) or "")
        assert prediction_line(None, True) is None


# ─── The evidence gate ────────────────────────────────────────────────────────


def _row(
    scenario: str,
    verdict: str,
    *,
    tier: str,
    sweep: str,
    fingerprint: str = "fp-1",
    target: str = "rt",
    pack: str = "p",
    **experiment: Any,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "pack": pack,
        "verdict": verdict,
        "sweep_id": sweep,
        "ts": "2026-01-01T00:00:00Z",  # deliberately equal: order is the ledger's
        "experiment": {
            "tier": tier,
            "fingerprint": fingerprint,
            "fingerprint_parts": {"runtime": target, "target": target},
            **experiment,
        },
    }


def _gate(rows, *names: str, fingerprint: str = "fp-1", target: str = "rt"):  # noqa: ANN001, ANN202
    return check_regression_gate(
        rows, selected=[("p", name) for name in names], fingerprint=fingerprint, target=target
    )


def _earn(sweep: str = "e1", fingerprint: str = "fp-1") -> dict[str, Any]:
    """A passing focused run of an unrelated scenario: it earns a full run."""
    return _row("z", "PASS", tier="focused", sweep=sweep, fingerprint=fingerprint)


class TestEarningAFullRun:
    """You test the wing before you build the plane."""

    def test_the_first_sweep_against_a_target_is_free(self) -> None:
        assert _gate([], "a", "b").allowed

    def test_history_on_another_target_does_not_count(self) -> None:
        rows = [_row("a", "FAIL", tier="focused", sweep="s1", target="in_memory")]
        assert _gate(rows, "a", "b").allowed

    def test_short_failing_tests_do_not_earn_a_full_run(self) -> None:
        """A few short tests, then the whole pack to "see what's working": refused."""
        rows = [
            _row("a", "FAIL", tier="focused", sweep="s1"),
            _row("b", "FAIL", tier="focused", sweep="s2"),
        ]
        decision = _gate(rows, "a", "b")
        assert not decision.allowed
        assert decision.unearned
        assert decision.missing == []

    def test_a_focused_pass_on_this_artifact_earns_it(self) -> None:
        rows = [_row("a", "FAIL", tier="focused", sweep="s1"), _earn("s2")]
        decision = _gate(rows, "a", "b")
        assert decision.allowed
        assert decision.earned_by == "s2"

    def test_a_probe_pass_does_not_earn_it(self) -> None:
        rows = [_row("a", "PASS", tier="probe", sweep="p1")]
        assert _gate(rows, "a", "b").unearned

    def test_a_clean_regression_must_be_earned_again(self) -> None:
        rows = [
            _earn("e0"),
            _row("a", "PASS", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="regression", sweep="r1"),
        ]
        assert _gate(rows, "a", "b").unearned

    def test_re_running_unchanged_code_is_not_earned(self) -> None:
        """Flakiness is a focused question: run the flaky scenario with more runs."""
        rows = [_earn("e0"), _row("a", "PASS", tier="regression", sweep="r1")]
        assert _gate(rows, "a").unearned

    def test_a_pass_on_an_older_artifact_is_stale_and_named(self) -> None:
        rows = [_earn("e1", fingerprint="fp-old")]
        decision = _gate(rows, "a", "b", fingerprint="fp-new")
        assert decision.unearned
        assert decision.stale_earner is not None
        assert decision.stale_earner["sweep_id"] == "e1"

    def test_a_pass_cut_short_by_its_budget_does_not_earn_it(self) -> None:
        rows = [_row("z", "PASS", tier="focused", sweep="e1", budget_exhausted=True)]
        assert _gate(rows, "a").unearned


class TestRegressionGate:
    """The per-scenario failure rule, on top of an earned run."""

    def test_a_failure_without_focused_evidence_is_refused(self) -> None:
        rows = [
            _row("a", "PASS", tier="regression", sweep="r1"),
            _row("b", "FAIL", tier="regression", sweep="r1"),
        ]
        decision = _gate(rows, "a", "b")
        assert not decision.allowed
        assert decision.missing == ["b"]
        assert decision.gated_sweeps == ["r1"]

    def test_invalid_counts_as_not_passed(self) -> None:
        rows = [_row("b", "INVALID", tier="regression", sweep="r1")]
        assert _gate(rows, "b").missing == ["b"]

    def test_the_sweeps_own_failure_judgement_wins_over_the_verdict(self) -> None:
        """A transport-only FAIL does not fail the sweep, so it does not gate either."""
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1", counts_as_failure=False),
            _earn(),
        ]
        assert _gate(rows, "b").allowed

    def test_a_focused_pass_on_this_artifact_satisfies_it(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="focused", sweep="f1", fingerprint="fp-2"),
        ]
        decision = _gate(rows, "a", "b", fingerprint="fp-2")
        assert decision.allowed
        assert decision.satisfied == ["b"]

    def test_a_focused_pass_on_an_older_artifact_is_stale(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="focused", sweep="f1", fingerprint="fp-2"),
        ]
        assert _gate(rows, "b", fingerprint="fp-3").missing == ["b"]

    def test_a_focused_pass_before_the_failing_regression_does_not_count(self) -> None:
        rows = [
            _row("b", "PASS", tier="focused", sweep="f0"),
            _row("b", "FAIL", tier="regression", sweep="r1"),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_a_probe_pass_is_not_evidence(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="probe", sweep="p1"),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_a_failing_focused_run_is_not_evidence(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "FAIL", tier="focused", sweep="f1"),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_a_focused_pass_cut_short_by_its_budget_is_not_evidence(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="focused", sweep="f1", budget_exhausted=True),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_a_focused_pass_on_another_pack_with_the_same_name_is_not_evidence(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="focused", sweep="f1", pack="other"),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_leaving_a_failure_out_of_the_selection_is_recorded_not_refused(self) -> None:
        rows = [_row("b", "FAIL", tier="regression", sweep="r1"), _earn()]
        decision = _gate(rows, "a", "c")
        assert decision.allowed
        assert decision.excluded_failing == ["b"]

    def test_a_failure_left_out_of_a_later_regression_stays_gated(self) -> None:
        """Excluding a failure once must not launder it for the regression after."""
        rows = [
            _row("a", "PASS", tier="regression", sweep="r1"),
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("a", "PASS", tier="regression", sweep="r2"),  # b left out
        ]
        assert _gate(rows, "a", "b").missing == ["b"]

    def test_a_later_passing_regression_row_clears_an_earlier_failure(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS", tier="regression", sweep="r2"),
            _earn(),
        ]
        assert _gate(rows, "b").allowed

    def test_order_comes_from_the_ledger_not_the_timestamp(self) -> None:
        """Two sweeps in the same second must not be confused (rows share a ts here)."""
        rows = [
            _row("b", "PASS", tier="regression", sweep="r1"),
            _row("b", "FAIL", tier="regression", sweep="r2"),
        ]
        assert _gate(rows, "b").missing == ["b"]

    def test_failures_on_another_runtime_target_do_not_gate(self) -> None:
        rows = [_row("b", "FAIL", tier="regression", sweep="r1", target="in_memory")]
        assert _gate(rows, "b", target="http://bench:8647").allowed

    def test_pass_with_variance_counts_as_passing(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("b", "PASS_WITH_VARIANCE", tier="focused", sweep="f1"),
        ]
        assert _gate(rows, "b").allowed

    def test_legacy_rows_without_an_experiment_never_gate(self) -> None:
        rows = [{"scenario_id": "b", "verdict": "FAIL", "ts": "2026-01-01T00:00:00Z"}]
        assert _gate(rows, "b").allowed


# ─── Artifact fingerprint ─────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "agent.py").write_text("PROMPT = 'v1'\n")
    (root / ".gitignore").write_text("*.log\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class TestWorktreeFingerprint:
    def test_outside_a_repository_there_is_no_tree(self, tmp_path: Path) -> None:
        assert git_worktree_tree(tmp_path) is None

    def test_a_tracked_edit_changes_the_tree(self, repo: Path) -> None:
        before = git_worktree_tree(repo)
        (repo / "agent.py").write_text("PROMPT = 'v2'\n")
        assert git_worktree_tree(repo) not in (None, before)

    def test_a_new_untracked_file_changes_the_tree(self, repo: Path) -> None:
        before = git_worktree_tree(repo)
        (repo / "scorer.py").write_text("x = 1\n")
        assert git_worktree_tree(repo) != before

    def test_ignored_files_do_not_change_the_tree(self, repo: Path) -> None:
        before = git_worktree_tree(repo)
        (repo / "debug.log").write_text("noise\n")
        assert git_worktree_tree(repo) == before

    def test_the_runs_directory_is_excluded(self, repo: Path) -> None:
        runs = repo / "runs"
        runs.mkdir()
        (runs / "ledger.ndjsonl").write_text("{}\n")
        before = git_worktree_tree(repo, exclude=[runs])
        (runs / "ledger.ndjsonl").write_text("{}\n{}\n")
        assert git_worktree_tree(repo, exclude=[runs]) == before

    def test_a_gitignored_runs_directory_keeps_the_snapshot_working(self, repo: Path) -> None:
        """Excluding an ignored path via pathspec makes `git add` fail outright."""
        (repo / ".gitignore").write_text("*.log\nruns/\n")
        runs = repo / "runs"
        runs.mkdir()
        (runs / "ledger.ndjsonl").write_text("{}\n")
        before = git_worktree_tree(repo, exclude=[runs])
        assert before is not None
        (repo / "agent.py").write_text("PROMPT = 'v3'\n")
        assert git_worktree_tree(repo, exclude=[runs]) not in (None, before)

    def test_a_report_file_written_into_the_repo_can_be_excluded(self, repo: Path) -> None:
        report = repo / "results.xml"
        report.write_text("<a/>")
        before = git_worktree_tree(repo, exclude=[report])
        report.write_text("<b/>")
        assert git_worktree_tree(repo, exclude=[report]) == before

    def test_no_objects_are_written_into_the_repository(self, repo: Path) -> None:
        (repo / "weights.bin").write_bytes(os.urandom(4096))
        objects = repo / ".git" / "objects"
        before = sorted(path for path in objects.rglob("*") if path.is_file())
        assert git_worktree_tree(repo) is not None
        assert sorted(path for path in objects.rglob("*") if path.is_file()) == before

    def test_the_real_index_is_left_untouched(self, repo: Path) -> None:
        (repo / "agent.py").write_text("PROMPT = 'v2'\n")
        (repo / "new.py").write_text("y = 2\n")
        status = _git(repo, "status", "--porcelain")
        git_worktree_tree(repo)
        assert _git(repo, "status", "--porcelain") == status


# ─── Probe replay and budgets in the runner ───────────────────────────────────


class _RecordingHandle:
    def __init__(self, *, full_history: bool = True) -> None:
        self.sent: list[list[dict[str, Any]]] = []
        self._windtunnel_consumes_full_history = full_history

    def send(self, messages, session_id):  # noqa: ANN001, ANN201
        self.sent.append([dict(message) for message in messages])
        return {"role": "assistant", "content": "ok", "tool_calls": []}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _Runtime:
    def __init__(self, handle: _RecordingHandle) -> None:
        self.handle = handle

    def provision(self, config, mcps=None):  # noqa: ANN001, ANN201
        return self.handle


def _recorded_trace():
    from windtunnel.api.trace import Trace, Turn

    def turn(role: str, content: str) -> Turn:
        return Turn(role=role, content=content, tool_calls=[], tool_results=[], latency_ms=0.0)

    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Trace(
        scenario_id="drift",
        agent_id="a",
        variant_id="v",
        model="m",
        quant="q",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=[
            turn("user", "my client is Bluewing"),
            turn("assistant", "noted"),
            turn("user", "what is their email?"),
            turn("assistant", "I don't know"),
        ],
        tool_schema_hash="sha256:x",
        worker_warnings=[],
        run_id="source-run",
    )


class TestProbeReplay:
    def test_split_defaults_to_the_scored_user_turn(self) -> None:
        from windtunnel.api.replay import split_at_user_turn

        prefix, live = split_at_user_turn(_recorded_trace(), None)
        assert [turn.content for turn in prefix.turns] == ["my client is Bluewing", "noted"]
        assert live == ["what is their email?"]

    def test_split_from_the_first_turn_replays_everything_live(self) -> None:
        from windtunnel.api.replay import split_at_user_turn

        prefix, live = split_at_user_turn(_recorded_trace(), 1)
        assert prefix.turns == ()
        assert live == ["my client is Bluewing", "what is their email?"]

    def test_split_rejects_a_turn_that_is_not_there(self) -> None:
        from windtunnel.api.replay import split_at_user_turn

        with pytest.raises(ValueError, match="out of range"):
            split_at_user_turn(_recorded_trace(), 3)

    def test_the_model_runs_on_the_recorded_history(self) -> None:
        from windtunnel.api.replay import split_at_user_turn
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        prefix, live = split_at_user_turn(_recorded_trace(), None)
        handle = _RecordingHandle()
        result = run_scenario(
            Scenario(name="drift", prompt=live[-1], user_turns=live, target_facts=[["ok"]]),
            _Runtime(handle),
            history_prefix=prefix,
        )
        assert handle.sent == [[
            {"role": "user", "content": "my client is Bluewing"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "what is their email?"},
        ]]
        trace = result.runs[0].trace
        assert [turn.content for turn in trace.turns] == [
            "my client is Bluewing", "noted", "what is their email?", "ok",
        ]
        assert "replay_prefix: source=source-run turns=2" in trace.worker_warnings
        assert result.aggregate.verdict == "PASS"

    def test_a_runtime_that_drops_history_refuses_the_probe(self) -> None:
        """A probe scored on history the model never saw would be a false signal."""
        from windtunnel.api.replay import split_at_user_turn
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        prefix, live = split_at_user_turn(_recorded_trace(), None)
        handle = _RecordingHandle(full_history=False)
        result = run_scenario(
            Scenario(name="drift", prompt=live[-1], target_facts=[["ok"]]),
            _Runtime(handle),
            history_prefix=prefix,
        )
        assert handle.sent == []
        assert result.aggregate.verdict != "PASS"
        warnings = result.runs[0].trace.worker_warnings
        assert any("cannot deliver a replayed history prefix" in w for w in warnings)

    def test_the_frozen_history_neither_satisfies_nor_fails_the_probe(self) -> None:
        """A tool call made by the ORIGINAL run must not count as the probe's own."""
        from windtunnel.api.replay import split_at_user_turn
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        source = _recorded_trace()
        source.turns[1].tool_calls = [
            {"id": "c1", "type": "function",
             "function": {"name": "client_lookup", "arguments": "{}"}},
        ]
        prefix, live = split_at_user_turn(source, None)
        result = run_scenario(
            Scenario(
                name="drift",
                prompt=live[-1],
                user_turns=live,
                target_facts=[["ok"]],
                requires_tool_use=True,
                must_call=["client_lookup"],
            ),
            _Runtime(_RecordingHandle()),
            history_prefix=prefix,
        )
        assert result.aggregate.verdict == "FAIL"
        trace = result.runs[0].trace
        assert trace.turns[1].tool_calls  # the saved record still shows the history

    def test_offline_rescoring_also_ignores_the_frozen_history(self) -> None:
        from windtunnel.api.replay import prefix_length, scoring_view

        trace = _recorded_trace()
        trace.worker_warnings.append("replay_prefix: source=x turns=2")
        assert prefix_length(trace) == 2
        assert [turn.content for turn in scoring_view(trace).turns] == [
            "what is their email?", "I don't know",
        ]

    def test_should_start_run_stops_the_remaining_runs(self) -> None:
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        handle = _RecordingHandle()
        asked: list[int] = []

        def allow(index: int) -> bool:
            asked.append(index)
            return index < 2

        result = run_scenario(
            Scenario(name="s", prompt="hi", target_facts=[["ok"]]),
            _Runtime(handle),
            runs_per_scenario=5,
            should_start_run=allow,
        )
        assert asked == [1, 2]
        assert result.aggregate.total == 2


# ─── Tier-ordered queueing ────────────────────────────────────────────────────


class TestQueueOrder:
    def test_a_probe_waiting_behind_a_regression_is_served_first(self) -> None:
        from windtunnel._cli.runlock import holder_record, lock_path, runtime_lock

        order: list[str] = []
        release = threading.Event()
        holding = threading.Event()

        def hold() -> None:
            with runtime_lock("shared", wait=True, holder=holder_record(), poll_s=0.02):
                holding.set()
                release.wait(timeout=10)

        def waiter(name: str, priority: int) -> None:
            with runtime_lock(
                "shared", wait=True, holder=holder_record(), poll_s=0.02, priority=priority
            ):
                order.append(name)

        queue = lock_path("shared").with_name(lock_path("shared").name + ".queue")

        def queued() -> int:
            return len(list(queue.glob("*.ticket"))) if queue.exists() else 0

        holder = threading.Thread(target=hold)
        holder.start()
        assert holding.wait(timeout=10)
        regression = threading.Thread(target=waiter, args=("regression", 2))
        regression.start()
        _until(lambda: queued() == 1)
        probe = threading.Thread(target=waiter, args=("probe", 0))
        probe.start()
        _until(lambda: queued() == 2)
        release.set()
        for thread in (holder, regression, probe):
            thread.join(timeout=10)
        assert order == ["probe", "regression"]

    def test_a_dead_waiters_ticket_is_reaped(self, tmp_path: Path) -> None:
        from windtunnel._cli.runlock import _head_ticket

        queue = tmp_path / "q"
        queue.mkdir()
        (queue / f"0-{time.time_ns():020d}-999999.ticket").write_text("")
        assert _head_ticket(queue) is None
        assert list(queue.glob("*.ticket")) == []

    def test_no_wait_does_not_jump_a_live_queue(self) -> None:
        from windtunnel._cli.runlock import (
            RuntimeBusy,
            _ticket,
            holder_record,
            lock_path,
            runtime_lock,
        )

        queue = lock_path("q2").with_name(lock_path("q2").name + ".queue")
        with _ticket(queue, 0):
            with pytest.raises(RuntimeBusy):
                with runtime_lock("q2", wait=False, holder=holder_record()):
                    pass


def _until(predicate, timeout: float = 10.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


# ─── wt run end to end ────────────────────────────────────────────────────────


class TestWtRunLadder:
    @pytest.fixture
    def bench(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
        import windtunnel.cli as cli

        monkeypatch.chdir(tmp_path)
        scenarios = {name: _scenario(name) for name in ("alpha", "beta", "gamma")}
        state: dict[str, Any] = {
            "fp": "sha256:one",
            "pass": {"alpha": True, "beta": False, "gamma": True},
        }
        _patch_cli_run(monkeypatch, [_pack("p", list(scenarios.values()))], {})
        import windtunnel.api.runner as runner

        monkeypatch.setattr(
            runner,
            "run_scenario",
            lambda scenario, runtime, *a, **k: _result(
                scenarios[scenario.name], passed=state["pass"][scenario.name]
            ),
        )
        monkeypatch.setattr(
            cli,
            "compute_fingerprint",
            lambda **kwargs: Fingerprint(
                value=state["fp"],
                parts={"git_tree": state["fp"], "target": kwargs["target"]},
            ),
        )
        runs_dir = tmp_path / "runs"

        def run(*extra: str) -> int:
            return cli.main(["run", "--runs-dir", str(runs_dir), "--label", "l", *extra])

        return run, state, runs_dir

    @staticmethod
    def _ledger(runs_dir: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (runs_dir / "ledger.ndjsonl").read_text().splitlines()
            if line
        ]

    def test_the_first_sweep_needs_no_declaration(self, bench) -> None:  # noqa: ANN001
        run, _state, runs_dir = bench
        assert run() == 1
        rows = self._ledger(runs_dir)
        assert {row["experiment"]["tier"] for row in rows} == {"regression"}
        assert len({row["sweep_id"] for row in rows}) == 1

    def test_later_sweeps_must_declare_a_question_and_expectation(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, _state, _runs_dir = bench
        run()
        capsys.readouterr()
        assert run("--scenario", "alpha") == 2
        assert "--question" in capsys.readouterr().err
        assert run("--scenario", "alpha", "--question", "q") == 2

    def test_a_regression_after_failures_is_refused_until_focused_passes(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, runs_dir = bench
        declare = ("--question", "does the fix hold?", "--expect", "pass")
        assert run() == 1  # beta fails in the baseline regression
        capsys.readouterr()

        assert run(*declare) == 2
        err = capsys.readouterr().err
        assert "refusing a regression sweep" in err
        assert "wt run --scenario beta --runtime in_memory --runs-dir " in err

        state["pass"]["beta"] = True
        assert run("--scenario", "beta", *declare) == 0
        assert "prediction held" in capsys.readouterr().err
        assert run(*declare) == 0

        final = self._ledger(runs_dir)[-3:]
        assert all(row["experiment"]["tier"] == "regression" for row in final)
        assert all(row["experiment"]["question"] == "does the fix hold?" for row in final)
        assert final[0]["experiment"]["evidence_for"]

    def test_short_tests_then_the_whole_pack_is_refused(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        """The overnight failure: a few short tests, then 1.5h "to see what works"."""
        run, _state, runs_dir = bench
        declare = ("--question", "what is working?", "--expect", "pass")
        assert run("--scenario", "beta") == 1  # first sweep: a failing short test
        assert run("--scenario", "beta", *declare) == 1
        capsys.readouterr()
        assert run(*declare) == 2
        err = capsys.readouterr().err
        assert "nothing has earned it" in err
        assert f"wt results --runs {runs_dir}" in err
        assert "--runtime in_memory" in err

    def test_a_passing_short_test_earns_the_whole_pack(self, bench) -> None:  # noqa: ANN001
        run, _state, runs_dir = bench
        declare = ("--question", "does my alpha fix hold everywhere?", "--expect", "pass")
        assert run("--scenario", "alpha") == 0
        assert run(*declare) == 1  # beta fails, but the run itself was earned
        rows = self._ledger(runs_dir)
        assert rows[-1]["experiment"]["earned_by"] == rows[0]["sweep_id"]

    def test_a_change_after_the_focused_pass_makes_it_stale(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, _runs_dir = bench
        declare = ("--question", "q", "--expect", "pass")
        run()
        state["pass"]["beta"] = True
        assert run("--scenario", "beta", *declare) == 0
        state["fp"] = "sha256:two"  # an edit after the focused pass
        capsys.readouterr()
        assert run(*declare) == 2
        assert "beta" in capsys.readouterr().err

    def test_an_edit_during_a_focused_sweep_voids_it_as_evidence(
        self, bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        import windtunnel.api.runner as runner

        run, state, runs_dir = bench
        declare = ("--question", "q", "--expect", "pass")
        run()
        state["pass"]["beta"] = True
        real = runner.run_scenario

        def edit_while_running(scenario, runtime, *args, **kwargs):  # noqa: ANN001, ANN202
            state["fp"] = "sha256:edited-mid-run"
            return real(scenario, runtime, *args, **kwargs)

        monkeypatch.setattr(runner, "run_scenario", edit_while_running)
        assert run("--scenario", "beta", *declare) == 0
        assert "is not evidence" in capsys.readouterr().err
        assert self._ledger(runs_dir)[-1]["experiment"]["fingerprint"] == "changed-during-sweep"
        assert run(*declare) == 2

    def test_dropping_the_failure_from_the_selection_is_recorded(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, _state, runs_dir = bench
        run()
        assert run("--scenario", "alpha", "--question", "q", "--expect", "pass") == 0
        capsys.readouterr()
        rc = run("--scenario", "alpha", "--scenario", "gamma", "--question", "q",
                 "--expect", "pass")
        assert rc == 0
        assert "not testing scenario(s) whose most recent regression run failed" in (
            capsys.readouterr().err
        )
        rows = self._ledger(runs_dir)[-2:]
        assert {row["experiment"]["tier"] for row in rows} == {"regression"}
        assert all(row["experiment"]["excluded_failing"] == ["beta"] for row in rows)

    def test_a_budget_above_the_tier_cap_is_refused(
        self, bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, _state, _runs_dir = bench
        assert run("--scenario", "alpha", "--budget", "5000") == 2
        assert "exceeds the focused tier cap" in capsys.readouterr().err

    def test_a_spent_budget_starts_no_further_scenarios(
        self, bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        import windtunnel.cli as cli

        run, _state, runs_dir = bench
        readings = iter([0.0, 1.0])  # start, then the check before alpha

        class _FakeClockBudget(Budget):
            def __init__(self, seconds: float | None) -> None:
                super().__init__(seconds, clock=lambda: next(readings, 100.0))

        monkeypatch.setattr(cli, "Budget", _FakeClockBudget)
        assert run("--budget", "10") == 1
        err = capsys.readouterr().err
        assert "budget of 10s spent" in err
        assert "not started: beta, gamma" in err
        rows = self._ledger(runs_dir)
        assert [row["scenario_id"] for row in rows] == ["alpha"]
        assert rows[0]["experiment"]["budget_s"] == 10.0


class TestWtRunProbe:
    @pytest.fixture
    def probe_bench(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
        import windtunnel.api.runner as runner
        import windtunnel.cli as cli
        from windtunnel.api.trace import save_trace

        monkeypatch.chdir(tmp_path)
        drift = _scenario("drift")
        other = _scenario("other")
        _patch_cli_run(monkeypatch, [_pack("p", [drift, other])], {})
        calls: list[dict[str, Any]] = []

        def fake_run_scenario(scenario, runtime, *args, **kwargs):  # noqa: ANN001, ANN202
            calls.append({"scenario": scenario, **kwargs})
            return _result(scenario, passed=True)

        monkeypatch.setattr(runner, "run_scenario", fake_run_scenario)
        monkeypatch.setattr(
            cli,
            "compute_fingerprint",
            lambda **_kwargs: Fingerprint(value="sha256:x", parts={}),
        )
        trace_path = tmp_path / "source.json"
        save_trace(_recorded_trace(), trace_path)

        def run(*extra: str) -> int:
            return cli.main(["run", "--runs-dir", str(tmp_path / "runs"), *extra])

        return run, calls, trace_path, tmp_path / "runs"

    def test_a_probe_runs_the_traces_scenario_on_its_recorded_history(
        self, probe_bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, calls, trace_path, runs_dir = probe_bench
        assert run("--from-trace", str(trace_path)) == 0
        assert "probe sweep, budget 300s" in capsys.readouterr().err
        (call,) = calls
        assert call["scenario"].name == "drift"
        assert call["scenario"].user_turns == ["what is their email?"]
        assert [turn.content for turn in call["history_prefix"].turns] == [
            "my client is Bluewing", "noted",
        ]
        (row,) = [
            json.loads(line) for line in (runs_dir / "ledger.ndjsonl").read_text().splitlines()
        ]
        assert row["experiment"]["tier"] == "probe"
        assert row["experiment"]["source_trace"] == str(trace_path)

    def test_from_turn_needs_a_trace(self, probe_bench) -> None:  # noqa: ANN001
        run, calls, _trace_path, _runs_dir = probe_bench
        assert run("--from-turn", "1", "--scenario", "drift") == 2
        assert calls == []

    def test_a_probe_refuses_a_different_selection(
        self, probe_bench, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, calls, trace_path, _runs_dir = probe_bench
        assert run("--from-trace", str(trace_path), "--scenario", "other") == 2
        assert "A probe runs exactly the trace's scenario" in capsys.readouterr().err
        assert calls == []


class TestExplainingStaleness:
    def test_compiled_python_is_never_part_of_the_artifact(self, repo: Path) -> None:
        before = git_worktree_tree(repo)
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "agent.cpython-311.pyc").write_bytes(b"\x00\x01")
        assert git_worktree_tree(repo) == before

    def test_outputs_recorded_by_any_command_stay_excluded(
        self, repo: Path, tmp_path: Path
    ) -> None:
        from windtunnel._cli.ladder import compute_fingerprint, record_output, recorded_outputs

        runs = tmp_path / "runs"
        report = repo / "report.html"
        report.write_text("v1")
        record_output(runs, report)
        record_output(runs, report)  # idempotent
        assert recorded_outputs(runs) == [report.resolve()]

        def fingerprint() -> str:
            return compute_fingerprint(
                runtime_name="rt", soul_path=None, agents_path=None,
                exclude=[runs, *recorded_outputs(runs)], cwd=repo,
            ).value

        before = fingerprint()
        report.write_text("v2")
        assert fingerprint() == before

    def test_describe_changes_names_the_files(self, repo: Path) -> None:
        from windtunnel._cli.ladder import compute_fingerprint, describe_changes

        def fingerprint():  # noqa: ANN202
            return compute_fingerprint(
                runtime_name="rt", soul_path=None, agents_path=None, cwd=repo
            )

        old = fingerprint()
        (repo / "agent.py").write_text("PROMPT = 'v9'\n")
        (repo / "notes.txt").write_text("noise\n")
        text = describe_changes(dict(old.parts), old.manifest, fingerprint())
        assert "agent.py" in text and "notes.txt" in text

    def test_the_refusal_names_what_made_a_focused_pass_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        from windtunnel._cli.ladder import (
            compute_fingerprint,
            gate_refusal_message,
            save_manifest,
        )

        runs = tmp_path / "runs"

        def fingerprint():  # noqa: ANN202
            return compute_fingerprint(
                runtime_name="rt", soul_path=None, agents_path=None, cwd=repo, target="rt"
            )

        passed_on = fingerprint()
        save_manifest(runs, passed_on)
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            {
                **_row("b", "PASS", tier="focused", sweep="f1", fingerprint=passed_on.value),
                "experiment": {
                    "tier": "focused",
                    "fingerprint": passed_on.value,
                    "fingerprint_parts": dict(passed_on.parts),
                },
            },
        ]
        (repo / "agent-debug.txt").write_text("the runtime wrote this\n")
        now = fingerprint()
        decision = _gate(rows, "b", fingerprint=now.value)
        assert decision.missing == ["b"]
        message = gate_refusal_message(decision, now, runs_dir=runs)
        assert "b (its focused pass is stale; since then files: agent-debug.txt)" in message
        assert "gitignore it" in message

    def test_only_failures_from_the_selected_packs_are_reported_as_excluded(self) -> None:
        rows = [
            _row("b", "FAIL", tier="regression", sweep="r1"),
            _row("z", "FAIL", tier="regression", sweep="r1", pack="unrelated"),
        ]
        decision = _gate(rows, "a")
        assert decision.excluded_failing == ["b"]


class TestTargets:
    def test_two_spellings_of_one_plugin_are_one_target(self) -> None:
        from windtunnel._cli.ladder import normalize_target

        class Plugin:
            pass

        assert normalize_target(Plugin, "entry_name", "entry_name") == normalize_target(
            Plugin(), "pkg.mod:Plugin", "pkg.mod:Plugin"
        )

    def test_loopback_and_trailing_slash_spellings_agree(self) -> None:
        from windtunnel._cli.ladder import normalize_target

        class Inject:
            pass

        a = normalize_target(Inject(), "http://localhost:8647/", "http_inject")
        b = normalize_target(Inject(), "HTTP://127.0.0.1:8647", "http_inject")
        assert a == b
        assert a != normalize_target(Inject(), "http://127.0.0.1:9000", "http_inject")
