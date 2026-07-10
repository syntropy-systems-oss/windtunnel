"""Runner-side hook dispatch and handle lifecycle serialization."""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from windtunnel.api._runner.messages import extract_reply
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score
from windtunnel.api.trace import Trace
from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle
from windtunnel.spi.hooks import Hook, HookArtifact, HookContext


class SerializedAgentHandle:
    """Serialize sends, resets, and teardown for one runtime handle."""

    def __init__(self, inner: AgentHandle) -> None:
        self._inner = inner
        self._lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._inner.send(messages, session_id)

    def reset_state(self) -> None:
        with self._lock:
            self._inner.reset_state()

    def teardown(self) -> None:
        with self._lock:
            self._inner.teardown()


@dataclass
class RunHookState:
    """Mutable hook side-channel for a single run."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    artifacts: list[HookArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def hook_name(hook: object) -> str:
    return str(getattr(hook, "name", hook.__class__.__name__))


def hook_method(hook: object, point: str) -> Callable[..., Any] | None:
    method = getattr(hook, point, None)
    if not callable(method):
        return None
    if isinstance(hook, Hook) and getattr(type(hook), point, None) is getattr(
        Hook, point, None
    ):
        return None
    return cast(Callable[..., Any], method)


def normalize_hook_reply(response: dict[str, Any]) -> str:
    reply, _tool_calls = extract_reply(response)
    return reply


def dispatch_hooks(
    hooks: Sequence[object],
    point: str,
    *,
    warning_sink: list[str],
    artifact_sink: list[HookArtifact],
    scenario: Scenario | None = None,
    agent: AgentConfig | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    trace: Trace | None = None,
    score: Score | None = None,
    aggregate: Any = None,
    handle: AgentHandle | None = None,
) -> None:
    """Dispatch one lifecycle point without allowing hook failures to gate."""
    if not hooks:
        return
    for hook in hooks:
        context: HookContext | None = None
        try:
            method = hook_method(hook, point)
            if method is None:
                continue
            context = HookContext(
                hook_name=hook_name(hook),
                phase=point,
                scenario=scenario,
                agent=agent,
                run_id=run_id,
                session_id=session_id,
                trace=trace,
                score=score,
                aggregate=aggregate,
                handle=handle if point == "on_run_scored" else None,
                reply_normalizer=normalize_hook_reply,
                warning_sink=warning_sink,
            )
            method(context)
        except Exception as exc:
            warning_sink.append(f"hook:{hook_name(hook)}: {exc}")
        if context is not None:
            artifact_sink.extend(context.artifacts)
