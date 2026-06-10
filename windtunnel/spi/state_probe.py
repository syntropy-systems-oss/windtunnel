"""StateProbe SPI — the external-state evidence contract.

MCP-mediated tools already leave server-witnessed evidence in the trace
(MCPHandle.call_log() → trace.mcp_calls). But an agent can mutate the
world through channels the mock MCP never sees: a fake GitHub the agent
drives with native git tools, a database written through a CLI, a
filesystem, an email outbox. Without a capture seam, the only way to
verify that state is a Policy predicate that queries the live fixture at
scoring time — which breaks the framework's deepest property, that a
saved Trace is re-scorable offline (the fixture died when the bench
exited, but the verdict depends on it).

StateProbe closes that gap: it snapshots external state into plain data
at the end of each run, and the runner freezes the snapshot into
trace.observations BEFORE scoring — the same evidence-first ordering
_collect_mcp_calls() follows for call logs. Policy predicates then read
trace.observations like any other trace field: pure data in, bool out,
no live fixture at scoring time, and saved traces re-score forever.

Lifecycle (mirrors MCPHandle's call-log contract, per run inside
run_scenario):

    probe.reset()              # before the run — wipe prior runs' state
    ... agent runs ...
    obs = probe.capture()      # after the final turn, before scoring
    trace.observations = obs   # frozen into the trace

reset() is NOT optional: with runs_per_scenario > 1, run 2's fixture
still contains run 1's mutations (the PR the agent opened, the rows it
inserted) unless the probe wipes it — the same cross-run contamination
class reset_call_log() exists for.

Wiring: ScenarioPack.state_probe_factory builds the probe per scenario
(mirroring mcp_factory), and run_scenario accepts state_probe= directly
for library callers. A probe usually closes over a live fixture started
elsewhere (e.g. a RuntimePlugin's pre_run starting a fake-GitHub server);
when the fixture is born in pre_run, pre_run is also the natural place to
set the pack's state_probe_factory — see ScenarioPack for the pattern.

Like the rest of spi/, this is a structural Protocol — implementers don't
subclass anything, they just provide matching methods.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StateProbe(Protocol):
    """Snapshot-er of external (non-MCP) world state for one scenario run.

    Implementers: any object with capture() + reset() — typically a thin
    wrapper around a bench fixture (fake GitHub, seeded database,
    scratch filesystem) that knows how to read it out as plain data.
    """

    def capture(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the external state.

        Called once per run, after the final agent turn and before
        scoring. Keys are evidence-source names, values their snapshots:

            {"github": {"branches": [...], "prs": [{"base": "main", ...}]}}

        Must return a dict of JSON-serializable data — the snapshot is
        stored verbatim on trace.observations and round-tripped through
        save_trace()/load_trace(). The runner coerces non-serializable
        leaves to repr() as a last resort (matching mcp_calls), but a
        probe should not rely on that: repr-mangled evidence is hard to
        write predicates against.

        A capture() that raises does not crash the run: the runner
        records a "probe_error: ..." worker warning and leaves
        observations empty, so triage can tell a broken probe from a
        policy the agent genuinely violated.
        """
        ...

    def reset(self) -> None:
        """Wipe the external state back to its seeded baseline.

        Called by the runner before EACH run (alongside
        MCPHandle.reset_call_log()) so run N's observations reflect only
        run N's mutations. Idempotent.
        """
        ...
