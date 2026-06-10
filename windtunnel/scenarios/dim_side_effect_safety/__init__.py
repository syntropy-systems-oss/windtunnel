# dim_side_effect_safety — effect-class autonomy ceilings
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_side_effect_safety.scenarios import (
    SIDE_EFFECT_SAFETY_SCENARIOS,
)

PACK = ScenarioPack(
    name="side_effect_safety",
    scenarios=list(SIDE_EFFECT_SAFETY_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_side_effect_safety.mock_mcp.server", port=8095
    ),
)
