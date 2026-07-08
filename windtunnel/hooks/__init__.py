"""Built-in Wind Tunnel lifecycle hooks."""
from __future__ import annotations

from windtunnel.hooks.debrief import DebriefHook
from windtunnel.hooks.state_probe import StateProbeHook

BUILTIN_HOOKS = {
    "debrief": DebriefHook,
}

__all__ = ["BUILTIN_HOOKS", "DebriefHook", "StateProbeHook"]
