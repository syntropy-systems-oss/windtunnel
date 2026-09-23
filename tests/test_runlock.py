"""The cross-process runtime lock that serializes sweeps sharing a runtime.

Contention is exercised with a real second process holding the lock, since
that is the situation the lock exists for: two `wt run` invocations (two
shells, two agents, two CI steps) against one runtime.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from windtunnel._cli.runlock import (
    EXIT_RUNTIME_BUSY,
    RuntimeBusy,
    holder_record,
    lock_dir,
    lock_path,
    runtime_lock,
)


@contextmanager
def _held_by_another_process(key: str, seconds: float) -> Iterator[subprocess.Popen[str]]:
    """Hold ``key``'s lock from a child process for ``seconds`` (or until exit)."""
    script = textwrap.dedent(
        f"""
        import sys, time
        from windtunnel._cli.runlock import holder_record, runtime_lock
        with runtime_lock({key!r}, wait=False, holder=holder_record(command="wt run --label other")):
            print("locked", flush=True)
            time.sleep({seconds})
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=dict(os.environ)
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        yield child
    finally:
        child.kill()
        child.wait(timeout=30)


class TestRuntimeLock:
    def test_lock_dir_honors_the_environment_override(self, tmp_path: Path) -> None:
        assert lock_dir() == tmp_path / ".wt-locks"  # set by tests/conftest.py

    def test_lock_paths_are_sanitized_and_distinct_per_key(self) -> None:
        first = lock_path("http_inject:http://127.0.0.1:8647")
        second = lock_path("http_inject:http://127.0.0.1:9000")
        assert first != second
        assert "/" not in first.name and ":" not in first.name

    def test_the_holder_record_is_readable_while_held_and_cleared_after(self) -> None:
        with runtime_lock("records", wait=False, holder=holder_record(label="a")):
            record = json.loads(lock_path("records").read_text(encoding="utf-8"))
            assert record["pid"] == os.getpid()
            assert record["label"] == "a"
        assert lock_path("records").read_text(encoding="utf-8") == ""

    def test_a_held_lock_refuses_without_waiting(self) -> None:
        with _held_by_another_process("busy", seconds=30):
            with pytest.raises(RuntimeBusy) as busy:
                with runtime_lock("busy", wait=False, holder=holder_record()):
                    pass
        assert busy.value.holder is not None
        assert busy.value.holder["command"] == "wt run --label other"
        assert "running `wt run --label other`" in str(busy.value)

    def test_a_waiting_acquirer_gets_the_lock_when_the_holder_exits(self) -> None:
        seen: dict[str, Any] = {}
        with _held_by_another_process("queue", seconds=0.6):
            started = time.monotonic()
            with runtime_lock(
                "queue",
                wait=True,
                holder=holder_record(),
                on_wait=lambda holder: seen.setdefault("holder", holder),
                on_acquired=lambda waited: seen.setdefault("waited", waited),
                poll_s=0.05,
            ):
                elapsed = time.monotonic() - started
        assert elapsed >= 0.3
        assert seen["holder"]["command"] == "wt run --label other"
        assert seen["waited"] >= 0.3

    def test_different_keys_do_not_contend(self) -> None:
        with _held_by_another_process("alpha", seconds=30):
            with runtime_lock("beta", wait=False, holder=holder_record()):
                pass


# ─── wt run ───────────────────────────────────────────────────────────────────


class _Runtime:
    accepts_runner_managed_mcps = False

    def provision(self, config: Any, mcps: list[Any] | None = None) -> _Handle:
        return _Handle()


class _Handle:
    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        return {"content": "ok"}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


def _wire(monkeypatch: pytest.MonkeyPatch, plugin: object, built: list[str]) -> None:
    import windtunnel.cli as cli
    from windtunnel.api.pack import ScenarioPack
    from windtunnel.api.scenario import Scenario

    scenario = Scenario(name="locked", prompt="q", target_facts=[["ok"]])
    monkeypatch.setattr(
        cli, "_discover_scenario_packs", lambda: [ScenarioPack(name="local", scenarios=[scenario])]
    )
    monkeypatch.setattr(cli, "_resolve_runtime_plugin", lambda _name: plugin)

    def _build(runtime_name: str, label: str, soul_path: str | None, **_kwargs: Any) -> _Runtime:
        built.append(runtime_name)
        return _Runtime()

    monkeypatch.setattr(cli, "_build_runtime", _build)


def _events(runs_dir: Path) -> list[dict[str, Any]]:
    path = runs_dir / "events.ndjsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestWtRunLock:
    def test_no_wait_refuses_a_busy_runtime_with_exit_75_before_building_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        built: list[str] = []
        _wire(monkeypatch, object(), built)
        runs_dir = tmp_path / "runs"
        with _held_by_another_process("shared_rt", seconds=30):
            rc = cli.main([
                "run", "--runtime", "shared_rt", "--no-wait", "--runs-dir", str(runs_dir),
            ])

        err = capsys.readouterr().err
        assert rc == EXIT_RUNTIME_BUSY == 75
        assert built == []
        assert "runtime 'shared_rt' is in use" in err
        assert "running `wt run --label other`" in err
        finished = _events(runs_dir)[-1]
        assert (finished["event"], finished["status"], finished["exit_code"]) == (
            "sweep_finished",
            "busy",
            75,
        )

    def test_a_second_sweep_waits_for_the_first_and_then_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import windtunnel.cli as cli

        built: list[str] = []
        _wire(monkeypatch, object(), built)
        runs_dir = tmp_path / "runs"
        with _held_by_another_process("shared_rt", seconds=0.8):
            started = time.monotonic()
            rc = cli.main(["run", "--runtime", "shared_rt", "--runs-dir", str(runs_dir)])
            elapsed = time.monotonic() - started

        err = capsys.readouterr().err
        assert rc == 0
        assert built == ["shared_rt"]
        assert elapsed >= 0.4
        assert "waiting for it to finish — pass --no-wait to exit instead" in err
        kinds = [event["event"] for event in _events(runs_dir)]
        assert kinds[:3] == ["sweep_started", "lock_waiting", "lock_acquired"]
        assert kinds[-1] == "sweep_finished"

    def test_the_lock_is_held_through_pre_run_and_post_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        observed: list[bool] = []

        def _is_held() -> bool:
            try:
                with runtime_lock("shared_rt", wait=False, holder=holder_record()):
                    return False
            except RuntimeBusy:
                return True

        class Plugin:
            def pre_run(self, runtime: object, scenarios: list, runtime_name: str) -> None:
                observed.append(_is_held())

            def post_run(self, runtime: object, scenarios: list, runtime_name: str) -> None:
                observed.append(_is_held())

        _wire(monkeypatch, Plugin(), [])
        rc = cli.main(["run", "--runtime", "shared_rt", "--runs-dir", str(tmp_path / "runs")])

        assert rc == 0
        assert observed == [True, True]
        assert _is_held() is False

    def test_runtimes_without_a_concurrency_limit_are_never_locked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        class Unlimited:
            max_concurrency = None

        _wire(monkeypatch, Unlimited(), [])
        with _held_by_another_process("shared_rt", seconds=30):
            rc = cli.main([
                "run", "--runtime", "shared_rt", "--no-wait", "--runs-dir", str(tmp_path / "runs"),
            ])
        assert rc == 0

    def test_a_plugin_lock_key_scopes_the_lock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import windtunnel.cli as cli

        class EndpointKeyed:
            def lock_key(self, runtime_name: str) -> str:
                return f"{runtime_name}:endpoint-b"

        _wire(monkeypatch, EndpointKeyed(), [])
        with _held_by_another_process("shared_rt:endpoint-a", seconds=30):
            rc = cli.main([
                "run", "--runtime", "shared_rt", "--no-wait", "--runs-dir", str(tmp_path / "runs"),
            ])
        assert rc == 0

    def test_http_inject_keys_its_lock_by_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from windtunnel._cli.runtime_discovery import _HttpInjectPlugin
        from windtunnel._cli.scheduling import runtime_lock_key

        monkeypatch.setenv("WT_INJECT_URL", "http://agent.example:9000/")
        assert runtime_lock_key(_HttpInjectPlugin(), "http_inject") == (
            "http_inject:http://agent.example:9000"
        )

    def test_concurrent_in_process_sweeps_on_one_runtime_serialize(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two sweeps racing for the same runtime never overlap."""
        import windtunnel.cli as cli

        active: list[int] = []
        peak: list[int] = [0]
        lock = threading.Lock()

        class Plugin:
            def pre_run(self, runtime: object, scenarios: list, runtime_name: str) -> None:
                with lock:
                    active.append(1)
                    peak[0] = max(peak[0], len(active))
                time.sleep(0.2)

            def post_run(self, runtime: object, scenarios: list, runtime_name: str) -> None:
                with lock:
                    active.pop()

        _wire(monkeypatch, Plugin(), [])
        codes: list[int] = []

        def _sweep(label: str) -> None:
            codes.append(
                cli.main([
                    "run", "--runtime", "shared_rt", "--label", label,
                    "--runs-dir", str(tmp_path / "runs"),
                ])
            )

        threads = [threading.Thread(target=_sweep, args=(f"s{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert codes == [0, 0]
        assert peak[0] == 1
