"""Hook discovery and CLI-scope pack-end dispatch."""

from __future__ import annotations

import sys

from windtunnel._cli.models import _CompletedAggregate
from windtunnel.spi.agent_runtime import AgentConfig
from windtunnel.spi.hooks import Hook, HookArtifact, HookContext


def _resolve_hooks(hook_names: list[str] | None) -> list[object]:
    """Resolve repeatable ``--hook`` names to hook instances."""
    if not hook_names:
        return []

    from importlib.metadata import entry_points

    from windtunnel.hooks import BUILTIN_HOOKS

    eps = entry_points(group="windtunnel.hooks")
    hooks: list[object] = []
    for hook_name in hook_names:
        if hook_name in BUILTIN_HOOKS:
            hooks.append(_as_hook_instance(BUILTIN_HOOKS[hook_name]))
            continue
        for ep in eps:
            if ep.name == hook_name:
                hooks.append(_as_hook_instance(ep.load()))
                break
        else:
            available = sorted({*BUILTIN_HOOKS, *(ep.name for ep in eps)})
            print(
                f"wt run: unknown hook {hook_name!r}. Available: "
                f"{', '.join(available) if available else '(none)'}.",
                file=sys.stderr,
            )
            sys.exit(2)
    return hooks


def _as_hook_instance(obj: object) -> object:
    if isinstance(obj, type):
        return obj()
    return obj


def _dispatch_pack_end_hooks(
    hooks: list[object],
    *,
    config: AgentConfig,
    completed: list[_CompletedAggregate],
) -> list[HookArtifact]:
    """Fire CLI-scope ``on_pack_end`` hooks and return buffered artifacts."""
    if not hooks:
        return []

    artifacts: list[HookArtifact] = []
    aggregates = [item.result.aggregate for item in completed]
    for hook in hooks:
        method = getattr(hook, "on_pack_end", None)
        if not callable(method):
            continue
        if isinstance(hook, Hook) and getattr(type(hook), "on_pack_end", None) is Hook.on_pack_end:
            continue
        ctx = None
        warnings: list[str] = []
        try:
            ctx = HookContext(
                hook_name=str(getattr(hook, "name", hook.__class__.__name__)),
                phase="on_pack_end",
                agent=config,
                aggregate=aggregates,
                warning_sink=warnings,
            )
            method(ctx)
        except Exception as exc:  # noqa: BLE001 - pack hooks are diagnostic only
            print(
                f"wt run: warning: hook:{getattr(hook, 'name', hook.__class__.__name__)}: {exc}",
                file=sys.stderr,
            )
        for warning in warnings:
            print(f"wt run: warning: {warning}", file=sys.stderr)
        if ctx is not None:
            artifacts.extend(ctx.artifacts)
    return artifacts
