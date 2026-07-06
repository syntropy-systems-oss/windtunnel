"""Terminus-2 runtime tests that stay green without Harbor installed."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from windtunnel.api.runner import _extract_reply
from windtunnel.spi.agent_runtime import AgentConfig


def _clear_harbor_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "harbor" or name.startswith("harbor."):
            monkeypatch.delitem(sys.modules, name, raising=False)


def _install_fake_harbor(
    monkeypatch: pytest.MonkeyPatch,
    terminus_cls: type,
) -> None:
    class FakeAgentContext:
        def __init__(self) -> None:
            self.metadata: dict[str, Any] | None = None

    harbor = types.ModuleType("harbor")
    harbor.__path__ = []  # type: ignore[attr-defined]
    agents = types.ModuleType("harbor.agents")
    agents.__path__ = []  # type: ignore[attr-defined]
    terminus = types.ModuleType("harbor.agents.terminus_2")
    terminus.Terminus2 = terminus_cls
    models = types.ModuleType("harbor.models")
    models.__path__ = []  # type: ignore[attr-defined]
    agent_models = types.ModuleType("harbor.models.agent")
    agent_models.__path__ = []  # type: ignore[attr-defined]
    context = types.ModuleType("harbor.models.agent.context")
    context.AgentContext = FakeAgentContext

    monkeypatch.setitem(sys.modules, "harbor", harbor)
    monkeypatch.setitem(sys.modules, "harbor.agents", agents)
    monkeypatch.setitem(sys.modules, "harbor.agents.terminus_2", terminus)
    monkeypatch.setitem(sys.modules, "harbor.models", models)
    monkeypatch.setitem(sys.modules, "harbor.models.agent", agent_models)
    monkeypatch.setitem(sys.modules, "harbor.models.agent.context", context)


def _message(response: dict[str, Any]) -> dict[str, Any]:
    return response["choices"][0]["message"]


class _FakeCommandRunner:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        args: list[str],
        *,
        timeout_sec: int | None = None,
        input_data: bytes | None = None,
    ) -> Any:
        self.calls.append(
            {
                "args": list(args),
                "timeout_sec": timeout_sec,
                "input_data": input_data,
            }
        )
        if self.results:
            return self.results.pop(0)
        import windtunnel.runtimes.terminus.runtime as runtime

        return runtime._ExecResult(stdout="", stderr="", return_code=0)


class _FakeUUID:
    hex = "abcdef1234567890"


def test_importing_terminus_runtime_does_not_import_harbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_harbor_modules(monkeypatch)

    importlib.import_module("windtunnel.runtimes.terminus")

    assert "harbor" not in sys.modules


def test_cli_selects_terminus_without_importing_harbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_harbor_modules(monkeypatch)
    import windtunnel.cli as cli

    plugin = cli._resolve_runtime_plugin("terminus")

    assert type(plugin).__name__ == "_TerminusPlugin"
    assert "harbor" not in sys.modules


def test_instantiating_without_harbor_raises_install_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    monkeypatch.setenv("WT_TERMINUS_MODEL", "openai/example-model")

    def fake_import_module(name: str) -> types.ModuleType:
        if name.startswith("harbor"):
            raise ImportError(name)
        return importlib.import_module(name)

    monkeypatch.setattr(runtime.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match=r"pip install windtunnel-bench\[terminus\].*Python >=3\.12"):
        runtime.TerminusRuntime()


def test_missing_model_env_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from windtunnel.runtimes.terminus import TerminusRuntimeConfig

    monkeypatch.delenv("WT_TERMINUS_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="WT_TERMINUS_MODEL is required"):
        TerminusRuntimeConfig.from_env()


def test_bad_max_turns_env_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from windtunnel.runtimes.terminus import TerminusRuntimeConfig

    monkeypatch.setenv("WT_TERMINUS_MODEL", "openai/example-model")
    monkeypatch.setenv("WT_TERMINUS_MAX_TURNS", "many")

    with pytest.raises(RuntimeError, match="WT_TERMINUS_MAX_TURNS must be a positive integer"):
        TerminusRuntimeConfig.from_env()


def test_isolation_env_defaults_to_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    from windtunnel.runtimes.terminus import TerminusRuntimeConfig

    monkeypatch.setenv("WT_TERMINUS_MODEL", "openai/example-model")
    monkeypatch.delenv("WT_TERMINUS_ISOLATION", raising=False)

    cfg = TerminusRuntimeConfig.from_env()

    assert cfg.isolation == "docker"
    assert cfg.docker_image == "python:3.12-slim"


def test_invalid_isolation_env_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from windtunnel.runtimes.terminus import TerminusRuntimeConfig

    monkeypatch.setenv("WT_TERMINUS_MODEL", "openai/example-model")
    monkeypatch.setenv("WT_TERMINUS_ISOLATION", "process")

    with pytest.raises(RuntimeError, match="WT_TERMINUS_ISOLATION must be 'docker' or 'host'"):
        TerminusRuntimeConfig.from_env()


def test_host_isolation_warns_at_provision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FakeTerminus2:
        pass

    _install_fake_harbor(monkeypatch, FakeTerminus2)
    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        logs_dir=tmp_path / "logs",
        isolation="host",
    )

    with pytest.warns(RuntimeWarning, match="executes model-generated commands directly"):
        runtime.TerminusRuntime(cfg).provision(AgentConfig())


def test_docker_isolation_without_docker_raises_precise_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FakeTerminus2:
        pass

    _install_fake_harbor(monkeypatch, FakeTerminus2)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None if name == "docker" else name)
    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        logs_dir=tmp_path / "logs",
        isolation="docker",
    )

    with pytest.raises(RuntimeError) as excinfo:
        runtime.TerminusRuntime(cfg).provision(AgentConfig())

    message = str(excinfo.value)
    assert "WT_TERMINUS_ISOLATION=docker requires Docker" in message
    assert "docker CLI was not found" in message
    assert "install Docker and start its daemon" in message
    assert "WT_TERMINUS_ISOLATION=host" in message


def test_docker_isolation_daemon_down_raises_precise_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FakeTerminus2:
        pass

    fake_runner = _FakeCommandRunner(
        [runtime._ExecResult(stderr="Cannot connect to Docker", return_code=1)]
    )
    _install_fake_harbor(monkeypatch, FakeTerminus2)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(runtime, "_new_command_runner", lambda: fake_runner)
    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        logs_dir=tmp_path / "logs",
        isolation="docker",
    )

    with pytest.raises(RuntimeError) as excinfo:
        runtime.TerminusRuntime(cfg).provision(AgentConfig())

    assert fake_runner.calls[0]["args"] == ["docker", "info"]
    message = str(excinfo.value)
    assert "WT_TERMINUS_ISOLATION=docker requires a running Docker daemon" in message
    assert "Cannot connect to Docker" in message
    assert "start Docker" in message
    assert "WT_TERMINUS_ISOLATION=host" in message


def test_workspace_reset_copies_template_and_wipes_changes(tmp_path: Path) -> None:
    from windtunnel.runtimes.terminus import TerminusWorkspaceManager

    template = tmp_path / "template"
    template.mkdir()
    (template / "seed.txt").write_text("seed\n", encoding="utf-8")
    manager = TerminusWorkspaceManager(template=template, logs_dir=tmp_path / "logs")

    workspace = manager.reset()
    assert (workspace / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    (workspace / "scratch.txt").write_text("dirty\n", encoding="utf-8")

    workspace = manager.reset()
    assert (workspace / "seed.txt").is_file()
    assert not (workspace / "scratch.txt").exists()

    workspace = manager.reset()
    assert (workspace / "seed.txt").is_file()

    run_dir = manager.run_dir
    manager.cleanup()
    assert not run_dir.exists()


def test_workspace_reset_creates_empty_workspace_when_template_unset(tmp_path: Path) -> None:
    from windtunnel.runtimes.terminus import TerminusWorkspaceManager

    manager = TerminusWorkspaceManager(template=None, logs_dir=tmp_path / "logs")
    workspace = manager.reset()

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []
    manager.cleanup()


def test_send_maps_fake_harbor_trajectory_to_openai_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FakeTerminus2:
        def __init__(self, *, logs_dir: Path, model_name: str, **kwargs: Any) -> None:
            self.logs_dir = Path(logs_dir)
            self.model_name = model_name
            self.kwargs = kwargs

        @staticmethod
        def name() -> str:
            return "terminus-2-fake"

        async def setup(self, environment: Any) -> None:
            self.environment = environment

        async def run(self, instruction: str, environment: Any, context: Any) -> None:
            (environment.workspace_dir / "answer.txt").write_text(
                instruction,
                encoding="utf-8",
            )
            trajectory = {
                "steps": [
                    {"source": "user", "message": instruction},
                    {
                        "source": "agent",
                        "message": "Plan: write the answer",
                        "tool_calls": [
                            {
                                "function_name": "bash_command",
                                "arguments": {
                                    "keystrokes": "printf 'done' > answer.txt\n",
                                    "duration": 1,
                                },
                            },
                            {
                                "function_name": "mark_task_complete",
                                "arguments": {},
                            },
                        ],
                        "observation": {
                            "results": [
                                {"content": "New Terminal Output:\ndone\n"},
                            ]
                        },
                    },
                ]
            }
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory),
                encoding="utf-8",
            )
            context.metadata = {"instruction": instruction}

    _install_fake_harbor(monkeypatch, FakeTerminus2)
    monkeypatch.setattr(runtime, "_ensure_tmux_available", lambda: None)

    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        max_turns=3,
        logs_dir=tmp_path / "logs",
        isolation="host",
    )
    with pytest.warns(RuntimeWarning, match="executes model-generated commands directly"):
        handle = runtime.TerminusRuntime(cfg).provision(AgentConfig())
    handle.reset_state()

    response = handle.send([{"role": "user", "content": "write done"}], "sid-1")

    content, tool_calls = _extract_reply(response)
    assert content == "New Terminal Output:\ndone\n"
    assert [call["function"]["name"] for call in tool_calls] == ["terminal"]
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "command": "printf 'done' > answer.txt\n"
    }
    assert _message(response)["role"] == "assistant"
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert (handle.workspace_dir / "answer.txt").read_text(encoding="utf-8") == "write done"
    handle.teardown()
    assert not handle.run_dir.exists()


def test_send_surfaces_fake_agent_error_as_worker_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FailingTerminus2:
        def __init__(self, *, logs_dir: Path, model_name: str, **kwargs: Any) -> None:
            self.logs_dir = Path(logs_dir)

        @staticmethod
        def name() -> str:
            return "terminus-2-failing"

        async def setup(self, environment: Any) -> None:
            return None

        async def run(self, instruction: str, environment: Any, context: Any) -> None:
            raise RuntimeError("agent exploded")

    _install_fake_harbor(monkeypatch, FailingTerminus2)
    monkeypatch.setattr(runtime, "_ensure_tmux_available", lambda: None)

    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        logs_dir=tmp_path / "logs",
        isolation="host",
    )
    with pytest.warns(RuntimeWarning, match="executes model-generated commands directly"):
        handle = runtime.TerminusRuntime(cfg).provision(AgentConfig())
    handle.reset_state()

    response = handle.send([{"role": "user", "content": "do work"}], "sid-err")

    content, tool_calls = _extract_reply(response)
    assert content == ""
    assert tool_calls == []
    assert "worker_warnings" in response
    assert "agent exploded" in response["worker_warnings"][0]
    handle.teardown()


def test_docker_lifecycle_and_exec_plumbing_uses_expected_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import windtunnel.runtimes.terminus.runtime as runtime

    class FakeTerminus2:
        @staticmethod
        def name() -> str:
            return "terminus-2-fake"

    fake_runner = _FakeCommandRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template"
    template.mkdir()
    _install_fake_harbor(monkeypatch, FakeTerminus2)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(runtime, "_new_command_runner", lambda: fake_runner)
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: _FakeUUID())

    cfg = runtime.TerminusRuntimeConfig(
        model="fake/model",
        logs_dir=tmp_path / "logs",
        workspace_template=template,
        isolation="docker",
        docker_image="python:3.12-slim",
        repo_mount=repo,
    )
    handle = runtime.TerminusRuntime(cfg).provision(AgentConfig())

    container = "wt-terminus-abcdef123456"
    home_setup = runtime._container_home_setup_command()
    assert fake_runner.calls[0]["args"] == ["docker", "info"]
    assert fake_runner.calls[1]["args"] == ["docker", "rm", "-f", container]
    run_args = fake_runner.calls[2]["args"]
    assert run_args == [
        "docker",
        "run",
        "--detach",
        "--name",
        container,
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/windtunnel-home",
        "--env",
        (
            "PATH=/workspace/.venv/bin:/usr/local/bin:/usr/local/sbin:"
            "/usr/bin:/usr/sbin:/bin:/sbin"
        ),
        "--env",
        "PYTHONPATH=/opt/windtunnel-src",
        "--env",
        "WT_REPO_ROOT=/opt/windtunnel-src",
        "--mount",
        f"type=bind,source={handle.workspace_dir.resolve()},target=/workspace",
        "--mount",
        f"type=bind,source={(handle.run_dir / 'container-logs').resolve()},target=/logs",
        "--mount",
        f"type=bind,source={repo.resolve()},target=/opt/windtunnel-src,readonly",
        "--pull=missing",
        "python:3.12-slim",
        "sleep",
        "infinity",
    ]
    assert fake_runner.calls[3]["args"] == [
        "docker",
        "exec",
        "--user",
        "root",
        container,
        "bash",
        "-c",
        home_setup,
    ]

    env = handle._new_environment(handle.run_dir / "agent-logs" / "manual", "sid")  # noqa: SLF001
    runtime._run_async(env.exec("printf ok > /workspace/out.txt"))
    exec_args = fake_runner.calls[-1]["args"]
    assert exec_args == [
        "docker",
        "exec",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/windtunnel-home",
        "--env",
        (
            "PATH=/workspace/.venv/bin:/usr/local/bin:/usr/local/sbin:"
            "/usr/bin:/usr/sbin:/bin:/sbin"
        ),
        "--env",
        "PYTHONPATH=/opt/windtunnel-src",
        "--env",
        "WT_REPO_ROOT=/opt/windtunnel-src",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        container,
        "bash",
        "-c",
        "printf ok > /workspace/out.txt",
    ]

    handle._current_environment = env  # noqa: SLF001
    handle.reset_state()
    reset_tail = [call["args"] for call in fake_runner.calls[-4:]]
    assert reset_tail[0][-3:] == [
        "bash",
        "-c",
        "tmux kill-session -t terminus-2-fake",
    ]
    assert reset_tail[1] == ["docker", "rm", "-f", container]
    assert reset_tail[2][0:5] == ["docker", "run", "--detach", "--name", container]
    assert reset_tail[3] == [
        "docker",
        "exec",
        "--user",
        "root",
        container,
        "bash",
        "-c",
        home_setup,
    ]

    handle.teardown()
    assert fake_runner.calls[-1]["args"] == ["docker", "rm", "-f", container]
    assert not handle.run_dir.exists()


@pytest.mark.integration
def test_real_harbor_docker_hello_world_run(tmp_path: Path) -> None:
    if importlib.util.find_spec("harbor") is None:
        pytest.skip("Harbor is not installed")
    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    docker_info = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if docker_info.returncode != 0:
        pytest.skip("Docker daemon is not running")
    if not os.environ.get("WT_TERMINUS_MODEL"):
        pytest.skip("WT_TERMINUS_MODEL is not set")

    from windtunnel.runtimes.terminus import TerminusRuntime, TerminusRuntimeConfig

    template = tmp_path / "template"
    template.mkdir()
    cfg = TerminusRuntimeConfig.from_env()
    cfg = TerminusRuntimeConfig(
        model=cfg.model,
        api_base=cfg.api_base,
        max_turns=min(cfg.max_turns, 10),
        workspace_template=template,
        logs_dir=tmp_path / "logs",
        isolation="docker",
        docker_image=cfg.docker_image,
        repo_mount=cfg.repo_mount,
    )
    handle = TerminusRuntime(cfg).provision(AgentConfig())
    response = handle.send(
        [
            {
                "role": "user",
                "content": (
                    "Create a file named hello.txt in the current directory containing "
                    "exactly hello-world, then mark the task complete."
                ),
            }
        ],
        "integration-hello",
    )

    assert not response.get("worker_warnings")
    assert (handle.workspace_dir / "hello.txt").read_text(encoding="utf-8").strip() == "hello-world"
    handle.teardown()
