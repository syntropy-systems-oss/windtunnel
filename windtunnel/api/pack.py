"""ScenarioPack — the distribution unit for one scenario dimension.

Runtimes are already pluggable: a driver package registers a RuntimePlugin
under the "windtunnel.runtimes" entry-point group and `wt --runtime <name>`
finds it without touching cli.py. ScenarioPack is the same seam for scenario
DIMENSIONS: a pack bundles a dim's scenarios with the runtime wiring the CLI
used to hardcode per dim (mock-MCP factory, state probe, and transport-only
policy). The selected pack is the operational authority; scenario tags remain
descriptive/filtering metadata. A third party ships a new dimension as a package
registering the "windtunnel.scenario_packs" entry-point group instead of
forking cli.py. Built-in dims expose a module-level PACK in their
windtunnel/scenarios/dim_*/__init__.py and are listed by
windtunnel.scenarios.builtin_packs().

Placement: api/ (not scenarios/ or spi/) because a pack is something scenario
AUTHORS construct — and the import invariant
(tests/test_import_invariants.py) lets api/ import spi/ (runner.py already
does) while forbidding api/ → windtunnel.runtimes.* / windtunnel.mcp.*.
That is why mcp_factory is typed against the spi.MCPServer Protocol, never a
concrete server class: concrete construction stays behind a deferred import
in the pack that needs it (see windtunnel/scenarios/_mock_factory.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from windtunnel.api.scenario import Scenario
from windtunnel.spi.mcp_server import MCPServer
from windtunnel.spi.state_probe import StateProbe


@dataclass
class ScenarioPack:
    """One dimension's scenarios plus the CLI wiring they need to run.

    name:        the pack name and scenario-selection identity (e.g.
        "tool_affordance"). The CLI binds operational wiring directly from a
        selected scenario's owning pack. ``dim:<name>`` scenario tags remain
        descriptive/filtering metadata and are validated at pack discovery so
        stale names fail loudly instead of silently mis-filtering a sweep.

    scenarios:   the Scenario objects this pack contributes to the selection
        pool. `wt run` and `wt selftest` flatten every discovered pack's list
        (built-ins in canonical order, then entry-point packs) and filter by
        --scenario.

    mcp_factory: builds the pack's mock MCPServer for a given scenario, or
        None when the pack needs no canned upstream tools. Called once per
        scenario batch by `wt run`, or once per reference case by `wt
        selftest`, so each run_scenario() call gets a FRESH,
        not-yet-started server (the runner owns start/stop). The CLI
        reads it from the selected scenario's owning pack, never from tags. It
        receives the selected Scenario so scenario-AWARE factories can
        specialize — silent_failure derives MOCK_MCP_FAILURE_MODE from the
        scenario's perturbation; most factories ignore the argument. Only
        plugin runtimes consume the result: the built-in in_memory runtime
        is scripted and ignores mocks entirely, so the CLI never invokes the
        factory for it.

    state_probe_factory: builds the pack's StateProbe for a given scenario,
        or returns None when that scenario needs no external-state capture.
        The probe seam mirrors mcp_factory: called once per scenario batch or
        reference case, the result is passed to
        run_scenario(state_probe=...), which resets it
        before each run and freezes capture() into trace.observations
        before scoring (see windtunnel/spi/state_probe.py). A probe
        usually closes over a live bench fixture; when that fixture is
        started by a RuntimePlugin's pre_run() (the driver pattern), the
        pack ships with state_probe_factory=None and pre_run sets it on
        the pack's module-level PACK singleton after the fixture is up —
        pre_run runs before any scenario, so the CLI's later factory call sees
        the wired value. Only the probe returned here (or supplied directly to
        the library API) populates PreconditionContext.state_probe; probes
        hidden in plugins or scorers do not. Scenarios that score observations
        should declare a ``StateProbeAvailable`` world precondition so missing
        wiring fails before the agent runs. None (the default) = no external
        state to observe.

    transport_only: True means this dim's history/context-shaping
        perturbation is applied POST-HOC to the recorded trace (see
        runner.py _run_once), so the live model never actually saw it — the
        MODEL verdict scores a counterfactual and must not flip the `wt run`
        exit code. The TRANSPORT is still exercised faithfully: the scenario
        runs, the trace is saved, and a real EXECUTION error (a
        `runner_error:` worker warning) still fails the sweep because no
        valid model turn happened at all. This replaces cli.py's old
        hardcoded _HISTORY_SHAPING_DIMS set; memory_conflict is the one
        built-in pack that sets it (see its __init__ for the dim-specific
        rationale — an injected system-role memory line is UNVERIFIED to
        reach the live model).

    owner: free-form ownership label for downstream suite stewardship: a team
        name, a GitHub handle, a CODEOWNERS path, or None when the pack does
        not declare one. Wind Tunnel attaches no policy to this value; the CLI
        only carries it into the append-only run ledger.

    metadata: free-form string map for pack-local annotations. Core does not
        interpret it. The default is a fresh empty dict so pack authors can add
        notes without sharing mutable state across ScenarioPack instances.
    """

    name: str
    scenarios: list[Scenario] = field(default_factory=list)
    mcp_factory: Callable[[Scenario], MCPServer] | None = None
    state_probe_factory: Callable[[Scenario], StateProbe | None] | None = None
    transport_only: bool = False
    owner: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
