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


# ─── Cost, value, and the model under test ────────────────────────────────────


class _UsageHandle(_RecordingHandle):
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        super().__init__()
        self._replies = iter(replies)

    def send(self, messages, session_id):  # noqa: ANN001, ANN201
        super().send(messages, session_id)
        return {"role": "assistant", "content": "ok", "tool_calls": [], **next(self._replies)}


def _two_turns(handle: _RecordingHandle, config: Any = None):  # noqa: ANN202
    from windtunnel.api.runner import run_scenario
    from windtunnel.api.scenario import Scenario

    scenario = Scenario(name="s", prompt="b", user_turns=["a", "b"], target_facts=[["ok"]])
    kwargs = {"config": config} if config is not None else {}
    return run_scenario(scenario, _Runtime(handle), **kwargs).runs[0].trace


class TestCost:
    def test_tokens_are_summed_across_sends_in_either_spelling(self) -> None:
        trace = _two_turns(_UsageHandle([
            {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            {"usage": {"input_tokens": 30, "output_tokens": 4}},
        ]))
        assert trace.usage == {"input_tokens": 40, "output_tokens": 6}

    def test_a_send_without_usage_makes_the_run_unreported_not_undercounted(self) -> None:
        trace = _two_turns(_UsageHandle([
            {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}, {},
        ]))
        assert trace.usage is None

    def test_sum_tokens_is_none_once_any_part_did_not_report(self) -> None:
        from windtunnel._cli.ladder import sum_tokens

        both = {"input_tokens": 1, "cached_tokens": 0, "output_tokens": 2}
        assert sum_tokens([both, both]) == {
            "input_tokens": 2, "cached_tokens": 0, "output_tokens": 4,
        }
        assert sum_tokens([both, None]) == {
            "input_tokens": None, "cached_tokens": None, "output_tokens": None,
        }

    def test_usage_survives_a_save_and_load(self, tmp_path: Path) -> None:
        from windtunnel.api.trace import load_trace, save_trace

        trace = _two_turns(_UsageHandle([
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ] * 2))
        save_trace(trace, tmp_path / "t.json")
        assert load_trace(tmp_path / "t.json").usage == {"input_tokens": 2, "output_tokens": 2}

    def test_model_calls_carries_one_normalized_entry_per_send(self) -> None:
        trace = _two_turns(_UsageHandle([
            {"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
            {"usage": {"input_tokens": 205, "cached_tokens": 100, "output_tokens": 5}},
        ]))
        assert trace.model_calls is not None
        assert len(trace.model_calls) == 2
        conversation = trace.model_calls[0]["conversation"]
        assert trace.model_calls[0] == {
            "conversation": conversation, "prompt_tokens": 100,
            "cached_tokens": None, "completion_tokens": 10,
        }
        assert trace.model_calls[1] == {
            "conversation": conversation, "prompt_tokens": 205,
            "cached_tokens": 100, "completion_tokens": 5,
        }

    def test_model_calls_is_none_when_the_runtime_reports_no_usage(self) -> None:
        trace = _two_turns(_UsageHandle([{}, {}]))
        assert trace.model_calls is None

    def test_model_calls_accepts_cache_read_input_tokens_and_details_spelling(self) -> None:
        trace = _two_turns(_UsageHandle([
            {"usage": {"prompt_tokens": 50, "completion_tokens": 5,
                       "cache_read_input_tokens": 20}},
            {"usage": {"prompt_tokens": 60, "completion_tokens": 5,
                       "prompt_tokens_details": {"cached_tokens": 30}}},
        ]))
        calls = trace.model_calls
        assert calls[0]["cached_tokens"] == 20
        assert calls[1]["cached_tokens"] == 30

    def test_model_calls_sums_separate_input_and_cache_read_spelling(self) -> None:
        from windtunnel.api.runner import _normalize_call_usage

        # Some wire shapes report "input" as uncached-only, with the cache
        # read kept separate ("cacheRead") rather than nested in the total.
        normalized = _normalize_call_usage({"input": 80, "cacheRead": 20, "output": 5})
        assert normalized == {"prompt_tokens": 100, "cached_tokens": 20, "completion_tokens": 5}


class TestModel:
    def test_the_model_the_responses_name_is_recorded(self) -> None:
        trace = _two_turns(_UsageHandle([{"model": "small-1"}] * 2))
        assert trace.model == "small-1"

    def test_a_configured_model_wins_over_what_responses_say(self) -> None:
        from windtunnel.spi.agent_runtime import AgentConfig, ModelSpec

        trace = _two_turns(
            _UsageHandle([{"model": "other"}] * 2), AgentConfig(model=ModelSpec(name="target"))
        )
        assert trace.model == "target"

    def test_the_model_label_comes_only_from_the_plugin_hook(self) -> None:
        from windtunnel._cli.ladder import model_label

        class Labelled:
            def model_label(self, runtime_name: str) -> str:
                return "big"

        class Broken:
            def model_label(self, runtime_name: str) -> str:
                raise RuntimeError("no config")

        assert model_label(Labelled(), "rt") == "big"
        assert model_label(Broken(), "rt") is None
        assert model_label(object(), "rt") is None

    def test_the_model_is_part_of_the_fingerprint_and_named_when_it_changes(
        self, tmp_path: Path
    ) -> None:
        from windtunnel._cli.ladder import compute_fingerprint, describe_changes

        def fingerprint(model: str) -> Fingerprint:
            return compute_fingerprint(
                runtime_name="rt", soul_path=None, agents_path=None, cwd=tmp_path, model=model
            )

        small, big = fingerprint("small"), fingerprint("big")
        assert small.value != big.value
        assert describe_changes(dict(small.parts), None, big) == "changed: model"

    def test_the_policy_maps_tiers_to_allowed_labels(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import check_model_policy, load_model_policy

        (tmp_path / "pyproject.toml").write_text(
            '[tool.windtunnel.ladder.models]\nprobe = ["small", "big"]\nregression = ["big"]\n'
        )
        policy = load_model_policy(tmp_path)
        check_model_policy("probe", "small", policy)
        check_model_policy("focused", None, policy)  # unlisted tier: any model
        with pytest.raises(LadderError, match="must use one of: big"):
            check_model_policy("regression", "small", policy)
        with pytest.raises(LadderError, match="does not report which model"):
            check_model_policy("regression", None, policy)

    def test_an_invalid_policy_is_refused_not_ignored(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_model_policy

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.models]\nsmoke = [\"x\"]\n"
        )
        with pytest.raises(LadderError, match="not a tier"):
            load_model_policy(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.models]\nprobe = []\n"
        )
        with pytest.raises(LadderError, match="non-empty list"):
            load_model_policy(tmp_path)


class TestPricing:
    """[tool.windtunnel.ladder.pricing]: $/hour and $/M tokens, and the $ they produce."""

    def test_no_pricing_table_gives_none(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_pricing

        assert load_pricing(tmp_path) is None

    def test_pricing_reads_rates_and_keeps_a_default_fallback(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_pricing

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing]\n"
            "time_per_hour = 75.0\n"
            "[tool.windtunnel.ladder.pricing.models]\n"
            '"big-model" = { input_per_m = 1.0, output_per_m = 2.0 }\n'
            "default = { input_per_m = 0.5, output_per_m = 1.5 }\n"
        )
        assert load_pricing(tmp_path) == {
            "time_per_hour": 75.0,
            "models": {
                "big-model": {"input_per_m": 1.0, "output_per_m": 2.0},
                "default": {"input_per_m": 0.5, "output_per_m": 1.5},
            },
        }

    def test_invalid_pricing_is_refused_not_ignored(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import LadderError, load_pricing

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing]\ntime_per_hour = -1.0\n"
        )
        with pytest.raises(LadderError, match="time_per_hour"):
            load_pricing(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing]\n"
            "[tool.windtunnel.ladder.pricing.models]\n"
            '"m" = { input_per_m = -0.1, output_per_m = 1.0 }\n'
        )
        with pytest.raises(LadderError, match="input_per_m"):
            load_pricing(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing]\nmodels = 1\n"
        )
        with pytest.raises(LadderError, match="models must be a table"):
            load_pricing(tmp_path)

    def test_pricing_accepts_an_optional_cache_read_rate(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_pricing

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing.models]\n"
            '"big" = { input_per_m = 1.0, output_per_m = 2.0, cache_read_per_m = 0.1 }\n'
        )
        assert load_pricing(tmp_path)["models"]["big"] == {
            "input_per_m": 1.0, "output_per_m": 2.0, "cache_read_per_m": 0.1,
        }

    def test_pricing_accepts_an_optional_per_label_time_rate(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_pricing

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.ladder.pricing]\n"
            "time_per_hour = 60.0\n"
            "[tool.windtunnel.ladder.pricing.models]\n"
            '"cheap" = { input_per_m = 1.0, output_per_m = 2.0, time_per_hour = 12.0 }\n'
            '"default" = { input_per_m = 1.0, output_per_m = 2.0 }\n'
        )
        pricing = load_pricing(tmp_path)
        assert pricing["models"]["cheap"] == {
            "input_per_m": 1.0, "output_per_m": 2.0, "time_per_hour": 12.0,
        }
        assert "time_per_hour" not in pricing["models"]["default"]

    def test_compute_cost_usd_splits_uncached_cache_read_and_output(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {
            "time_per_hour": 3600.0,
            "models": {"big": {"input_per_m": 1.0, "output_per_m": 2.0, "cache_read_per_m": 0.1}},
        }
        cost = compute_cost_usd(
            wall_s=1.0, input_tokens=1_000_000, cached_tokens=400_000, output_tokens=500_000,
            model="big", pricing=pricing,
        )
        assert cost == {
            "uncached_input": 0.6, "cache_read": 0.04, "output": 1.0, "time": 1.0,
            "total": 2.64, "tokens_known": True, "cache_split_known": True,
        }

    def test_compute_cost_usd_falls_back_to_input_rate_without_a_cache_rate(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {
            "time_per_hour": 0.0,
            "models": {"big": {"input_per_m": 2.0, "output_per_m": 1.0}},
        }
        cost = compute_cost_usd(
            wall_s=0.0, input_tokens=1_000_000, cached_tokens=500_000, output_tokens=0,
            model="big", pricing=pricing,
        )
        # No cache_read_per_m configured: cache reads price at input_per_m too.
        assert cost["cache_read"] == 1.0 and cost["uncached_input"] == 1.0

    def test_compute_cost_usd_falls_back_to_the_default_model(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {"time_per_hour": 0.0, "models": {"default": {"input_per_m": 1.0, "output_per_m": 1.0}}}
        cost = compute_cost_usd(
            wall_s=0.0, input_tokens=1_000_000, cached_tokens=0, output_tokens=0,
            model="unlisted", pricing=pricing,
        )
        assert cost["uncached_input"] == 1.0 and cost["total"] == 1.0 and cost["tokens_known"]

    def test_compute_cost_usd_prices_all_input_at_the_uncached_rate_when_cache_split_unknown(
        self,
    ) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {
            "time_per_hour": 0.0,
            "models": {"big": {"input_per_m": 1.0, "output_per_m": 1.0, "cache_read_per_m": 0.1}},
        }
        cost = compute_cost_usd(
            wall_s=0.0, input_tokens=1_000_000, cached_tokens=None, output_tokens=0,
            model="big", pricing=pricing,
        )
        assert cost == {
            "uncached_input": 1.0, "cache_read": None, "output": 0.0, "time": 0.0,
            "total": 1.0, "tokens_known": True, "cache_split_known": False,
        }

    def test_compute_cost_usd_is_time_only_when_tokens_or_price_are_missing(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {"time_per_hour": 3600.0, "models": {}}
        expected = {
            "uncached_input": None, "cache_read": None, "output": None, "time": 2.0,
            "total": 2.0, "tokens_known": False, "cache_split_known": False,
        }
        # no usage reported at all
        assert compute_cost_usd(
            wall_s=2.0, input_tokens=None, cached_tokens=None, output_tokens=None,
            model="big", pricing=pricing,
        ) == expected
        # usage reported, but the model has no price and there is no default
        assert compute_cost_usd(
            wall_s=2.0, input_tokens=100, cached_tokens=0, output_tokens=50,
            model="big", pricing=pricing,
        ) == expected

    def test_compute_cost_usd_uses_a_labels_own_time_rate_over_the_global_one(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        pricing = {
            "time_per_hour": 60.0,
            "models": {
                "cheap": {"input_per_m": 0.0, "output_per_m": 0.0, "time_per_hour": 12.0},
                "default": {"input_per_m": 0.0, "output_per_m": 0.0},
            },
        }
        priced = compute_cost_usd(
            wall_s=3600.0, input_tokens=0, cached_tokens=0, output_tokens=0,
            model="cheap", pricing=pricing,
        )
        assert priced["time"] == 12.0 and priced["total"] == 12.0
        # a label with no time_per_hour of its own falls back to the global rate
        fallback = compute_cost_usd(
            wall_s=3600.0, input_tokens=0, cached_tokens=0, output_tokens=0,
            model="default", pricing=pricing,
        )
        assert fallback["time"] == 60.0 and fallback["total"] == 60.0

    def test_compute_cost_usd_is_none_without_pricing(self) -> None:
        from windtunnel._cli.ladder import compute_cost_usd

        assert compute_cost_usd(
            wall_s=1.0, input_tokens=1, cached_tokens=0, output_tokens=1, model="x", pricing=None
        ) is None

    def test_cost_block_shows_tokens_in_cached_out_wall_and_dollars(self) -> None:
        from windtunnel._cli.ladder import cost_block

        cost = {"wall_s": 12.3, "input_tokens": 100, "cached_tokens": 40, "output_tokens": 50}
        cost_usd = {
            "uncached_input": 0.006, "cache_read": 0.0004, "output": 0.01, "time": 0.02,
            "total": 0.0364, "tokens_known": True, "cache_split_known": True,
        }
        block = cost_block("does it help?", cost, cost_usd)
        assert "cost — 'does it help?'" in block
        assert "100 in (40 cached) / 50 out" in block
        assert "12.3s" in block
        assert (
            "$ uncached 0.0060 + cache read 0.0004 + output 0.0100 + time 0.0200 "
            "= total 0.0364" in block
        )

    def test_cost_block_flags_unknown_cache_split_priced_at_the_input_rate(self) -> None:
        from windtunnel._cli.ladder import cost_block

        cost = {"wall_s": 1.0, "input_tokens": 100, "cached_tokens": None, "output_tokens": 50}
        cost_usd = {
            "uncached_input": 0.01, "cache_read": None, "output": 0.005, "time": 0.01,
            "total": 0.025, "tokens_known": True, "cache_split_known": False,
        }
        block = cost_block(None, cost, cost_usd)
        assert "100 in (cache split unknown) / 50 out" in block
        assert (
            "$ uncached 0.0100 (cache split unknown) + output 0.0050 + time 0.0100 "
            "= total 0.0250" in block
        )

    def test_cost_block_flags_unknown_tokens_and_skips_dollars_unpriced(self) -> None:
        from windtunnel._cli.ladder import cost_block

        cost = {"wall_s": 1.0, "input_tokens": None, "cached_tokens": None, "output_tokens": None}
        unpriced = cost_block(None, cost, None)
        assert "tokens: unknown" in unpriced
        assert "$" not in unpriced
        cost_usd = {
            "uncached_input": None, "cache_read": None, "output": None, "time": 0.01,
            "total": 0.01, "tokens_known": False, "cache_split_known": False,
        }
        priced = cost_block(None, cost, cost_usd)
        assert "tokens unknown — time cost only: total 0.0100" in priced

    def test_cost_block_shows_the_cache_check_verdict_and_per_call_lines(self) -> None:
        from windtunnel._cli.ladder import cost_block

        cost = {"wall_s": 1.0, "input_tokens": 100, "cached_tokens": 5, "output_tokens": 10}
        cache_check = {
            "result": "fail",
            "reason": "prompt cache miss at call 2: cached 5 of 50 prompt tokens",
            "calls": ["call 2: cached 5/50"],
        }
        block = cost_block(None, cost, None, cache_check)
        assert "cache check: fail — prompt cache miss at call 2: cached 5 of 50" in block
        assert "call 2: cached 5/50" in block

    def test_ladder_summary_sums_cost_usd_per_tier_and_flags_unknown_tokens(self) -> None:
        from windtunnel._cli.ladder import cumulative_cost_usd, ladder_summary

        rows = [
            {"kind": "sweep", "tier": "focused", "sweep_id": "a",
             "cost": {"wall_s": 1.0}, "cost_usd": {"total": 1.5, "tokens_known": True}},
            {"kind": "sweep", "tier": "focused", "sweep_id": "b",
             "cost": {"wall_s": 1.0}, "cost_usd": {"total": 0.5, "tokens_known": False}},
            {"kind": "sweep", "tier": "regression", "sweep_id": "c", "cost": {"wall_s": 1.0}},
        ]
        summary = ladder_summary(rows)
        assert summary["focused"]["cost_usd"] == 2.0
        assert summary["focused"]["priced_sweeps"] == 2
        assert summary["focused"]["tokens_unknown_sweeps"] == 1
        assert summary["regression"]["priced_sweeps"] == 0
        assert cumulative_cost_usd(summary) == 2.0

    def test_cumulative_cost_usd_is_none_when_nothing_priced(self) -> None:
        from windtunnel._cli.ladder import cumulative_cost_usd

        assert cumulative_cost_usd({"focused": {"priced_sweeps": 0, "cost_usd": 0.0}}) is None


class TestCacheConfig:
    """[tool.windtunnel.cache]: the prompt-cache-miss check's config."""

    def test_no_cache_table_gives_none(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_cache_config

        assert load_cache_config(tmp_path) is None

    def test_defaults_are_off_and_half(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_cache_config

        (tmp_path / "pyproject.toml").write_text("[tool.windtunnel.cache]\n")
        assert load_cache_config(tmp_path) == {"fail_on_miss": False, "min_cached_ratio": 0.5}

    def test_reads_fail_on_miss_and_ratio(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import load_cache_config

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nfail_on_miss = true\nmin_cached_ratio = 0.8\n"
        )
        assert load_cache_config(tmp_path) == {"fail_on_miss": True, "min_cached_ratio": 0.8}

    def test_invalid_config_is_refused_not_ignored(self, tmp_path: Path) -> None:
        from windtunnel._cli.ladder import LadderError, load_cache_config

        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nmin_cached_ratio = 1.5\n"
        )
        with pytest.raises(LadderError, match="min_cached_ratio"):
            load_cache_config(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nfail_on_miss = 1\n"
        )
        with pytest.raises(LadderError, match="fail_on_miss"):
            load_cache_config(tmp_path)


class TestCallUsageTotals:
    def test_sums_prompt_cached_and_completion_across_calls(self) -> None:
        from windtunnel._cli.ladder import call_usage_totals

        calls = [
            {"conversation": "a", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10},
            {"conversation": "a", "prompt_tokens": 150, "cached_tokens": 90, "completion_tokens": 8},
        ]
        assert call_usage_totals(calls) == {
            "input_tokens": 250, "cached_tokens": 90, "output_tokens": 18,
        }

    def test_cached_goes_unknown_independently_of_the_totals(self) -> None:
        from windtunnel._cli.ladder import call_usage_totals

        calls = [
            {"conversation": "a", "prompt_tokens": 100, "cached_tokens": None, "completion_tokens": 10},
            {"conversation": "a", "prompt_tokens": 150, "cached_tokens": 90, "completion_tokens": 8},
        ]
        totals = call_usage_totals(calls)
        assert totals == {"input_tokens": 250, "cached_tokens": None, "output_tokens": 18}

    def test_none_when_there_are_no_calls(self) -> None:
        from windtunnel._cli.ladder import call_usage_totals

        assert call_usage_totals(None) == {
            "input_tokens": None, "cached_tokens": None, "output_tokens": None,
        }
        assert call_usage_totals([]) == {
            "input_tokens": None, "cached_tokens": None, "output_tokens": None,
        }


class TestCacheMissCheck:
    """check_cache_misses: every model call after the first in a conversation."""

    def test_a_call_below_the_ratio_fails_with_the_call_number_and_counts(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        calls = [
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c1", "prompt_tokens": 50, "cached_tokens": 5, "completion_tokens": 5},
        ]
        result = check_cache_misses(calls, min_cached_ratio=0.5)
        assert result["result"] == "fail"
        assert result["reason"] == "prompt cache miss at call 2: cached 5 of 50 prompt tokens"
        assert result["calls"] == ["call 2: cached 5/50"]

    def test_the_first_call_in_a_conversation_is_never_checked(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        # call 1 is 0 cached of 100 — would fail any positive ratio, but it's
        # never checked, and there is no call 2 to check either.
        calls = [
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
        ]
        assert check_cache_misses(calls, min_cached_ratio=0.5) == {
            "result": "pass", "reason": None, "calls": [],
        }

    def test_a_call_at_or_above_the_ratio_passes(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        calls = [
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": 50, "completion_tokens": 5},
        ]
        result = check_cache_misses(calls, min_cached_ratio=0.5)
        assert result == {"result": "pass", "reason": None, "calls": ["call 2: cached 50/100"]}

    def test_an_unreported_cache_split_is_unknown_never_pass(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        calls = [
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c1", "prompt_tokens": 100, "cached_tokens": None, "completion_tokens": 5},
        ]
        result = check_cache_misses(calls, min_cached_ratio=0.5)
        assert result["result"] == "unknown"
        assert result["calls"] == ["call 2: cache split unknown"]

    def test_call_numbering_and_the_first_call_reset_per_conversation(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        # Two runs of the same scenario pooled into one list (their
        # "conversation" ids differ): each has its own "first call".
        calls = [
            {"conversation": "run-1", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "run-2", "prompt_tokens": 200, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "run-1", "prompt_tokens": 100, "cached_tokens": 90, "completion_tokens": 5},
            {"conversation": "run-2", "prompt_tokens": 200, "cached_tokens": 10, "completion_tokens": 5},
        ]
        result = check_cache_misses(calls, min_cached_ratio=0.5)
        assert result["result"] == "fail"
        assert "prompt cache miss at call 2: cached 10 of 200 prompt tokens" in result["reason"]
        assert set(result["calls"]) == {"call 2: cached 90/100", "call 2: cached 10/200"}

    def test_no_calls_is_a_trivial_pass(self) -> None:
        from windtunnel._cli.ladder import check_cache_misses

        assert check_cache_misses(None, min_cached_ratio=0.5) == {
            "result": "pass", "reason": None, "calls": [],
        }

    def test_combine_cache_checks_fail_beats_unknown_beats_pass(self) -> None:
        from windtunnel._cli.ladder import combine_cache_checks

        passing = {"result": "pass", "reason": None, "calls": ["call 2: cached 90/100"]}
        unknown = {"result": "unknown", "reason": "cache split not reported for one or more calls",
                   "calls": ["call 2: cache split unknown"]}
        failing = {"result": "fail", "reason": "prompt cache miss at call 2: cached 1 of 100 "
                   "prompt tokens", "calls": ["call 2: cached 1/100"]}

        assert combine_cache_checks([passing, unknown])["result"] == "unknown"
        assert combine_cache_checks([passing, unknown, failing])["result"] == "fail"
        assert combine_cache_checks([passing, passing]) == {
            "result": "pass", "reason": None,
            "calls": ["call 2: cached 90/100", "call 2: cached 90/100"],
        }
        assert combine_cache_checks([]) == {"result": "pass", "reason": None, "calls": []}


class TestPerCallUsageEndToEnd:
    """A fake runtime emitting per-call usage, through run_scenario end to end.

    Exercises the real production path — AgentHandle.send() usage dicts ->
    _run_once -> Trace.model_calls -> call_usage_totals / compute_cost_usd /
    check_cache_misses — not a hand-built Trace.
    """

    def test_a_three_call_conversation_prices_and_flags_a_cache_miss(self) -> None:
        from windtunnel._cli.ladder import call_usage_totals, check_cache_misses, compute_cost_usd

        scenario_helper = _UsageHandle([
            # call 1: never checked for cache misses (nothing to cache yet).
            {"usage": {"prompt_tokens": 1000, "cached_tokens": 0, "completion_tokens": 50}},
            # call 2: reports a cache split below any reasonable ratio (miss).
            {"usage": {"input_tokens": 1200, "cached_tokens": 100, "output_tokens": 40}},
            # call 3: a healthy cache hit.
            {"usage": {"prompt_tokens": 1500, "cached_tokens": 1300, "completion_tokens": 30}},
        ])

        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        scenario = Scenario(
            name="three-turn", prompt="c", user_turns=["a", "b", "c"], target_facts=[["ok"]],
        )
        trace = run_scenario(scenario, _Runtime(scenario_helper)).runs[0].trace

        assert trace.model_calls is not None and len(trace.model_calls) == 3
        conversation = trace.model_calls[0]["conversation"]
        assert all(call["conversation"] == conversation for call in trace.model_calls)

        totals = call_usage_totals(trace.model_calls)
        assert totals == {"input_tokens": 3700, "cached_tokens": 1400, "output_tokens": 120}

        pricing = {
            "time_per_hour": 0.0,
            "models": {"big": {"input_per_m": 1.0, "output_per_m": 1.0, "cache_read_per_m": 0.1}},
        }
        cost_usd = compute_cost_usd(
            wall_s=0.0, input_tokens=totals["input_tokens"], cached_tokens=totals["cached_tokens"],
            output_tokens=totals["output_tokens"], model="big", pricing=pricing,
        )
        assert cost_usd["cache_split_known"] is True
        assert cost_usd["uncached_input"] == pytest.approx((3700 - 1400) / 1e6)
        assert cost_usd["cache_read"] == pytest.approx(1400 * 0.1 / 1e6)

        check = check_cache_misses(trace.model_calls, min_cached_ratio=0.5)
        assert check["result"] == "fail"
        assert check["reason"] == "prompt cache miss at call 2: cached 100 of 1200 prompt tokens"
        assert check["calls"] == [
            "call 2: cached 100/1200", "call 3: cached 1300/1500",
        ]

    def test_one_send_with_several_inference_calls_is_not_diluted_into_the_aggregate(
        self,
    ) -> None:
        """usage["calls"]: one send (an agent's internal tool loop) making
        several model calls. A miss on the 2nd of 3 must still be caught,
        even though the SEND's aggregate cache ratio is high (9500/10000).
        """
        from windtunnel._cli.ladder import call_usage_totals, check_cache_misses
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        scenario_helper = _UsageHandle([{
            "usage": {
                # aggregate (if a caller summed only this): 9500/10000 cached
                # — well above any reasonable ratio, and would hide the miss.
                "prompt_tokens": 10000, "cached_tokens": 9500, "completion_tokens": 60,
                "calls": [
                    {"prompt_tokens": 1000, "cached_tokens": 0, "completion_tokens": 10},
                    {"prompt_tokens": 4000, "cached_tokens": 500, "completion_tokens": 20},
                    {"prompt_tokens": 5000, "cached_tokens": 4900, "completion_tokens": 30},
                ],
            },
        }])
        scenario = Scenario(name="one-send-three-calls", prompt="p", target_facts=[["ok"]])
        trace = run_scenario(scenario, _Runtime(scenario_helper)).runs[0].trace

        # One send -> three model_calls entries, not one aggregated entry.
        assert trace.model_calls is not None and len(trace.model_calls) == 3
        conversation = trace.model_calls[0]["conversation"]
        assert all(call["conversation"] == conversation for call in trace.model_calls)
        assert call_usage_totals(trace.model_calls) == {
            "input_tokens": 10000, "cached_tokens": 5400, "output_tokens": 60,
        }

        check = check_cache_misses(trace.model_calls, min_cached_ratio=0.5)
        assert check["result"] == "fail"
        assert check["reason"] == "prompt cache miss at call 2: cached 500 of 4000 prompt tokens"
        assert check["calls"] == ["call 2: cached 500/4000", "call 3: cached 4900/5000"]

    def test_a_call_may_override_its_conversation_for_a_side_conversation(self) -> None:
        """A "calls" entry may name its own conversation (e.g. a forked
        review call), so its own first call is judged as a first call of
        its own conversation rather than folded into the run's.
        """
        from windtunnel._cli.ladder import check_cache_misses
        from windtunnel.api.runner import run_scenario
        from windtunnel.api.scenario import Scenario

        scenario_helper = _UsageHandle([{
            "usage": {
                "calls": [
                    {"prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
                    # A miss, but it is call 1 of its own "review" conversation
                    # — never checked, since the first call never is.
                    {"prompt_tokens": 200, "cached_tokens": 0, "completion_tokens": 5,
                     "conversation": "review"},
                ],
            },
        }])
        scenario = Scenario(name="side-conversation", prompt="p", target_facts=[["ok"]])
        trace = run_scenario(scenario, _Runtime(scenario_helper)).runs[0].trace

        assert trace.model_calls is not None
        run_conversation = trace.model_calls[0]["conversation"]
        assert trace.model_calls[1]["conversation"] == "review"
        assert run_conversation != "review"

        check = check_cache_misses(trace.model_calls, min_cached_ratio=0.5)
        assert check == {"result": "pass", "reason": None, "calls": []}


class TestWtRunCacheCheck:
    """[tool.windtunnel.cache] end to end through `wt run`: off by default, a
    miss fails the sweep when configured, unknown never passes.
    """

    @pytest.fixture
    def cache_bench(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
        from datetime import UTC, datetime

        import windtunnel.cli as cli
        from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
        from windtunnel.api.runner import ScenarioResult
        from windtunnel.api.score import LayerResult, Score
        from windtunnel.api.trace import Trace, Turn, compute_hash

        monkeypatch.chdir(tmp_path)
        alpha = _scenario("alpha")
        state: dict[str, Any] = {"model_calls": None}  # set per test

        def fake_run_scenario(scenario, runtime, *a, **k):  # noqa: ANN001, ANN202
            started = datetime(2026, 1, 1, tzinfo=UTC)
            trace = Trace(
                scenario_id=scenario.name, agent_id="a", variant_id="v", model="m", quant="q",
                sampler={}, started_at=started, finished_at=started,
                turns=[Turn(role="user", content="hi", tool_calls=[], tool_results=[],
                             latency_ms=0.0)],
                tool_schema_hash=compute_hash(scenario.name), worker_warnings=[],
                model_calls=state["model_calls"],
            )
            score = Score(
                outcome=LayerResult(passed=True, detail="ok"),
                trajectory=LayerResult(passed=True, detail="ok"),
                constraint=LayerResult(passed=True, detail="ok"),
                integrity=LayerResult(passed=True, detail="ok"),
            )
            run = ScenarioRunResult(score=score, trace=trace)
            return ScenarioResult(
                aggregate=aggregate_runs([run], gate_layers=scenario.resolved_gate_layers()),
                runs=[run],
            )

        _patch_cli_run(monkeypatch, [_pack("p", [alpha])], {})
        import windtunnel.api.runner as runner
        monkeypatch.setattr(runner, "run_scenario", fake_run_scenario)
        monkeypatch.setattr(
            cli, "compute_fingerprint",
            lambda **kwargs: Fingerprint(value="sha256:one", parts={"git_tree": "sha256:one"}),
        )
        runs_dir = tmp_path / "runs"

        def run(*extra: str) -> int:
            return cli.main(["run", "--runs-dir", str(runs_dir), "--label", "l",
                              "--scenario", "alpha", *extra])

        return run, state, runs_dir

    @staticmethod
    def _ledger(runs_dir: Path) -> list[dict[str, Any]]:
        return TestWtRunLadder._ledger(runs_dir)

    def test_off_by_default_a_miss_does_not_fail_the_sweep(self, cache_bench) -> None:  # noqa: ANN001
        run, state, runs_dir = cache_bench
        state["model_calls"] = [
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 1, "completion_tokens": 5},
        ]
        assert run() == 0
        assert "cache_check" not in self._ledger(runs_dir)[-1]["experiment"]

    def test_a_configured_miss_fails_the_sweep_and_is_recorded(
        self, cache_bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, runs_dir = cache_bench
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nfail_on_miss = true\nmin_cached_ratio = 0.5\n"
        )
        state["model_calls"] = [
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 1, "completion_tokens": 5},
        ]
        assert run() == 1  # the scenario itself passed; the cache miss fails the sweep
        err = capsys.readouterr().err
        assert "cache check: fail — prompt cache miss at call 2: cached 1 of 100" in err
        assert "call 2: cached 1/100" in err
        row = self._ledger(runs_dir)[-1]
        assert row["experiment"]["counts_as_failure"] is True
        assert row["experiment"]["cache_check"]["result"] == "fail"

    def test_an_unreported_split_is_unknown_and_still_fails_when_configured(
        self, cache_bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, runs_dir = cache_bench
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nfail_on_miss = true\n"
        )
        state["model_calls"] = [
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": None, "completion_tokens": 5},
        ]
        assert run() == 1
        assert "cache check: unknown" in capsys.readouterr().err
        row = self._ledger(runs_dir)[-1]
        assert row["experiment"]["cache_check"]["result"] == "unknown"

    def test_a_healthy_cache_hit_passes_and_is_recorded(
        self, cache_bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, runs_dir = cache_bench
        (tmp_path / "pyproject.toml").write_text(
            "[tool.windtunnel.cache]\nfail_on_miss = true\n"
        )
        state["model_calls"] = [
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 5},
            {"conversation": "c", "prompt_tokens": 100, "cached_tokens": 90, "completion_tokens": 5},
        ]
        assert run() == 0
        assert "cache check: pass" in capsys.readouterr().err
        row = self._ledger(runs_dir)[-1]
        assert row["experiment"]["cache_check"]["result"] == "pass"


class TestWtRunCostAndModel:
    """End to end through `wt run`, on TestWtRunLadder's bench."""

    bench = TestWtRunLadder.bench
    _ledger = staticmethod(TestWtRunLadder._ledger)

    @pytest.fixture
    def labelled(self, bench, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
        import windtunnel.cli as cli

        run, state, runs_dir = bench
        state["model"] = "small"

        class Plugin:
            def model_label(self, runtime_name: str) -> str:
                return str(state["model"])

        monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda runtime_name: Plugin())
        monkeypatch.setattr(
            cli,
            "compute_fingerprint",
            lambda **kwargs: Fingerprint(
                value=f"{state['fp']}|{kwargs['model']}",
                parts={"git_tree": state["fp"], "target": kwargs["target"],
                       "model": kwargs["model"]},
            ),
        )
        return run, state, runs_dir

    def test_a_pass_on_one_model_does_not_earn_a_regression_on_another(
        self, labelled, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, _runs_dir = labelled
        declare = ("--question", "q", "--expect", "pass")
        assert run("--scenario", "alpha") == 0  # focused pass on the small model
        state["model"] = "big"
        capsys.readouterr()
        assert run(*declare) == 2
        assert "changed: model" in capsys.readouterr().err
        assert run("--scenario", "alpha", *declare) == 0  # the same pass, on big
        assert run(*declare) == 1  # earned (beta still fails)

    def test_the_tier_model_policy_refuses_the_wrong_model(
        self, labelled, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        run, state, runs_dir = labelled
        (tmp_path / "pyproject.toml").write_text(
            '[tool.windtunnel.ladder.models]\nfocused = ["big"]\n'
        )
        assert run("--scenario", "alpha") == 2
        assert "this runtime uses 'small'" in capsys.readouterr().err
        assert not (runs_dir / "ledger.ndjsonl").exists()
        state["model"] = "big"
        assert run("--scenario", "alpha") == 0

    def test_a_sweep_records_its_cost_plan_and_outcome(
        self, labelled, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        from windtunnel._cli.ladder import read_experiments

        run, _state, runs_dir = labelled
        assert run("--scenario", "alpha", "--expect", "fail", "--if-pass",
                   "ship it", "--if-fail", "probe turn 2") == 0
        assert "you planned, on this outcome: ship it" in capsys.readouterr().err
        row = self._ledger(runs_dir)[-1]
        cost = row["experiment"]["cost"]
        assert isinstance(cost["wall_s"], float)
        # The fake runs report no tokens: recorded as unknown, never as zero.
        assert cost["input_tokens"] is None and cost["output_tokens"] is None
        assert row["experiment"]["if_pass"] == "ship it"
        [sweep] = read_experiments(runs_dir)
        assert sweep["sweep_id"] == row["sweep_id"]
        assert sweep["tier"] == "focused"
        assert sweep["model"] == "small"
        assert sweep["outcome"] == "pass"
        assert sweep["prediction_held"] is False

    def test_reviews_feed_the_per_tier_summary(
        self, labelled, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        import windtunnel.cli as cli

        run, _state, runs_dir = labelled
        declare = ("--question", "q", "--expect", "pass")
        run("--scenario", "alpha")
        run("--scenario", "gamma", *declare)
        first, second = (row["sweep_id"] for row in self._ledger(runs_dir))
        assert cli.main(["review", first, "--runs", str(runs_dir), "--decision", "kept"]) == 0
        assert cli.main(["review", second, "--runs", str(runs_dir), "--no-change"]) == 0
        assert cli.main(["review", "nope", "--runs", str(runs_dir), "--no-change"]) == 2
        capsys.readouterr()
        assert cli.main(["results", "--runs", str(runs_dir), "--ladder", "--json"]) == 0
        focused = json.loads(capsys.readouterr().out)["tiers"]["focused"]
        assert focused["sweeps"] == 2
        assert focused["predictions"] == 1 and focused["predictions_held"] == 1
        assert focused["reviewed"] == 2 and focused["changed_decision"] == 1
        assert focused["tokens_reported"] == 0
        assert cli.main(["results", "--runs", str(runs_dir), "--ladder"]) == 0
        assert "1/2 reviewed changed a decision" in capsys.readouterr().out

    _PRICING_TOML = (
        "[tool.windtunnel.ladder.pricing]\ntime_per_hour = 3600.0\n"
        "[tool.windtunnel.ladder.pricing.models]\n"
        'small = { input_per_m = 1.0, output_per_m = 1.0 }\n'
    )

    def test_pricing_prices_the_sweep_by_time_only_when_tokens_go_unreported(
        self, labelled, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        from windtunnel._cli.ladder import read_experiments

        run, _state, runs_dir = labelled
        (tmp_path / "pyproject.toml").write_text(self._PRICING_TOML)
        assert run("--scenario", "alpha") == 0
        err = capsys.readouterr().err
        assert "wt run: cost" in err
        assert "tokens: unknown" in err
        assert "time cost only" in err
        row = self._ledger(runs_dir)[-1]
        cost_usd = row["experiment"]["cost_usd"]
        assert cost_usd["tokens_known"] is False and cost_usd["uncached_input"] is None
        assert cost_usd["total"] == cost_usd["time"] >= 0
        [sweep] = read_experiments(runs_dir)
        sweep_cost_usd = sweep["cost_usd"]
        assert sweep_cost_usd["tokens_known"] is False and sweep_cost_usd["uncached_input"] is None
        assert sweep_cost_usd["total"] == sweep_cost_usd["time"] >= 0

    def test_no_pricing_configured_means_no_cost_usd_field(self, labelled) -> None:  # noqa: ANN001
        from windtunnel._cli.ladder import read_experiments

        run, _state, runs_dir = labelled
        assert run("--scenario", "alpha") == 0
        row = self._ledger(runs_dir)[-1]
        assert "cost_usd" not in row["experiment"]
        [sweep] = read_experiments(runs_dir)
        assert "cost_usd" not in sweep

    def test_results_ladder_shows_dollars_and_a_cumulative_total(
        self, labelled, tmp_path: Path, capsys: pytest.CaptureFixture[str]  # noqa: ANN001
    ) -> None:
        import windtunnel.cli as cli

        run, _state, runs_dir = labelled
        (tmp_path / "pyproject.toml").write_text(self._PRICING_TOML)
        assert run("--scenario", "alpha") == 0
        capsys.readouterr()
        assert cli.main(["results", "--runs", str(runs_dir), "--ladder", "--json"]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["cumulative_cost_usd"] >= 0
        assert document["tiers"]["focused"]["priced_sweeps"] == 1
        assert cli.main(["results", "--runs", str(runs_dir), "--ladder"]) == 0
        out = capsys.readouterr().out
        assert "1/1 priced" in out
        assert "tokens unknown" in out
        assert "cumulative: $" in out
