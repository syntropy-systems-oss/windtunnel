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

Lifecycle (one `wt run` or supported `wt selftest` invocation):
    plugin = resolve(runtime_name)
    runtime = plugin.build(runtime_name, label, soul_path)
    scenarios = load_scenarios(...)
    plugin.pre_run(runtime, scenarios, runtime_name)   # optional hook
    ... run or reference-case loop ...
    plugin.post_run(runtime, scenarios, runtime_name)   # optional hook — wt run only, see below

The same plugin instance is retained for both calls. pre_run() is where
platform-specific BENCH PREP lives — the glue that used to
be hardcoded in cli.py: container env propagation, fake-server wiring,
workspace seeding, readiness-probe specialization. It is called exactly once,
after build() and scenario loading and before any scenario executes. Plugins
decide applicability themselves by inspecting scenario tags (e.g. only start
a bench fixture server when a matching dim is selected) — the CLI never
special-cases a platform.

post_run() is pre_run()'s symmetric counterpart: bench TEARDOWN for whatever
pre_run() stood up for the WHOLE sweep — a subprocess, a mock server, a
container, anything scoped to the batch rather than to one scenario. Without
it, a plugin's only teardown seam was AgentHandle.teardown() (per-runtime,
torn down inside run_scenario's own finally), which a bench fixture started
in pre_run() never goes through — nothing ever released it, and it leaked
for the lifetime of the `wt run` process. `wt run` wraps its whole sweep (the
scenario loop, its circuit breaker, and final exit-code accounting) in a
try/finally so post_run() runs exactly once: on a clean finish, on the
sweep's own circuit-breaker abort, and on any exception that escapes the loop
itself — never only the happy path. It receives the same (runtime, scenarios,
runtime_name) arguments as pre_run() so a plugin can find whatever it started
there without needing extra module-level state. `wt selftest`'s reference-case
loop does not call post_run() yet — a plugin whose bench prep needs
whole-process teardown should still release it there some other way (e.g. an
atexit hook) until that command grows the same symmetric wiring.

Both hooks are OPTIONAL: the CLI invokes each via getattr(plugin, name, None),
so a minimal plugin may omit either or both entirely. RuntimePlugin therefore
declares only the required build() method; implementations may add pre_run
and/or post_run without a second registration contract. A plugin that defines
one without the other is well-formed — e.g. a plugin whose bench prep needs
no teardown (or vice versa, one that only needs to clean up a resource
started somewhere other than pre_run) never has to stub the unused half.

Like the rest of spi/, this is a structural Protocol — implementers don't
subclass anything, they just provide matching methods.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from windtunnel.spi.agent_runtime import AgentRuntime


@runtime_checkable
class RuntimePlugin(Protocol):
    """Required runtime factory contract for one or several runtime names.

    Implementers: the built-in plugins (windtunnel._cli.runtime_discovery),
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
