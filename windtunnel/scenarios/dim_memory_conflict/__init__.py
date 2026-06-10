"""dim_memory_conflict — memory conflict and stale-fact handling scenarios."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_memory_conflict.scenarios import MEMORY_CONFLICT_SCENARIOS

PACK = ScenarioPack(
    name="memory_conflict",
    scenarios=list(MEMORY_CONFLICT_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_memory_conflict.mock_mcp.server", port=8085
    ),
    # transport_only (see ScenarioPack for the general semantics): this dim's
    # InjectStaleMemory.shape_messages injects the stale memory as a
    # SYSTEM-role context message. Whether an agent honors an injected system
    # message in conversation_history (alongside its own SOUL system prompt)
    # is UNVERIFIED — it may be stripped, in which case the model never sees
    # the conflict and trivially "passes". A REAL-memory seeding helper exists
    # (memory_seeder.py in this package) but the generic `wt run` path doesn't
    # invoke it. Until the system-context fidelity is empirically confirmed
    # (or memory-file seeding is wired into the runtime), this dim's MODEL
    # verdict is not trusted for the exit code. The TRANSPORT is still
    # exercised faithfully. Contrast: recovery + icl_poisoning inject
    # ASSISTANT + TOOL turns (a mechanism proven end-to-end), and
    # silent_failure's failures are ENV-shaping via the live mock — none of
    # those are counterfactual, so none set this flag.
    transport_only=True,
)
