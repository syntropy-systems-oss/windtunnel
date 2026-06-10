"""Dim clarify_vs_guess — scenario package."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_clarify_vs_guess.scenarios import CLARIFY_VS_GUESS_SCENARIOS

PACK = ScenarioPack(
    name="clarify_vs_guess",
    scenarios=list(CLARIFY_VS_GUESS_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_clarify_vs_guess.mock_mcp.server", port=8092
    ),
)
