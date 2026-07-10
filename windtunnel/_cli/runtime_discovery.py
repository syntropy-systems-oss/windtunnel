"""Runtime plugin discovery and construction for the CLI."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from typing import cast

from windtunnel.spi.agent_runtime import AgentRuntime
from windtunnel.spi.runtime_plugin import RuntimePlugin


class _InMemoryPlugin:
    """Built-in plugin for the zero-infrastructure scripted runtime."""

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> AgentRuntime:
        from windtunnel.runtimes.in_memory import InMemoryRuntime

        return InMemoryRuntime(scripted_responses=["ok"])


class _HttpInjectPlugin:
    """Built-in plugin for Contract C HTTP inject endpoints."""

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> AgentRuntime:
        from windtunnel.runtimes.http_inject import HttpInjectRuntime

        return HttpInjectRuntime()


class _TerminusPlugin:
    """Built-in plugin for Harbor Terminus-2 terminal agents."""

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> AgentRuntime:
        from windtunnel.runtimes.terminus import TerminusRuntime

        return TerminusRuntime()


def _resolve_runtime_plugin(runtime_name: str) -> RuntimePlugin:
    """Resolve a built-in, entry-point, or dotted-path runtime plugin."""
    builtin: dict[str, Callable[[], RuntimePlugin]] = {
        "http_inject": _HttpInjectPlugin,
        "in_memory": _InMemoryPlugin,
        "terminus": _TerminusPlugin,
    }
    if runtime_name in builtin:
        return builtin[runtime_name]()

    from importlib.metadata import entry_points

    eps = entry_points(group="windtunnel.runtimes")
    for ep in eps:
        if ep.name == runtime_name:
            return _as_plugin_instance(ep.load())

    if ":" in runtime_name:
        module_name, _, attr = runtime_name.partition(":")
        try:
            obj = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            print(
                f"wt run: could not load runtime plugin {runtime_name!r}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        return _as_plugin_instance(obj)

    available = sorted({*builtin, *(ep.name for ep in eps)})
    print(
        f"wt run: unknown runtime {runtime_name!r}. Available: "
        f"{', '.join(available)}. (Or pass a 'module:attr' dotted path to a "
        f"RuntimePlugin.)",
        file=sys.stderr,
    )
    sys.exit(2)


def _as_plugin_instance(obj: object) -> RuntimePlugin:
    """Normalize an entry-point or dotted-path target to a plugin instance."""
    if isinstance(obj, type):
        obj = obj()
    return cast(RuntimePlugin, obj)


def _build_runtime(
    runtime_name: str,
    label: str,
    soul_path: str | None,
    *,
    _plugin: RuntimePlugin | None = None,
) -> AgentRuntime:
    """Instantiate the requested runtime via one resolved plugin instance.

    ``_plugin`` lets the run command retain the exact object it resolved so
    the optional ``pre_run`` lifecycle hook executes on the same instance
    whose ``build`` method created the runtime. Other commands can continue
    using the three-argument form and have the plugin resolved here.
    """
    plugin = _plugin or _resolve_runtime_plugin(runtime_name)
    return plugin.build(runtime_name, label, soul_path)
