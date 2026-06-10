"""ScenarioPack — the distribution unit for one scenario dimension.

Runtimes are already pluggable: a driver package registers a RuntimePlugin
under the "windtunnel.runtimes" entry-point group and `wt --runtime <name>`
finds it without touching cli.py. ScenarioPack is the same seam for scenario
DIMENSIONS: a pack bundles a dim's scenarios with the wiring the CLI used to
hardcode per dim (the dim-tag → mock-MCP factory registry and the
transport-only exemption set), so a third party ships a new dimension as a
package registering the "windtunnel.scenario_packs" entry-point group
instead of forking cli.py. Built-in dims expose a module-level PACK in their
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


@dataclass
class ScenarioPack:
    """One dimension's scenarios plus the CLI wiring they need to run.

    name:        the dim name WITHOUT the "dim:" prefix (e.g. "tool_affordance").
        The CLI keys its mock registry and transport-only set by the derived
        tag f"dim:{name}", matching the `tags=["dim:<name>"]` convention on
        each Scenario — so a scenario finds its pack's mock by tag, exactly
        as the old hand-built registry did.

    scenarios:   the Scenario objects this pack contributes to the selection
        pool. `wt run` flattens every discovered pack's list (built-ins in
        canonical order, then entry-point packs) and filters by --scenario.

    mcp_factory: builds the pack's mock MCPServer for a given scenario, or
        None when the pack needs no canned upstream tools. Called once per
        scenario so each run_scenario() batch gets a FRESH, not-yet-started
        server (the runner owns start-per-batch / stop-per-batch). It
        receives the selected Scenario so scenario-AWARE factories can
        specialize — silent_failure derives MOCK_MCP_FAILURE_MODE from the
        scenario's perturbation; most factories ignore the argument. Only
        plugin runtimes consume the result: the built-in in_memory runtime
        is scripted and ignores mocks entirely, so the CLI never invokes the
        factory for it.

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
    """

    name: str
    scenarios: list[Scenario] = field(default_factory=list)
    mcp_factory: Callable[[Scenario], MCPServer] | None = None
    transport_only: bool = False
