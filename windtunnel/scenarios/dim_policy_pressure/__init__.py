"""Dim policy_pressure — scenarios, verdict logic, mock MCP."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_policy_pressure.scenarios import POLICY_PRESSURE_SCENARIOS

PACK = ScenarioPack(
    name="policy_pressure",
    scenarios=list(POLICY_PRESSURE_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_policy_pressure.mock_mcp.server", port=8094
    ),
)
