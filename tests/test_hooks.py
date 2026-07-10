from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from windtunnel.api.aggregate import ScenarioRunResult, aggregate_runs
from windtunnel.api.runner import ScenarioResult, run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import LayerResult, Score
from windtunnel.api.trace import Trace, Turn, save_trace, storage_path
from windtunnel.hooks.debrief import DebriefHook
from windtunnel.runtimes.in_memory import InMemoryRuntime
from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.hooks import Hook, HookContext


def _scenario(name: str = "hook_scenario", *, fact: str = "ok") -> Scenario:
    return Scenario(name=name, prompt="say ok", target_facts=[[fact]])


def _trace(scenario_id: str = "hook_scenario") -> Trace:
    now = datetime.now(UTC)
    return Trace(
        scenario_id=scenario_id,
        agent_id="agent",
        variant_id="variant",
        model="model",
        quant="unknown",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=[
            Turn(role="user", content="say ok", tool_calls=[], tool_results=[], latency_ms=0),
            Turn(role="assistant", content="ok", tool_calls=[], tool_results=[], latency_ms=1),
        ],
        tool_schema_hash="sha256:test",
        worker_warnings=[],
    )


def _score(
    passed: bool = True,
    *,
    trajectory_passed: bool = True,
    constraint_passed: bool = True,
    integrity_passed: bool = True,
) -> Score:
    return Score(
        outcome=LayerResult(passed=passed, detail="outcome detail"),
        trajectory=LayerResult(passed=trajectory_passed, detail="trajectory detail"),
        constraint=LayerResult(passed=constraint_passed, detail="constraint detail"),
        integrity=LayerResult(passed=integrity_passed, detail="integrity detail"),
    )


def test_hooks_fire_in_activation_order_at_runner_points() -> None:
    events: list[tuple[str, str, bool, bool, bool]] = []

    class RecordingHook(Hook):
        def __init__(self, name: str) -> None:
            self.name = name

        def _record(self, point: str, ctx: HookContext) -> None:
            events.append((
                self.name,
                point,
                ctx.run_id is not None,
                ctx.trace is not None,
                ctx.aggregate is not None,
            ))

        def on_provisioned(self, ctx: HookContext) -> None:
            self._record("on_provisioned", ctx)

        def on_run_start(self, ctx: HookContext) -> None:
            self._record("on_run_start", ctx)

        def on_run_scored(self, ctx: HookContext) -> None:
            self._record("on_run_scored", ctx)

        def on_run_end(self, ctx: HookContext) -> None:
            self._record("on_run_end", ctx)

        def on_scenario_end(self, ctx: HookContext) -> None:
            self._record("on_scenario_end", ctx)

    result = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        hooks=[RecordingHook("first"), RecordingHook("second")],
    )

    assert result.aggregate.verdict == "PASS"
    assert [(name, point) for name, point, *_ in events] == [
        ("first", "on_provisioned"),
        ("second", "on_provisioned"),
        ("first", "on_run_start"),
        ("second", "on_run_start"),
        ("first", "on_run_scored"),
        ("second", "on_run_scored"),
        ("first", "on_run_end"),
        ("second", "on_run_end"),
        ("first", "on_scenario_end"),
        ("second", "on_scenario_end"),
    ]
    assert events[0][2:] == (False, False, False)
    assert events[2][2:] == (True, False, False)
    assert events[4][2:] == (True, True, False)
    assert events[-1][2:] == (False, False, True)


def test_converse_raises_outside_on_run_scored() -> None:
    phases: dict[str, str] = {}

    class InvalidConverseHook(Hook):
        name = "invalid_converse"

        def _check(self, point: str, ctx: HookContext) -> None:
            try:
                ctx.converse("not now")
            except RuntimeError:
                phases[point] = "raised"
            else:
                phases[point] = "allowed"

        def on_provisioned(self, ctx: HookContext) -> None:
            self._check("on_provisioned", ctx)

        def on_run_start(self, ctx: HookContext) -> None:
            self._check("on_run_start", ctx)

        def on_run_end(self, ctx: HookContext) -> None:
            self._check("on_run_end", ctx)

        def on_scenario_end(self, ctx: HookContext) -> None:
            self._check("on_scenario_end", ctx)

    run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        hooks=[InvalidConverseHook()],
    )

    assert phases == {
        "on_provisioned": "raised",
        "on_run_start": "raised",
        "on_run_end": "raised",
        "on_scenario_end": "raised",
    }


def test_converse_uses_run_session_id_and_is_absent_from_trace_turns() -> None:
    class ConverseHook(Hook):
        name = "converse_hook"

        def __init__(self) -> None:
            self.reply = ""
            self.session_id = ""

        def on_run_scored(self, ctx: HookContext) -> None:
            self.session_id = ctx.session_id or ""
            self.reply = ctx.converse("debrief question")

    hook = ConverseHook()
    runtime = InMemoryRuntime(scripted_responses=["ok", "hook reply"])
    result = run_scenario(_scenario(), runtime, hooks=[hook])
    _config, handle = runtime.provisions[0]

    assert hook.reply == "hook reply"
    assert len(handle.calls) == 2
    assert handle.calls[0][1] == handle.calls[1][1] == hook.session_id
    assert handle.calls[1][0] == [{"role": "user", "content": "debrief question"}]
    turn_text = [turn.content for turn in result.runs[0].trace.turns]
    assert turn_text == ["say ok", "ok"]


def test_hook_exception_becomes_worker_warning_and_verdict_is_unchanged() -> None:
    class FailingHook(Hook):
        name = "failing_hook"

        def on_run_scored(self, ctx: HookContext) -> None:
            raise RuntimeError("boom")

    result = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        hooks=[FailingHook()],
    )

    assert result.aggregate.verdict == "PASS"
    assert "hook:failing_hook: boom" in result.runs[0].trace.worker_warnings


def test_provision_hook_exception_is_surfaced_on_scenario_result() -> None:
    class ProvisionFailHook(Hook):
        name = "provision_fail"

        def on_provisioned(self, ctx: HookContext) -> None:
            raise RuntimeError("setup note")

    result = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        hooks=[ProvisionFailHook()],
    )

    assert result.aggregate.verdict == "PASS"
    assert result.worker_warnings == ["hook:provision_fail: setup note"]


def test_emit_artifact_buffers_and_cli_writes_run_sidecars(tmp_path: Path) -> None:
    import windtunnel.cli as cli

    class ArtifactHook(Hook):
        name = "artifact_hook"

        def on_run_scored(self, ctx: HookContext) -> None:
            ctx.emit_artifact({"plain": True})
            ctx.emit_artifact({"labeled": True}, label="details")

    result = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        hooks=[ArtifactHook()],
    )
    artifacts = result.runs[0].hook_artifacts
    assert [artifact.label for artifact in artifacts] == [None, "details"]

    trace_path = storage_path(result.runs[0].trace, base_dir=tmp_path / "runs")
    save_trace(result.runs[0].trace, trace_path)
    written = [cli._write_hook_artifact_sidecar(trace_path, artifact) for artifact in artifacts]

    assert written[0] == trace_path.with_suffix(".artifact_hook.json")
    assert written[1] == trace_path.with_suffix(".artifact_hook.details.json")
    assert json.loads(written[0].read_text(encoding="utf-8")) == {"plain": True}
    assert json.loads(written[1].read_text(encoding="utf-8")) == {"labeled": True}


def test_pack_end_hook_emits_pack_artifact(tmp_path: Path) -> None:
    import windtunnel.cli as cli

    class PackHook(Hook):
        name = "packhook"

        def on_pack_end(self, ctx: HookContext) -> None:
            ctx.emit_artifact({"aggregate_count": len(ctx.aggregate)})

    run = ScenarioRunResult(score=_score(), trace=_trace())
    result = ScenarioResult(aggregate=aggregate_runs([run]), runs=[run])
    completed = [
        cli._CompletedAggregate(
            pack=SimpleNamespace(name="pack"),
            scenario=_scenario(),
            result=result,
            transport_only=False,
            had_runner_error=False,
        )
    ]

    artifacts = cli._dispatch_pack_end_hooks(
        [PackHook()],
        config=AgentConfig(),
        completed=completed,
    )
    path = cli._write_pack_hook_artifact(tmp_path, "20260102T030405000000Z", artifacts[0])

    assert path.name == "20260102T030405000000Z.packhook.pack.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"aggregate_count": 1}


def test_hook_unknown_name_errors_and_lists_available(capsys: pytest.CaptureFixture[str]) -> None:
    import windtunnel.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli._resolve_hooks(["missing_hook"])

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "unknown hook 'missing_hook'" in err
    assert "debrief" in err


def test_hook_entry_point_resolution_instantiates_class(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    import windtunnel.cli as cli

    class ExternalHook(Hook):
        name = "external"

    class FakeEntryPoint:
        name = "external"

        def load(self):
            return ExternalHook

    def fake_entry_points(*, group: str):
        assert group == "windtunnel.hooks"
        return [FakeEntryPoint()]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

    hooks = cli._resolve_hooks(["external"])

    assert len(hooks) == 1
    assert isinstance(hooks[0], ExternalHook)


def test_debrief_defaults_to_fail_only_and_env_all_includes_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WT_DEBRIEF_ON", raising=False)

    passing = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok", "unused debrief"]),
        hooks=[DebriefHook()],
    )
    assert passing.runs[0].hook_artifacts == []

    failing = run_scenario(
        _scenario(fact="missing"),
        InMemoryRuntime(scripted_responses=["nope", "failure debrief"]),
        hooks=[DebriefHook()],
    )
    assert len(failing.runs[0].hook_artifacts) == 1
    assert failing.runs[0].hook_artifacts[0].payload["verdict"] == "FAIL"

    monkeypatch.setenv("WT_DEBRIEF_ON", "all")
    passing_with_env = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok", "pass debrief"]),
        hooks=[DebriefHook()],
    )
    assert len(passing_with_env.runs[0].hook_artifacts) == 1
    assert passing_with_env.runs[0].hook_artifacts[0].payload["verdict"] == "PASS"


def test_debrief_artifact_schema_records_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowHandle:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict], str]] = []

        def send(self, messages: list[dict], session_id: str) -> dict:
            self.calls.append((messages, session_id))
            if len(self.calls) == 1:
                return {"content": "nope"}
            time.sleep(0.05)
            return {"content": "late"}

        def reset_state(self) -> None:
            self.calls.clear()

        def teardown(self) -> None:
            pass

    class SlowRuntime:
        def __init__(self) -> None:
            self.handle = SlowHandle()

        def provision(self, config: AgentConfig, mcps: list | None = None):
            return self.handle

    monkeypatch.setenv("WT_HOOK_CONVERSE_TIMEOUT_S", "0.001")
    result = run_scenario(
        _scenario(fact="missing"),
        SlowRuntime(),
        hooks=[DebriefHook()],
    )

    payload = result.runs[0].hook_artifacts[0].payload
    assert set(payload) == {
        "schema_version",
        "run_id",
        "scenario_id",
        "agent",
        "model",
        "verdict",
        "failed_layers",
        "reasons",
        "prompt",
        "reply",
        "tools_disabled",
        "timed_out",
        "duration_ms",
        "error",
    }
    assert payload["schema_version"] == 2
    assert payload["tools_disabled"] is False
    assert payload["timed_out"] is True
    assert payload["error"]
    assert isinstance(payload["duration_ms"], int)
    assert payload["reasons"]["outcome"]
    assert payload["failed_layers"] == ["outcome"]


def test_sidecar_rule_skips_debrief_json_in_report_rescore_and_triage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import windtunnel.cli as cli
    from windtunnel.api.pack import ScenarioPack
    from windtunnel.report import load_runs

    scenario = _scenario("sidecar_case", fact="missing")
    run = ScenarioRunResult(score=_score(passed=False), trace=_trace("sidecar_case"))
    trace_path = storage_path(run.trace, base_dir=tmp_path / "runs")
    save_trace(run.trace, trace_path)
    cli._write_score_sidecar(trace_path, run.score, scenario)
    trace_path.with_suffix(".debrief.json").write_text(
        json.dumps({"schema_version": 1}),
        encoding="utf-8",
    )

    assert list(load_runs(tmp_path / "runs")) == [("sidecar_case", "variant")]
    paths = cli._rescore_trace_paths(
        argparse.Namespace(trace=None, runs=str(tmp_path / "runs"))
    )
    assert paths == [trace_path]

    monkeypatch.setattr(cli, "_discover_scenario_packs", lambda *args, **kwargs: [
        ScenarioPack(name="local", scenarios=[scenario]),
    ])
    rc = cli.main(["triage", "--runs", str(tmp_path / "runs")])
    assert rc == 0
    assert "Skipped (no score):** 0" in capsys.readouterr().out


def test_zero_hooks_path_does_not_add_trace_or_artifacts(tmp_path: Path) -> None:
    result = run_scenario(_scenario(), InMemoryRuntime(scripted_responses=["ok"]))
    trace_path = storage_path(result.runs[0].trace, base_dir=tmp_path / "runs")
    save_trace(result.runs[0].trace, trace_path)

    saved = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.runs[0].hook_artifacts == []
    assert result.worker_warnings == []
    assert result.runs[0].trace.worker_warnings == []
    assert "hook_artifacts" not in saved
    assert "hooks" not in saved


def test_zero_hooks_path_allocates_no_hook_state_and_trace_mints_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import windtunnel.api.runner as runner

    original_trace = runner.Trace
    trace_kwargs: list[dict] = []

    def trace_spy(*args, **kwargs):
        trace_kwargs.append(dict(kwargs))
        return original_trace(*args, **kwargs)

    def fail_hook_state(*args, **kwargs):
        raise AssertionError("_RunHookState should not be constructed without hooks")

    monkeypatch.setattr(runner, "Trace", trace_spy)
    monkeypatch.setattr(runner, "_RunHookState", fail_hook_state)

    result = runner.run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
    )

    assert len(trace_kwargs) == 1
    assert "run_id" not in trace_kwargs[0]
    assert result.runs[0].trace.run_id


def test_hooks_fire_on_synthetic_runner_errors() -> None:
    events: list[tuple[str, bool, bool]] = []

    class BrokenHandle:
        def reset_state(self) -> None:
            pass

        def send(self, messages: list[dict], session_id: str) -> dict:
            raise RuntimeError("send exploded")

        def teardown(self) -> None:
            pass

    class BrokenRuntime:
        def provision(self, config: AgentConfig, mcps: list | None = None):
            return BrokenHandle()

    class ErrorHook(Hook):
        name = "error_hook"

        def on_run_start(self, ctx: HookContext) -> None:
            events.append(("start", ctx.trace is not None, ctx.score is not None))

        def on_run_scored(self, ctx: HookContext) -> None:
            assert ctx.trace is not None
            assert ctx.score is not None
            assert ctx.run_id == ctx.trace.run_id
            events.append(("scored", ctx.trace is not None, ctx.score is not None))
            ctx.emit_artifact({"synthetic": True, "run_id": ctx.run_id})

        def on_run_end(self, ctx: HookContext) -> None:
            events.append(("end", ctx.trace is not None, ctx.score is not None))

    result = run_scenario(_scenario(), BrokenRuntime(), hooks=[ErrorHook()])
    run = result.runs[0]

    assert result.aggregate.verdict == "INVALID"
    assert any(w.startswith("runner_error: send exploded") for w in run.trace.worker_warnings)
    assert events == [
        ("start", False, False),
        ("scored", True, True),
        ("end", True, True),
    ]
    assert run.hook_artifacts[0].payload == {
        "synthetic": True,
        "run_id": run.trace.run_id,
    }


def test_cli_surfaces_scenario_and_pack_hook_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import windtunnel.api.runner as runner
    import windtunnel.cli as cli
    from windtunnel.api.pack import ScenarioPack

    scenario = _scenario("warning_case")
    run = ScenarioRunResult(score=_score(), trace=_trace("warning_case"))
    result = ScenarioResult(
        aggregate=aggregate_runs([run]),
        runs=[run],
        worker_warnings=["hook:scenario_warn: scenario note"],
    )

    class PackWarnHook(Hook):
        name = "pack_warn"

        def on_pack_end(self, ctx: HookContext) -> None:
            ctx.warn("pack note")

    monkeypatch.setattr(
        cli,
        "_discover_scenario_packs",
        lambda *args, **kwargs: [ScenarioPack(name="pack", scenarios=[scenario])],
    )
    monkeypatch.setattr(cli, "_build_runtime", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda runtime_name: object())
    monkeypatch.setattr(cli, "_resolve_hooks", lambda hook_names: [PackWarnHook()])
    monkeypatch.setattr(cli, "_git_sha", lambda: "testsha")
    monkeypatch.setattr(cli, "_wt_version", lambda: "0.test")
    monkeypatch.setattr(
        runner,
        "run_scenario",
        lambda *args, **kwargs: result,
    )

    rc = cli.main([
        "run",
        "--scenario",
        "warning_case",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--hook",
        "pack_warn",
    ])

    err = capsys.readouterr().err
    assert rc == 0
    assert "wt run: warning: hook:scenario_warn: scenario note" in err
    assert "wt run: warning: hook:pack_warn: pack note" in err


def test_late_reply_after_converse_timeout_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedHandle:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.calls = 0

        def send(self, messages: list[dict], session_id: str) -> dict:
            with self._lock:
                self.calls += 1
                call_no = self.calls
            if call_no == 1:
                time.sleep(0.05)
                return {"content": "late"}
            return {"content": f"reply-{call_no}"}

    monkeypatch.setenv("WT_HOOK_CONVERSE_TIMEOUT_S", "0.02")
    ctx = HookContext(
        hook_name="late_reply",
        phase="on_run_scored",
        session_id="session",
        handle=DelayedHandle(),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        ctx.converse("slow")
    assert ctx.converse("fast") == "reply-2"
    time.sleep(0.06)
    ctx.emit_artifact({"reply": "reply-2", "calls": list(ctx.converse_calls)})

    payload = ctx.artifacts[0].payload
    assert payload["reply"] == "reply-2"
    assert payload["timed_out"] is True
    assert [call["status"] for call in ctx.converse_calls] == ["timeout", "ok"]
    assert [call["timed_out"] for call in ctx.converse_calls] == [True, False]


def test_timed_out_hook_send_finishes_before_next_run_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a timed-out daemon send must not race reset_state() and
    repopulate the next run after its reset has completed."""
    events: list[str] = []

    class OrderedHandle:
        def __init__(self) -> None:
            self.reset_count = 0
            self.main_count = 0
            self.hook_count = 0

        def reset_state(self) -> None:
            self.reset_count += 1
            events.append(f"reset-{self.reset_count}")

        def send(self, messages: list[dict], session_id: str) -> dict:
            del session_id
            if messages[0]["content"] == "diagnostic":
                self.hook_count += 1
                call = self.hook_count
                events.append(f"hook-{call}-start")
                time.sleep(0.04)
                events.append(f"hook-{call}-end")
                return {"content": "late diagnostic"}
            self.main_count += 1
            events.append(f"main-{self.main_count}")
            return {"content": "ok"}

        def teardown(self) -> None:
            events.append("teardown")

    class OrderedRuntime:
        def __init__(self) -> None:
            self.handle = OrderedHandle()

        def provision(self, config: AgentConfig, mcps: list | None = None):
            del config, mcps
            return self.handle

    class TimeoutHook(Hook):
        name = "timeout_hook"

        def on_run_scored(self, ctx: HookContext) -> None:
            try:
                ctx.converse("diagnostic")
            except RuntimeError:
                pass

    monkeypatch.setenv("WT_HOOK_CONVERSE_TIMEOUT_S", "0.01")

    result = run_scenario(
        _scenario(),
        OrderedRuntime(),
        runs_per_scenario=2,
        hooks=[TimeoutHook()],
    )

    assert result.aggregate.total == 2
    assert events.index("hook-1-end") < events.index("reset-2")
    assert events.index("hook-2-end") < events.index("teardown")


def test_scenario_artifact_collision_uses_counter_and_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import windtunnel.cli as cli

    artifact = SimpleNamespace(hook_name="artifact_hook", label=None, payload={"n": 1})

    first = cli._write_scenario_hook_artifact(
        tmp_path,
        "20260102T030405000000Z",
        artifact,
        "scenario/one",
    )
    second = cli._write_scenario_hook_artifact(
        tmp_path,
        "20260102T030405000000Z",
        artifact,
        "scenario/one",
    )

    assert first.name == "20260102T030405000000Z.artifact_hook.scenario_one.pack.json"
    assert second.name == "20260102T030405000000Z.artifact_hook.scenario_one-2.pack.json"
    assert json.loads(second.read_text(encoding="utf-8")) == {"n": 1}
    assert "hook artifact target exists" in capsys.readouterr().err


def test_debrief_trajectory_only_failure_verdict_matches_score_sidecar(
    tmp_path: Path,
) -> None:
    import windtunnel.cli as cli

    class ReplyHandle:
        def send(self, messages: list[dict], session_id: str) -> dict:
            return {"content": "trajectory debrief"}

    scenario = _scenario("trajectory_only")
    scenario.gate_layers = ["outcome", "trajectory"]
    trace = _trace("trajectory_only")
    score = _score(passed=True, trajectory_passed=False)
    trace_path = storage_path(trace, base_dir=tmp_path / "runs")
    save_trace(trace, trace_path)
    score_path = cli._write_score_sidecar(trace_path, score, scenario)
    sidecar = json.loads(score_path.read_text(encoding="utf-8"))
    sidecar_verdict = sidecar["verdict"]

    ctx = HookContext(
        hook_name="debrief",
        phase="on_run_scored",
        scenario=scenario,
        agent=AgentConfig(),
        run_id=trace.run_id,
        session_id="session",
        trace=trace,
        score=score,
        handle=ReplyHandle(),
    )
    DebriefHook().on_run_scored(ctx)

    payload = ctx.artifacts[0].payload
    assert payload["verdict"] == sidecar_verdict == "FAIL"
    assert payload["failed_layers"] == ["trajectory"]
    assert payload["reply"] == "trajectory debrief"


def test_hook_run_ids_are_unique_per_run_and_match_trace() -> None:
    class RunIdHook(Hook):
        name = "run_id_hook"

        def __init__(self) -> None:
            self.started: list[str] = []
            self.scored: list[str] = []

        def on_run_start(self, ctx: HookContext) -> None:
            assert ctx.run_id is not None
            self.started.append(ctx.run_id)

        def on_run_scored(self, ctx: HookContext) -> None:
            assert ctx.trace is not None
            assert ctx.run_id == ctx.trace.run_id
            self.scored.append(ctx.trace.run_id)

    hook = RunIdHook()
    result = run_scenario(
        _scenario(),
        InMemoryRuntime(scripted_responses=["ok"]),
        runs_per_scenario=3,
        hooks=[hook],
    )

    trace_run_ids = [run.trace.run_id for run in result.runs]
    assert hook.started == trace_run_ids
    assert hook.scored == trace_run_ids
    assert len(set(trace_run_ids)) == 3
