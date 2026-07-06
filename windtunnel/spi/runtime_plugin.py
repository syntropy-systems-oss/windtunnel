"""RuntimePlugin SPI — the CLI's runtime-discovery contract.

AgentRuntime (agent_runtime.py) is the *execution* seam: how the framework
drives one provisioned agent. RuntimePlugin is the *packaging* seam: how the
`wt` CLI finds, constructs, and prepares a runtime WITHOUT importing the
runtime's package. This is what lets a platform driver (e.g. an Acme
driver) live in a separate installable package while `wt --runtime <name>`
keeps working unchanged.

Discovery (resolved by the CLI, in order):
  1. Built-ins — "in_memory" (zero-infrastructure scripted runtime) and
     "http_inject" (Contract C endpoint driver), both shipped with the
     framework.
  2. Entry points — `importlib.metadata.entry_points(group="windtunnel.runtimes")`,
     matched by entry-point NAME == the --runtime value. The entry-point value
     must reference a RuntimePlugin INSTANCE or a RuntimePlugin CLASS; a class
     is instantiated with no arguments. One plugin object can be registered
     under several names (e.g. both "acme" and "acme_gateway") — build()
     receives the resolved name so it can pick the right runtime class.
  3. Dotted path — a --runtime value containing ":" is treated as
     "module:attr" and imported directly (same instance-or-class rule).

Lifecycle (one `wt run` invocation):
    plugin = resolve(runtime_name)
    runtime = plugin.build(runtime_name, label, soul_path)
    scenarios = load_scenarios(...)
    plugin.pre_run(runtime, scenarios, runtime_name)   # optional hook
    ... run loop ...

pre_run() is where platform-specific BENCH PREP lives — the glue that used to
be hardcoded in cli.py: container env propagation, fake-server wiring,
workspace seeding, readiness-probe specialization. It is called exactly once,
after build() and scenario loading and before any scenario executes. Plugins
decide applicability themselves by inspecting scenario tags (e.g. only start
a bench fixture server when a matching dim is selected) — the CLI never
special-cases a platform.

pre_run is OPTIONAL: the CLI invokes it via getattr(plugin, "pre_run", None),
so a minimal plugin may omit it entirely. (Note: omitting it also means
isinstance(plugin, RuntimePlugin) is False under @runtime_checkable — the CLI
deliberately does not isinstance-gate on this Protocol for that reason.)

Like the rest of spi/, this is a structural Protocol — implementers don't
subclass anything, they just provide matching methods.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from windtunnel.spi.agent_runtime import AgentRuntime


@runtime_checkable
class RuntimePlugin(Protocol):
    """Factory + bench-prep hook for one (or several) named runtimes.

    Implementers: the built-in in_memory plugin (windtunnel.cli),
    platform driver packages (e.g. an AcmePlugin), and any
    third-party driver registered under the "windtunnel.runtimes"
    entry-point group.
    """

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> AgentRuntime:
        """Construct the AgentRuntime for `runtime_name`.

        runtime_name: the resolved --runtime value. Passed through so one
            plugin registered under multiple entry-point names can serve
            them all (e.g. "acme" vs "acme_gateway" choose different
            runtime classes over the same config).
        label:        the variant label for this run (recorded in traces);
            available for plugins whose construction is label-sensitive.
        soul_path:    the --soul PATH argument (or None). Most plugins ignore
            it here — the CLI separately threads the file content into
            AgentConfig.system_prompt — but it is part of the contract so a
            plugin CAN specialize construction on it.

        Returns a ready-to-provision AgentRuntime. May read platform env
        vars (e.g. WT_ACME_*) and should raise/exit loudly when they
        are missing — build() runs before any scenario, so failing fast
        here is the cheap failure.
        """
        ...

    def pre_run(self, runtime: AgentRuntime, scenarios: list, runtime_name: str) -> None:
        """OPTIONAL one-shot bench prep, after build() + scenario loading.

        This is the home for platform-specific run preparation that needs to
        see the full scenario selection: starting fake upstream servers,
        propagating their dynamic ports into container env (recreate, not
        restart — Docker can't mutate a running container's env), seeding
        fixture workspaces, swapping readiness probes. Inspect the scenarios'
        tags to decide what applies; do nothing when nothing matches.

        Called at most once per `wt run` invocation, before the first
        scenario executes. NOT called by `wt replay` (replay has no scenario
        selection to prep for). Implementations needing end-of-process
        cleanup for resources started here should register it themselves
        (e.g. atexit) — the CLI has no post_run hook.
        """
        ...
