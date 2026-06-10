"""dim_multi_turn_drift — multi-turn context preservation dimension."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_multi_turn_drift.scenarios import MULTI_TURN_DRIFT_SCENARIOS

PACK = ScenarioPack(
    name="multi_turn_drift",
    # user_turns is a first-class Scenario field set in this dim's
    # scenarios.py constructors — the runner drives the full turn sequence
    # directly. (Historically the MultiTurnScenario wrapper carried the
    # turns and this pack attached them via setattr; the wrapper is now a
    # thin proxy and only the inner Scenario is exported to the runner.)
    scenarios=[mts.scenario for mts in MULTI_TURN_DRIFT_SCENARIOS],
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_multi_turn_drift.mock_mcp.server", port=8084
    ),
)
