"""Dim sampler_sensitivity — matrix dispatcher/aggregation across model × temp × top_p."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_sampler_sensitivity.scenarios import (
    SAMPLER_SENSITIVITY_SCENARIOS,
)

PACK = ScenarioPack(
    name="sampler_sensitivity",
    scenarios=list(SAMPLER_SENSITIVITY_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_sampler_sensitivity.mock_mcp.server", port=8097
    ),
)
