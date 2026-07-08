"""Abstract state-probe lifecycle hook."""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from windtunnel.spi.hooks import Hook, HookContext
from windtunnel.spi.state_probe import StateProbe

_MAX_DEPTH = 4
_MAX_ITEMS = 12
_MAX_TEXT = 400
_MAX_DIFF_KEYS = 12


@dataclass(frozen=True)
class _Snapshot:
    run_id: str | None
    state: dict[str, Any]
    fingerprint: str
    summary: Any


class StateProbeHook(Hook, ABC):
    """Assert reset isolation continuously with StateProbe-compatible snapshots.

    ``run_reset_canary(..., state_probe=...)`` remains the seeded-nonce doctor
    check. This hook reuses that same ``windtunnel.spi.state_probe.StateProbe``
    capture contract for in-pack observation: subclasses may wrap an existing
    canary-compatible probe, but the hook only calls ``capture()``. It does not
    call ``reset()``, because the reset being audited is the runtime's
    ``reset_state()`` before each ``on_run_start``.

    The first run's post-reset snapshot establishes the clean baseline. Each
    later run-start snapshot is compared against that baseline; a mismatch is
    reported as a hook warning plus a bounded violation artifact, never as a
    raised verdict-changing failure.
    """

    name = "state_probe"
    violation_label = "violation"
    capture_run_end_context = True

    def __init__(self, probe: StateProbe | None = None) -> None:
        self._probe = probe
        self._baseline: _Snapshot | None = None
        self._previous_run_end: _Snapshot | None = None

    @abstractmethod
    def capture_state(self) -> dict[str, Any]:
        """Capture the current state snapshot.

        Subclasses that wrap an existing canary-compatible ``StateProbe`` can
        implement this as ``return self._capture_from_probe()``. Subclasses
        with deployment-specific fixtures can read those fixtures directly, as
        long as the returned value follows the StateProbe ``capture()`` shape.
        """

        return self._capture_from_probe()

    def _capture_from_probe(self) -> dict[str, Any]:
        if self._probe is None:
            raise RuntimeError("StateProbeHook subclass did not provide a probe")
        return self._probe.capture()

    def on_provisioned(self, ctx: HookContext) -> None:
        self._baseline = None
        self._previous_run_end = None

    def on_run_start(self, ctx: HookContext) -> None:
        current = _snapshot(ctx.run_id, self.capture_state())
        if self._baseline is None:
            self._baseline = current
            return
        if current.fingerprint == self._baseline.fingerprint:
            return

        difference = _describe_difference(self._baseline.state, current.state)
        message = (
            "post-reset state differs from the first-run baseline; "
            "the previous run may have leaked past reset_state()"
        )
        ctx.warn(f"{message}: {difference['summary']}")
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": ctx.run_id or "",
            "baseline_run_id": self._baseline.run_id or "",
            "violation": "post_reset_state_mismatch",
            "message": message,
            "difference": difference,
            "baseline_fingerprint": self._baseline.fingerprint,
            "observed_fingerprint": current.fingerprint,
            "baseline_summary": self._baseline.summary,
            "observed_summary": current.summary,
        }
        if self._previous_run_end is not None:
            payload["previous_run_end"] = {
                "run_id": self._previous_run_end.run_id or "",
                "fingerprint": self._previous_run_end.fingerprint,
                "summary": self._previous_run_end.summary,
            }
        ctx.emit_artifact(payload, label=self.violation_label)

    def on_run_end(self, ctx: HookContext) -> None:
        if not self.capture_run_end_context:
            return
        self._previous_run_end = _snapshot(ctx.run_id, self.capture_state())


def _snapshot(run_id: str | None, state: dict[str, Any]) -> _Snapshot:
    if not isinstance(state, dict):
        raise TypeError("state probe capture() must return a dict")
    return _Snapshot(
        run_id=run_id,
        state=state,
        fingerprint=_fingerprint(state),
        summary=_bounded_json(state),
    )


def _fingerprint(value: Any) -> str:
    try:
        stable = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_bounded_repr,
        )
    except (TypeError, ValueError):
        stable = json.dumps(
            _bounded_json(value),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _describe_difference(baseline: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    baseline_keys = {str(key): key for key in baseline}
    observed_keys = {str(key): key for key in observed}
    added_all = sorted(set(observed_keys) - set(baseline_keys))
    removed_all = sorted(set(baseline_keys) - set(observed_keys))
    changed_all = [
        key
        for key in sorted(set(baseline_keys) & set(observed_keys))
        if _fingerprint(baseline[baseline_keys[key]])
        != _fingerprint(observed[observed_keys[key]])
    ]

    added = _bounded_key_list(added_all)
    removed = _bounded_key_list(removed_all)
    changed = _bounded_key_list(changed_all)
    parts: list[str] = []
    if added:
        parts.append(f"added keys {', '.join(added)}")
    if removed:
        parts.append(f"removed keys {', '.join(removed)}")
    if changed:
        parts.append(f"changed keys {', '.join(changed)}")
    if not parts:
        parts.append("snapshot changed")

    return {
        "summary": "; ".join(parts),
        "added_keys": added,
        "removed_keys": removed,
        "changed_keys": changed,
        "added_key_count": len(added_all),
        "removed_key_count": len(removed_all),
        "changed_key_count": len(changed_all),
    }


def _bounded_key_list(values: list[str]) -> list[str]:
    head = [_bounded_text(value) for value in values[:_MAX_DIFF_KEYS]]
    if len(values) <= _MAX_DIFF_KEYS:
        return head
    return [*head, f"... {len(values) - _MAX_DIFF_KEYS} more"]


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _bounded_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _MAX_DEPTH:
        return _bounded_repr(value)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        out: dict[str, Any] = {}
        for key, item_value in items[:_MAX_ITEMS]:
            out[_bounded_text(str(key))] = _bounded_json(
                item_value,
                depth=depth + 1,
            )
        if len(items) > _MAX_ITEMS:
            out["__truncated__"] = f"{len(items) - _MAX_ITEMS} more keys"
        return out
    if isinstance(value, list | tuple):
        out = [_bounded_json(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            out.append({"__truncated__": f"{len(value) - _MAX_ITEMS} more items"})
        return out
    if isinstance(value, set | frozenset):
        rendered = sorted(_bounded_repr(item) for item in value)
        out = rendered[:_MAX_ITEMS]
        if len(rendered) > _MAX_ITEMS:
            out.append(f"... {len(rendered) - _MAX_ITEMS} more")
        return out
    return _bounded_repr(value)


def _bounded_repr(value: Any) -> str:
    return _bounded_text(repr(value))


def _bounded_text(value: str) -> str:
    if len(value) <= _MAX_TEXT:
        return value
    return f"{value[:_MAX_TEXT]}... <truncated {len(value) - _MAX_TEXT} chars>"
