"""TerminusRuntime — direct Harbor Terminus-2 driver.

Architecture seam: (a) this runtime drives ``harbor.agents.terminus_2.Terminus2``
directly against a minimal workspace-backed terminal environment instead of
generating a Harbor task and running the trial machinery. Docker isolation is
the default environment; explicit ``WT_TERMINUS_ISOLATION=host`` keeps the
original host-tmux spike path for local debugging. Reading Harbor 0.17.1 showed
that ``Trial`` owns task download, Docker environment construction, artifact
collection, verifier execution, and result persistence; Wind Tunnel needs none
of Harbor's verifier/reward stack because scenarios do their own scoring.
``Terminus2`` itself only requires an environment object with a tmux session
surface and an ``AgentContext`` sink, so the direct seam has fewer moving parts
and no ``harbor run`` subprocess.

``provision(config, mcps)`` intentionally ignores ``mcps``. Terminus-2 exposes a
single terminal tool, not a dynamic MCP tool bus, so Wind Tunnel scenarios for
this runtime should score outcomes from the workspace and use trajectory
evidence from terminal commands rather than witnessed MCP calls.

``send(messages, session_id)`` is one coarse turn: it extracts the newest user
message as the task instruction, runs Terminus-2 to completion in the current
workspace, and returns an OpenAI-shaped response. Harbor's ATIF trajectory
records parsed terminal actions as ``bash_command`` tool calls with
``arguments.keystrokes``. The runtime maps those, in order, to OpenAI function
calls named ``terminal`` with ``{"command": <raw keystrokes>}`` so Wind Tunnel's
``must_call`` trajectory layer has real terminal evidence. Harbor's
``mark_task_complete`` pseudo-call is not mapped because it is not a terminal
command.

All Harbor imports are lazy and happen only after this runtime is selected.
The rest of Wind Tunnel remains importable and testable on Python 3.11 without
the optional Harbor dependency.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import shlex
import shutil
import tempfile
import threading
import uuid
import warnings
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, Message, Response

DEFAULT_MAX_TURNS = 80
DEFAULT_ISOLATION = "docker"
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
CONTAINER_WORKSPACE_DIR = "/workspace"
CONTAINER_LOGS_DIR = "/logs"
CONTAINER_REPO_DIR = "/opt/windtunnel-src"
CONTAINER_HOME_DIR = "/tmp/windtunnel-home"
CONTAINER_ENV = {
    "HOME": CONTAINER_HOME_DIR,
    "PATH": (
        "/workspace/.venv/bin:"
        "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"
    ),
    "PYTHONPATH": CONTAINER_REPO_DIR,
    "WT_REPO_ROOT": CONTAINER_REPO_DIR,
}
DOCKER_BOOTSTRAP_HOOK = Path(".windtunnel") / "terminus-bootstrap.sh"
DOCKER_RUNTIME_METADATA = Path(".windtunnel") / "terminus-runtime.json"
HARBOR_INSTALL_REMEDY = (
    "Terminus runtime requires Harbor. Install it with "
    "`pip install windtunnel-bench[terminus]` on Python >=3.12."
)
HOST_ISOLATION_WARNING = (
    "WT_TERMINUS_ISOLATION=host executes model-generated commands directly "
    "on this machine."
)


@dataclass(frozen=True)
class TerminusRuntimeConfig:
    """Env-derived configuration for the Terminus-2 runtime."""

    model: str
    api_base: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    workspace_template: Path | None = None
    logs_dir: Path = Path(tempfile.gettempdir()) / "windtunnel-terminus"
    isolation: str = DEFAULT_ISOLATION
    docker_image: str = DEFAULT_DOCKER_IMAGE
    repo_mount: Path = field(default_factory=lambda: _repo_root())

    def __post_init__(self) -> None:
        object.__setattr__(self, "isolation", _parse_isolation(self.isolation))
        object.__setattr__(self, "repo_mount", Path(self.repo_mount).expanduser())

    @classmethod
    def from_env(cls) -> TerminusRuntimeConfig:
        model = os.environ.get("WT_TERMINUS_MODEL")
        if not model:
            raise RuntimeError(
                "WT_TERMINUS_MODEL is required for terminus runtime "
                "(LiteLLM model string, e.g. 'openai/<model>')."
            )

        max_turns = _parse_positive_int(
            "WT_TERMINUS_MAX_TURNS",
            os.environ.get("WT_TERMINUS_MAX_TURNS"),
            DEFAULT_MAX_TURNS,
        )

        template = _optional_dir_from_env("WT_TERMINUS_WORKSPACE_TEMPLATE")
        isolation = _parse_isolation(os.environ.get("WT_TERMINUS_ISOLATION"))
        repo_mount = _optional_dir_from_env("WT_TERMINUS_REPO_MOUNT") or _repo_root()
        logs_dir = Path(
            os.environ.get("WT_TERMINUS_LOGS_DIR")
            or (Path(tempfile.gettempdir()) / "windtunnel-terminus")
        ).expanduser()

        return cls(
            model=model,
            api_base=os.environ.get("WT_TERMINUS_API_BASE") or None,
            max_turns=max_turns,
            workspace_template=template,
            logs_dir=logs_dir,
            isolation=isolation,
            docker_image=os.environ.get("WT_TERMINUS_IMAGE") or DEFAULT_DOCKER_IMAGE,
            repo_mount=repo_mount,
        )


class TerminusWorkspaceManager:
    """Materialize a fresh per-run workspace from an optional template."""

    def __init__(self, template: Path | None, logs_dir: Path) -> None:
        self.template = template
        self.logs_dir = logs_dir.expanduser()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(tempfile.mkdtemp(prefix="wt-terminus-", dir=self.logs_dir))
        self.workspace_dir = self.run_dir / "workspace"
        self.agent_logs_root = self.run_dir / "agent-logs"
        self._counter = 0

    def reset(self) -> Path:
        """Synchronously wipe and re-materialize the workspace."""
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)
        if self.template is None:
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copytree(self.template, self.workspace_dir, symlinks=True)
        self.agent_logs_root.mkdir(parents=True, exist_ok=True)
        return self.workspace_dir

    def new_agent_logs_dir(self, session_id: str) -> Path:
        self._counter += 1
        safe_session = "".join(
            char if char.isalnum() or char in "-._" else "_"
            for char in session_id
        )[:80]
        if not safe_session:
            safe_session = uuid.uuid4().hex
        path = self.agent_logs_root / f"{self._counter:04d}-{safe_session}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.run_dir, ignore_errors=True)


@dataclass
class _ExecResult:
    stdout: str | None = None
    stderr: str | None = None
    return_code: int = 0


class _SubprocessCommandRunner:
    async def run(
        self,
        args: list[str],
        *,
        timeout_sec: int | None = None,
        input_data: bytes | None = None,
    ) -> _ExecResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE
            if input_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            if timeout_sec:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(input=input_data),
                    timeout=timeout_sec,
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate(input=input_data)
        except TimeoutError:
            process.kill()
            await process.wait()
            return _ExecResult(
                stdout=None,
                stderr=f"Command timed out after {timeout_sec} seconds",
                return_code=124,
            )

        return _ExecResult(
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else None,
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else None,
            return_code=process.returncode or 0,
        )


def _new_command_runner() -> _SubprocessCommandRunner:
    return _SubprocessCommandRunner()


@dataclass(frozen=True)
class _TrialPaths:
    trial_dir: Path

    @property
    def agent_dir(self) -> Path:
        return self.trial_dir / "agent"

    @property
    def verifier_dir(self) -> Path:
        return self.trial_dir / "verifier"

    @property
    def artifacts_dir(self) -> Path:
        return self.trial_dir / "artifacts"

    def mkdir(self) -> None:
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.verifier_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


class _LocalWorkspaceEnvironment:
    """Small Harbor-compatible environment backed by a host workspace."""

    def __init__(self, workspace_dir: Path, logs_dir: Path, session_id: str) -> None:
        self.workspace_dir = workspace_dir
        self.session_id = session_id
        self.default_user: str | int | None = None
        self.trial_paths = _TrialPaths(logs_dir)
        self.trial_paths.mkdir()
        self._env_overlays: list[dict[str, str]] = []

    @contextlib.contextmanager
    def with_default_user(self, user: str | int | None) -> Generator[None, None, None]:
        previous = self.default_user
        self.default_user = user
        try:
            yield
        finally:
            self.default_user = previous

    @contextlib.contextmanager
    def scoped_exec_env(self, env: dict[str, str]) -> Generator[None, None, None]:
        if not env:
            yield
            return
        self._env_overlays.append(dict(env))
        try:
            yield
        finally:
            self._env_overlays.pop()

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> _ExecResult:
        del user  # Local execution runs as the current bench user.
        mapped_command = self._map_command_paths(command)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        for overlay in self._env_overlays:
            merged_env.update(overlay)
        run_cwd = self._map_path(cwd) if cwd else self.workspace_dir

        process = await asyncio.create_subprocess_shell(
            mapped_command,
            cwd=str(run_cwd),
            env=merged_env,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            if timeout_sec:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_sec
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            return _ExecResult(
                stdout=None,
                stderr=f"Command timed out after {timeout_sec} seconds",
                return_code=124,
            )

        return _ExecResult(
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else None,
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else None,
            return_code=process.returncode or 0,
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        target = self._map_path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        target = self._map_path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_dir, target, symlinks=True)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._map_path(source_path), target)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self._map_path(source_dir), target, symlinks=True)

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        del user
        return self._map_path(path).is_dir()

    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        del user
        return self._map_path(path).is_file()

    async def kill_tmux_session(self, name: str) -> None:
        await self.exec(f"tmux kill-session -t {name}")

    def _map_command_paths(self, command: str) -> str:
        return (
            command.replace("/logs/agent", str(self.trial_paths.agent_dir))
            .replace("/logs/verifier", str(self.trial_paths.verifier_dir))
            .replace("/logs/artifacts", str(self.trial_paths.artifacts_dir))
        )

    def _map_path(self, raw_path: str | Path | None) -> Path:
        if raw_path is None:
            return self.workspace_dir
        path_text = str(raw_path)
        replacements = {
            "/logs/agent": self.trial_paths.agent_dir,
            "/logs/verifier": self.trial_paths.verifier_dir,
            "/logs/artifacts": self.trial_paths.artifacts_dir,
            "/workspace": self.workspace_dir,
        }
        for prefix, replacement in replacements.items():
            if path_text == prefix:
                return replacement
            if path_text.startswith(prefix + "/"):
                return replacement / path_text.removeprefix(prefix + "/")
        path = Path(path_text).expanduser()
        if path.is_absolute():
            return path
        return self.workspace_dir / path


class _DockerWorkspaceEnvironment:
    """Small Harbor-compatible environment backed by one Docker container."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        logs_dir: Path,
        session_id: str,
        container_name: str,
        command_runner: Any,
        repo_mount: Path,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.session_id = session_id
        self.container_name = container_name
        self.default_user: str | int | None = _host_container_user()
        self.trial_paths = _TrialPaths(logs_dir)
        self.trial_paths.mkdir()
        self._command_runner = command_runner
        self._repo_mount = repo_mount
        self._env_overlays: list[dict[str, str]] = []

    @contextlib.contextmanager
    def with_default_user(self, user: str | int | None) -> Generator[None, None, None]:
        previous = self.default_user
        self.default_user = user
        try:
            yield
        finally:
            self.default_user = previous

    @contextlib.contextmanager
    def scoped_exec_env(self, env: dict[str, str]) -> Generator[None, None, None]:
        if not env:
            yield
            return
        self._env_overlays.append(dict(env))
        try:
            yield
        finally:
            self._env_overlays.pop()

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> _ExecResult:
        exec_env = dict(CONTAINER_ENV)
        for overlay in self._env_overlays:
            exec_env.update(overlay)
        if env:
            exec_env.update(env)

        args = ["docker", "exec"]
        run_cwd = self._container_path(cwd) if cwd else CONTAINER_WORKSPACE_DIR
        if run_cwd:
            args.extend(["--workdir", run_cwd])
        for key in sorted(exec_env):
            args.extend(["--env", f"{key}={exec_env[key]}"])
        resolved_user = self._resolve_user(user)
        if resolved_user is not None:
            args.extend(["--user", str(resolved_user)])
        args.extend(
            [
                self.container_name,
                "bash",
                "-c",
                self._container_command(command),
            ]
        )
        return cast(_ExecResult, await self._command_runner.run(args, timeout_sec=timeout_sec))

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        host_target = self._host_path_for_container_path(target_path)
        if host_target is not None:
            host_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, host_target)
            return

        container_target = self._container_path(target_path)
        await self.exec(
            f"mkdir -p {shlex.quote(str(Path(container_target).parent))}",
            user="root",
        )
        result = await self._command_runner.run(
            ["docker", "cp", str(source_path), f"{self.container_name}:{container_target}"]
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker cp upload", result))

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        host_target = self._host_path_for_container_path(target_dir)
        if host_target is not None:
            if host_target.exists():
                shutil.rmtree(host_target)
            shutil.copytree(source_dir, host_target, symlinks=True)
            return

        container_target = self._container_path(target_dir)
        await self.exec(
            f"rm -rf {shlex.quote(container_target)} && mkdir -p {shlex.quote(container_target)}",
            user="root",
        )
        result = await self._command_runner.run(
            ["docker", "cp", f"{source_dir}/.", f"{self.container_name}:{container_target}"]
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker cp upload", result))

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        host_source = self._host_path_for_container_path(source_path)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if host_source is not None:
            shutil.copy2(host_source, target)
            return

        result = await self._command_runner.run(
            ["docker", "cp", f"{self.container_name}:{source_path}", str(target)]
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker cp download", result))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        host_source = self._host_path_for_container_path(source_dir)
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        if host_source is not None:
            shutil.copytree(host_source, target, symlinks=True)
            return

        target.mkdir(parents=True, exist_ok=True)
        result = await self._command_runner.run(
            ["docker", "cp", f"{self.container_name}:{source_dir}/.", str(target)]
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker cp download", result))

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        result = await self.exec(
            f"test -d {shlex.quote(self._container_path(path))}",
            user=user,
            timeout_sec=10,
        )
        return result.return_code == 0

    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        result = await self.exec(
            f"test -f {shlex.quote(self._container_path(path))}",
            user=user,
            timeout_sec=10,
        )
        return result.return_code == 0

    async def kill_tmux_session(self, name: str) -> None:
        await self.exec(f"tmux kill-session -t {shlex.quote(name)}")

    def _resolve_user(self, user: str | int | None) -> str | int | None:
        if user is not None:
            return user
        return self.default_user

    def _container_command(self, command: str) -> str:
        replacements = {
            str(self.workspace_dir): CONTAINER_WORKSPACE_DIR,
            str(self.trial_paths.agent_dir): f"{CONTAINER_LOGS_DIR}/agent",
            str(self.trial_paths.verifier_dir): f"{CONTAINER_LOGS_DIR}/verifier",
            str(self.trial_paths.artifacts_dir): f"{CONTAINER_LOGS_DIR}/artifacts",
            str(self._repo_mount): CONTAINER_REPO_DIR,
        }
        mapped = command
        for host_path, container_path in replacements.items():
            mapped = mapped.replace(host_path, container_path)
        return mapped

    def _container_path(self, raw_path: str | Path | None) -> str:
        if raw_path is None:
            return CONTAINER_WORKSPACE_DIR
        path_text = str(raw_path)
        replacements = {
            str(self.workspace_dir): CONTAINER_WORKSPACE_DIR,
            str(self.trial_paths.agent_dir): f"{CONTAINER_LOGS_DIR}/agent",
            str(self.trial_paths.verifier_dir): f"{CONTAINER_LOGS_DIR}/verifier",
            str(self.trial_paths.artifacts_dir): f"{CONTAINER_LOGS_DIR}/artifacts",
            str(self._repo_mount): CONTAINER_REPO_DIR,
            CONTAINER_WORKSPACE_DIR: CONTAINER_WORKSPACE_DIR,
            f"{CONTAINER_LOGS_DIR}/agent": f"{CONTAINER_LOGS_DIR}/agent",
            f"{CONTAINER_LOGS_DIR}/verifier": f"{CONTAINER_LOGS_DIR}/verifier",
            f"{CONTAINER_LOGS_DIR}/artifacts": f"{CONTAINER_LOGS_DIR}/artifacts",
        }
        for prefix, replacement in replacements.items():
            if path_text == prefix:
                return replacement
            if path_text.startswith(prefix + "/"):
                return replacement + "/" + path_text.removeprefix(prefix + "/")
        path = Path(path_text)
        if path.is_absolute():
            return path_text
        return f"{CONTAINER_WORKSPACE_DIR}/{path_text}"

    def _host_path_for_container_path(self, raw_path: str | Path) -> Path | None:
        path_text = str(raw_path)
        mappings = {
            CONTAINER_WORKSPACE_DIR: self.workspace_dir,
            f"{CONTAINER_LOGS_DIR}/agent": self.trial_paths.agent_dir,
            f"{CONTAINER_LOGS_DIR}/verifier": self.trial_paths.verifier_dir,
            f"{CONTAINER_LOGS_DIR}/artifacts": self.trial_paths.artifacts_dir,
        }
        for prefix, replacement in mappings.items():
            if path_text == prefix:
                return replacement
            if path_text.startswith(prefix + "/"):
                return replacement / path_text.removeprefix(prefix + "/")
        return None


@dataclass(frozen=True)
class _HarborSymbols:
    Terminus2: type
    AgentContext: type


class TerminusRuntime:
    """AgentRuntime backed by Harbor's Terminus-2 terminal agent."""

    accepts_runner_managed_mcps = False

    def __init__(self, config: TerminusRuntimeConfig | None = None) -> None:
        self.config = config or TerminusRuntimeConfig.from_env()
        self._harbor = _load_harbor_symbols()
        self._command_runner = _new_command_runner()
        self.provisions: list[tuple[AgentConfig, _TerminusHandle]] = []

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> AgentHandle:
        # mcps: ignored — Terminus-2 has one terminal tool, not mountable MCPs.
        del mcps
        if self.config.isolation == "host":
            warnings.warn(HOST_ISOLATION_WARNING, RuntimeWarning, stacklevel=2)
        else:
            _ensure_docker_available(self._command_runner)

        manager = TerminusWorkspaceManager(
            template=self.config.workspace_template,
            logs_dir=self.config.logs_dir,
        )
        handle = _TerminusHandle(
            runtime_config=self.config,
            agent_config=config,
            harbor=self._harbor,
            workspace=manager,
            command_runner=self._command_runner,
        )
        if self.config.isolation == "docker":
            handle.reset_state()
        self.provisions.append((config, handle))
        return handle


class _TerminusHandle:
    # Terminus receives one coarse instruction per trial, not an OpenAI message
    # history. Refuse history-shaped perturbations rather than scoring an unseen
    # counterfactual and marking experiment integrity green.
    _windtunnel_consumes_full_history = False

    def __init__(
        self,
        *,
        runtime_config: TerminusRuntimeConfig,
        agent_config: AgentConfig,
        harbor: _HarborSymbols,
        workspace: TerminusWorkspaceManager,
        command_runner: Any,
    ) -> None:
        self._runtime_config = runtime_config
        self._agent_config = agent_config
        self._harbor = harbor
        self._workspace = workspace
        self._command_runner = command_runner
        self._container_name = _new_container_name()
        self._container_logs_dir = self._workspace.run_dir / "container-logs"
        self._container_running = False
        self._workspace_ready = False
        self._current_environment: _LocalWorkspaceEnvironment | _DockerWorkspaceEnvironment | None = None
        self._current_agent: Any = None
        self._teardown = False

    @property
    def workspace_dir(self) -> Path:
        return self._workspace.workspace_dir

    @property
    def workspace_template(self) -> Path | None:
        return self._workspace.template

    @property
    def run_dir(self) -> Path:
        return self._workspace.run_dir

    def send(self, messages: list[Message], session_id: str) -> Response:
        instruction = _newest_user_text(messages)
        if not self._workspace_ready:
            self.reset_state()

        agent_logs_dir = self._workspace.new_agent_logs_dir(session_id)
        environment = self._new_environment(agent_logs_dir, session_id)
        self._current_environment = environment

        try:
            if self._runtime_config.isolation == "host":
                _ensure_tmux_available()
            _run_async(environment.kill_tmux_session(_terminus_session_name(self._harbor.Terminus2)))
            agent = self._new_agent(agent_logs_dir, session_id)
            self._current_agent = agent
            context = self._harbor.AgentContext()
            _run_async(agent.setup(environment=environment))
            _run_async(
                agent.run(
                    instruction=instruction,
                    environment=environment,
                    context=context,
                )
            )
            trajectory = _load_trajectory(agent_logs_dir)
            tool_calls = _openai_tool_calls(_terminal_commands_from_trajectory(trajectory))
            content = _final_content_from_trajectory(trajectory)
            return _to_response(content=content, tool_calls=tool_calls)
        except Exception as exc:  # noqa: BLE001 - report agent/runtime run errors in trace
            trajectory = _load_trajectory(agent_logs_dir)
            tool_calls = _openai_tool_calls(_terminal_commands_from_trajectory(trajectory))
            return _to_response(
                content="",
                tool_calls=tool_calls,
                worker_warnings=[f"terminus run error: {type(exc).__name__}: {exc}"],
            )
        finally:
            try:
                _run_async(
                    environment.kill_tmux_session(
                        _terminus_session_name(self._harbor.Terminus2)
                    )
                )
            except Exception:
                pass
            self._current_agent = None

    def reset_state(self) -> None:
        self._stop_current_session()
        self._workspace.reset()
        if self._runtime_config.isolation == "docker":
            self._recreate_container()
        self._workspace_ready = True

    def teardown(self) -> None:
        if self._teardown:
            return
        self._teardown = True
        try:
            self._stop_current_session()
        except Exception:
            pass
        if self._runtime_config.isolation == "docker":
            self._remove_container()
        try:
            self._workspace.cleanup()
        except Exception:
            pass

    def _new_environment(
        self,
        agent_logs_dir: Path,
        session_id: str,
    ) -> _LocalWorkspaceEnvironment | _DockerWorkspaceEnvironment:
        if self._runtime_config.isolation == "host":
            return _LocalWorkspaceEnvironment(
                self._workspace.workspace_dir,
                agent_logs_dir,
                session_id,
            )
        if not self._container_running:
            self._recreate_container()
        self._prepare_container_log_mounts(agent_logs_dir)
        return _DockerWorkspaceEnvironment(
            workspace_dir=self._workspace.workspace_dir,
            logs_dir=agent_logs_dir,
            session_id=session_id,
            container_name=self._container_name,
            command_runner=self._command_runner,
            repo_mount=self._runtime_config.repo_mount,
        )

    def _recreate_container(self) -> None:
        self._remove_container()
        self._container_logs_dir.mkdir(parents=True, exist_ok=True)
        self._write_runtime_metadata()
        args = [
            "docker",
            "run",
            "--detach",
            "--name",
            self._container_name,
            "--workdir",
            CONTAINER_WORKSPACE_DIR,
        ]
        for key in sorted(CONTAINER_ENV):
            args.extend(["--env", f"{key}={CONTAINER_ENV[key]}"])
        args.extend(
            [
                "--mount",
                _bind_mount_arg(self._workspace.workspace_dir, CONTAINER_WORKSPACE_DIR),
                "--mount",
                _bind_mount_arg(self._container_logs_dir, CONTAINER_LOGS_DIR),
                "--mount",
                _bind_mount_arg(
                    self._runtime_config.repo_mount,
                    CONTAINER_REPO_DIR,
                    readonly=True,
                ),
                "--pull=missing",
                self._runtime_config.docker_image,
                "sleep",
                "infinity",
            ]
        )
        result = _run_async(self._command_runner.run(args, timeout_sec=120))
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker run", result))
        self._container_running = True
        self._container_root_exec(_container_home_setup_command())
        self._run_docker_bootstrap_hook()

    def _remove_container(self) -> None:
        result = _run_async(
            self._command_runner.run(
                ["docker", "rm", "-f", self._container_name],
                timeout_sec=30,
            )
        )
        del result
        self._container_running = False

    def _container_root_exec(self, command: str, timeout_sec: int | None = 60) -> _ExecResult:
        args = [
            "docker",
            "exec",
            "--user",
            "root",
            self._container_name,
            "bash",
            "-c",
            command,
        ]
        result = cast(
            _ExecResult,
            _run_async(self._command_runner.run(args, timeout_sec=timeout_sec)),
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("docker exec", result))
        return result

    def _run_docker_bootstrap_hook(self) -> None:
        hook = self._workspace.workspace_dir / DOCKER_BOOTSTRAP_HOOK
        if not hook.is_file():
            return
        environment = _DockerWorkspaceEnvironment(
            workspace_dir=self._workspace.workspace_dir,
            logs_dir=self._workspace.run_dir / "bootstrap-logs",
            session_id="bootstrap",
            container_name=self._container_name,
            command_runner=self._command_runner,
            repo_mount=self._runtime_config.repo_mount,
        )
        result = _run_async(
            environment.exec(
                f"bash {shlex.quote(str(Path(CONTAINER_WORKSPACE_DIR) / DOCKER_BOOTSTRAP_HOOK))}",
                cwd=CONTAINER_WORKSPACE_DIR,
                timeout_sec=300,
            )
        )
        if result.return_code != 0:
            raise RuntimeError(_command_failure("terminus docker bootstrap hook", result))

    def _prepare_container_log_mounts(self, agent_logs_dir: Path) -> None:
        trial_paths = _TrialPaths(agent_logs_dir)
        trial_paths.mkdir()
        self._container_logs_dir.mkdir(parents=True, exist_ok=True)
        links = {
            "agent": trial_paths.agent_dir,
            "verifier": trial_paths.verifier_dir,
            "artifacts": trial_paths.artifacts_dir,
        }
        for name, target in links.items():
            link = self._container_logs_dir / name
            if link.is_symlink() or link.is_file():
                link.unlink()
            elif link.exists():
                shutil.rmtree(link)
            link.symlink_to(os.path.relpath(target, start=self._container_logs_dir))

    def _write_runtime_metadata(self) -> None:
        metadata_path = self._workspace.workspace_dir / DOCKER_RUNTIME_METADATA
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "isolation": "docker",
                    "container_name": self._container_name,
                    "workspace": CONTAINER_WORKSPACE_DIR,
                    "image": self._runtime_config.docker_image,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _new_agent(self, logs_dir: Path, session_id: str) -> Any:
        sampling = self._agent_config.sampling
        temperature = sampling.temperature if sampling is not None else None
        return self._harbor.Terminus2(
            logs_dir=logs_dir,
            model_name=self._runtime_config.model,
            max_turns=self._runtime_config.max_turns,
            parser_name="json",
            api_base=self._runtime_config.api_base,
            temperature=temperature,
            trajectory_config={"raw_content": False, "linear_history": True},
            record_terminal_session=False,
            suppress_max_turns_warning=True,
            session_id=session_id,
        )

    def _stop_current_session(self) -> None:
        environment = self._current_environment
        if environment is None:
            return
        try:
            _run_async(
                environment.kill_tmux_session(
                    _terminus_session_name(self._harbor.Terminus2)
                )
            )
        finally:
            self._current_environment = None


def _parse_positive_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _parse_isolation(raw: str | None) -> str:
    value = (raw or DEFAULT_ISOLATION).strip().lower()
    if value not in {"docker", "host"}:
        raise RuntimeError(
            "WT_TERMINUS_ISOLATION must be 'docker' or 'host', "
            f"got {raw!r}"
        )
    return value


def _optional_dir_from_env(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise RuntimeError(f"{name} must point to a directory, got {raw!r}")
    return path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _new_container_name() -> str:
    return f"wt-terminus-{uuid.uuid4().hex[:12]}"


def _host_container_user() -> str | None:
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return f"{os.getuid()}:{os.getgid()}"
    return None


def _bind_mount_arg(source: Path, target: str, *, readonly: bool = False) -> str:
    parts = [
        "type=bind",
        f"source={source.resolve()}",
        f"target={target}",
    ]
    if readonly:
        parts.append("readonly")
    return ",".join(parts)


def _container_home_setup_command() -> str:
    exports = "".join(
        f"export {key}={shlex.quote(value)}\n"
        for key, value in sorted(CONTAINER_ENV.items())
        if key != "HOME"
    )
    quoted_home = shlex.quote(CONTAINER_HOME_DIR)
    quoted_profile = shlex.quote(f"{CONTAINER_HOME_DIR}/.bash_profile")
    return (
        f"mkdir -p {quoted_home} && chmod 777 {quoted_home} && "
        f"printf %s {shlex.quote(exports)} > {quoted_profile} && "
        f"chmod 644 {quoted_profile}"
    )


def _ensure_docker_available(command_runner: Any) -> None:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "WT_TERMINUS_ISOLATION=docker requires Docker, but the docker CLI "
            "was not found on PATH. Remedies: install Docker and start its "
            "daemon, or set WT_TERMINUS_ISOLATION=host to explicitly run "
            "model-generated commands on this machine."
        )

    result = _run_async(command_runner.run(["docker", "info"], timeout_sec=10))
    if result.return_code != 0:
        detail = _tail_text(result.stderr or result.stdout or "").strip()
        suffix = f" Details: {detail}" if detail else ""
        raise RuntimeError(
            "WT_TERMINUS_ISOLATION=docker requires a running Docker daemon, "
            f"but `docker info` failed.{suffix} Remedies: start Docker, or "
            "set WT_TERMINUS_ISOLATION=host to explicitly run model-generated "
            "commands on this machine."
        )


def _command_failure(label: str, result: _ExecResult) -> str:
    stderr = _tail_text(result.stderr or "")
    stdout = _tail_text(result.stdout or "")
    return (
        f"{label} failed with exit code {result.return_code}. "
        f"stdout: {stdout!r}. stderr: {stderr!r}."
    )


def _tail_text(text: str, limit: int = 2000) -> str:
    return text[-limit:]


def _load_harbor_symbols() -> _HarborSymbols:
    try:
        terminus_module = importlib.import_module("harbor.agents.terminus_2")
        context_module = importlib.import_module("harbor.models.agent.context")
    except ImportError as exc:
        raise RuntimeError(HARBOR_INSTALL_REMEDY) from exc
    return _HarborSymbols(
        Terminus2=getattr(terminus_module, "Terminus2"),
        AgentContext=getattr(context_module, "AgentContext"),
    )


def _newest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, str):
                raise RuntimeError(
                    "terminus send: newest user message content must be a string, "
                    f"got {type(content).__name__}"
                )
            return content
    raise RuntimeError("terminus send: no user message found")


def _terminus_session_name(terminus_cls: type) -> str:
    name = getattr(terminus_cls, "name", None)
    if callable(name):
        try:
            resolved = name()
            if isinstance(resolved, str) and resolved:
                return resolved
        except Exception:
            pass
    return "terminus-2"


def _ensure_tmux_available() -> None:
    if shutil.which("tmux") is None:
        raise RuntimeError(
            "terminus runtime requires tmux on PATH for the direct terminal "
            "environment."
        )


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 - propagate across thread
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _load_trajectory(logs_dir: Path) -> dict[str, Any]:
    path = logs_dir / "trajectory.json"
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _terminal_commands_from_trajectory(trajectory: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return commands

    for step in steps:
        if not isinstance(step, dict):
            continue
        tool_calls = step.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            command = _command_from_atif_tool_call(call)
            if command is not None:
                commands.append(command)
    return commands


def _command_from_atif_tool_call(call: Any) -> str | None:
    if isinstance(call, dict):
        name = call.get("function_name") or call.get("name")
        arguments = call.get("arguments")
    else:
        name = getattr(call, "function_name", None) or getattr(call, "name", None)
        arguments = getattr(call, "arguments", None)
    if name != "bash_command" or not isinstance(arguments, dict):
        return None
    command = arguments.get("keystrokes")
    if not isinstance(command, str) or not command:
        return None
    return command


def _final_content_from_trajectory(trajectory: dict[str, Any]) -> str:
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return ""

    last_agent_message = ""
    last_observation = ""
    for step in steps:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        message = step.get("message")
        if isinstance(message, str) and message.strip():
            last_agent_message = message
        observation = step.get("observation")
        if isinstance(observation, dict):
            results = observation.get("results")
            if isinstance(results, list):
                for result in results:
                    if isinstance(result, dict) and isinstance(result.get("content"), str):
                        last_observation = result["content"]

    return last_observation or last_agent_message


def _openai_tool_calls(commands: list[str]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for idx, command in enumerate(commands):
        converted.append(
            {
                "id": f"call_{idx}",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": command}),
                },
            }
        )
    return converted


def _to_response(
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
    worker_warnings: list[str] | None = None,
) -> Response:
    response: Response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ]
    }
    if worker_warnings:
        response["worker_warnings"] = worker_warnings
    return response
