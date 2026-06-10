"""Dim: recovery from bad intermediate state."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_recovery.scenarios import RECOVERY_SCENARIOS

PACK = ScenarioPack(
    name="recovery",
    scenarios=list(RECOVERY_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_recovery.mock_mcp.server", port=8083
    ),
)
