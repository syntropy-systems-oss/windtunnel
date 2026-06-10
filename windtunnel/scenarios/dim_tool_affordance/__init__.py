"""Dim: tool affordance understanding.

Three scenarios that test whether the model builds the right mental
model of each tool's contract: strict-id vs lenient-name, in-scope vs
out-of-scope, when to chain lookup→fetch vs one-shot.

Scenarios:
  lookup_before_action  — must call client_lookup BEFORE order_query
  field_scope_inference — contact email IS in scope via client_lookup
  wrong_tool_correction — catalog info = product_lookup, not order tools

Mock MCP: mock_mcp/server.py — ops-suite-shaped tools for this dim only.
Run via `wt run` (windtunnel.api.runner.run_scenario).
"""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import fastmcp_factory
from windtunnel.scenarios.dim_tool_affordance.scenarios import TOOL_AFFORDANCE_SCENARIOS

PACK = ScenarioPack(
    name="tool_affordance",
    scenarios=list(TOOL_AFFORDANCE_SCENARIOS),
    mcp_factory=fastmcp_factory(
        "windtunnel.scenarios.dim_tool_affordance.mock_mcp.server", port=8091
    ),
)
