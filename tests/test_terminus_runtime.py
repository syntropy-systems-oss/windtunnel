"""Terminus-2 runtime tests that stay green without Harbor installed."""
from __future__ import annotations

import importlib
import json
import os
import shutil
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
    )
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

    cfg = runtime.TerminusRuntimeConfig(model="fake/model", logs_dir=tmp_path / "logs")
    handle = runtime.TerminusRuntime(cfg).provision(AgentConfig())
    handle.reset_state()

    response = handle.send([{"role": "user", "content": "do work"}], "sid-err")

    content, tool_calls = _extract_reply(response)
    assert content == ""
    assert tool_calls == []
    assert "worker_warnings" in response
    assert "agent exploded" in response["worker_warnings"][0]
    handle.teardown()


@pytest.mark.integration
def test_real_harbor_hello_world_run(tmp_path: Path) -> None:
    if importlib.util.find_spec("harbor") is None:
        pytest.skip("Harbor is not installed")
    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
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
    )
    handle = TerminusRuntime(cfg).provision(AgentConfig())
    handle.reset_state()
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
