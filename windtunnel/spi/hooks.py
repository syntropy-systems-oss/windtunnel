"""Lifecycle hook SPI for observing Wind Tunnel runs.

Hooks are deliberately observational: they can inspect scoped run metadata,
ask one post-score follow-up turn through ``HookContext.converse()``, and
buffer JSON artifacts for the framework to persist. They do not receive the
raw runtime handle and cannot reset, teardown, score, or write files.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle

_HOOK_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class HookArtifact:
    """A buffered JSON artifact emitted by a hook."""

    hook_name: str
    payload: Any
    label: str | None = None


class Hook:
    """Base class for lifecycle hooks.

    Subclass and override whichever points you need. The framework dispatches
    hooks in activation order and catches exceptions at every point.
    """

    name: str = "hook"

    def on_provisioned(self, ctx: HookContext) -> None:
        pass

    def on_run_start(self, ctx: HookContext) -> None:
        pass

    def on_run_scored(self, ctx: HookContext) -> None:
        pass

    def on_run_end(self, ctx: HookContext) -> None:
        pass

    def on_scenario_end(self, ctx: HookContext) -> None:
        pass

    def on_pack_end(self, ctx: HookContext) -> None:
        pass


class HookContext:
    """Scoped read-only context handed to lifecycle hooks."""

    def __init__(
        self,
        *,
        hook_name: str,
        phase: str,
        scenario: Any = None,
        agent: AgentConfig | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        trace: Any = None,
        score: Any = None,
        aggregate: Any = None,
        handle: AgentHandle | None = None,
        reply_normalizer: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        if not _HOOK_NAME_RE.fullmatch(hook_name):
            raise ValueError(
                "hook name must be a filesystem-safe slug matching [a-z0-9_-]+"
            )
        self._hook_name = hook_name
        self._phase = phase
        self._scenario = scenario
        self._agent = agent
        self._run_id = run_id
        self._session_id = session_id
        self._trace = trace
        self._score = score
        self._aggregate = aggregate
        self._handle = handle
        self._reply_normalizer = reply_normalizer or _default_reply_normalizer
        self._artifacts: list[HookArtifact] = []
        self._converse_used = False
        self._converse_calls: list[dict[str, Any]] = []

    @property
    def scenario(self) -> Any:
        return self._scenario

    @property
    def agent(self) -> AgentConfig | None:
        return self._agent

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def trace(self) -> Any:
        return self._trace

    @property
    def score(self) -> Any:
        return self._score

    @property
    def aggregate(self) -> Any:
        return self._aggregate

    @property
    def artifacts(self) -> tuple[HookArtifact, ...]:
        return tuple(self._artifacts)

    @property
    def converse_used(self) -> bool:
        return self._converse_used

    @property
    def converse_timed_out(self) -> bool:
        return any(bool(call.get("timed_out")) for call in self._converse_calls)

    @property
    def converse_duration_ms(self) -> int | None:
        if not self._converse_calls:
            return None
        duration = self._converse_calls[-1].get("duration_ms")
        return int(duration) if duration is not None else None

    @property
    def converse_error(self) -> str | None:
        errors = [
            str(call["error"])
            for call in self._converse_calls
            if call.get("error")
        ]
        return "; ".join(errors) if errors else None

    @property
    def converse_calls(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(call) for call in self._converse_calls)

    def converse(self, text: str) -> str:
        """Send one post-score text turn into the current run's session."""

        if self._phase != "on_run_scored" or self._handle is None or self._session_id is None:
            raise RuntimeError("ctx.converse() is only valid during on_run_scored")

        self._converse_used = True

        timeout_s = _converse_timeout_s()
        started = time.perf_counter()
        result_queue: queue.Queue[tuple[str, str | BaseException]] = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                response = self._handle.send(
                    [{"role": "user", "content": text}],
                    self._session_id or "",
                )
                reply = self._reply_normalizer(response)
            except BaseException as exc:  # noqa: BLE001 - propagate through queue
                item: tuple[str, str | BaseException] = ("error", exc)
            else:
                item = ("ok", reply)
            try:
                result_queue.put_nowait(item)
            except queue.Full:
                pass

        thread = threading.Thread(target=_worker, name="windtunnel-hook-converse", daemon=True)
        thread.start()

        try:
            status, value = result_queue.get(timeout=timeout_s)
        except queue.Empty as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            error = f"converse timed out after {timeout_s:g}s"
            self._converse_calls.append({
                "status": "timeout",
                "timed_out": True,
                "duration_ms": duration_ms,
                "error": error,
            })
            raise RuntimeError(error) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        if status == "error":
            assert isinstance(value, BaseException)
            self._converse_calls.append({
                "status": "error",
                "timed_out": False,
                "duration_ms": duration_ms,
                "error": str(value),
            })
            raise RuntimeError(f"converse failed: {value}") from value
        assert isinstance(value, str)
        self._converse_calls.append({
            "status": "ok",
            "timed_out": False,
            "duration_ms": duration_ms,
            "error": None,
        })
        return value

    def emit_artifact(self, payload: Any, label: str | None = None) -> None:
        """Buffer a JSON-serializable hook artifact."""

        if label is not None and not _HOOK_NAME_RE.fullmatch(label):
            raise ValueError(
                "artifact label must be a filesystem-safe slug matching [a-z0-9_-]+"
            )

        try:
            normalized = json.loads(json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise TypeError("hook artifacts must be JSON-serializable") from exc

        if isinstance(normalized, dict):
            if self._converse_used and "tools_disabled" not in normalized:
                normalized["tools_disabled"] = False
            if self.converse_timed_out and "timed_out" not in normalized:
                normalized["timed_out"] = True

        self._artifacts.append(
            HookArtifact(hook_name=self._hook_name, payload=normalized, label=label)
        )


def _default_reply_normalizer(response: dict[str, Any]) -> str:
    msg: dict[str, Any] = {}
    choices = response.get("choices")
    if choices:
        msg = choices[0].get("message") or {}
    elif isinstance(response.get("message"), dict):
        msg = response["message"]
    elif "choices" not in response:
        msg = response
    return msg.get("content") or ""


def _converse_timeout_s() -> float:
    raw = os.environ.get("WT_HOOK_CONVERSE_TIMEOUT_S")
    if raw is None:
        return 30.0
    try:
        timeout = float(raw)
    except ValueError:
        return 30.0
    if timeout <= 0:
        return 30.0
    return timeout
