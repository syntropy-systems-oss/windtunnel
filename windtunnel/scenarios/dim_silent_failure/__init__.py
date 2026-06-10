# dim_silent_failure — environment-misbehavior dimension
from typing import Any

from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios._mock_factory import build_fastmcp_server
from windtunnel.scenarios.dim_silent_failure.scenarios import SILENT_FAILURE_SCENARIOS

# silent_failure is the one scenario-AWARE mcp_factory: the mock injects the
# failure via the MOCK_MCP_FAILURE_MODE env (read at import in subprocess
# mode), and each scenario carries a different perturbation class. Derive the
# mode (+ timeout) from the selected scenario's perturbation so the failure
# actually fires — otherwise the scenario would pass VACUOUSLY with no
# injected failure.
_FAILURE_MODE_BY_PERTURBATION = {
    "ToolReturnsMalformedJson": "malformed_json",
    "ToolTimeoutPerScenario": "timeout",
    "ToolReturnsEmptyUnexpected": "empty_unexpected",
    "ToolReturnsSchemaError": "schema_error",
}


def _silent_failure_server(scenario: Any = None) -> Any:
    extra_env: dict[str, str] = {}
    perturbations = list(getattr(scenario, "perturbations", []) or [])
    if perturbations:
        pert = perturbations[0]
        mode = _FAILURE_MODE_BY_PERTURBATION.get(type(pert).__name__)
        if mode:
            extra_env["MOCK_MCP_FAILURE_MODE"] = mode
            if mode == "timeout":
                delay = getattr(pert, "delay_seconds", None)
                if delay is not None:
                    extra_env["MOCK_MCP_TIMEOUT_SECONDS"] = str(delay)
    return build_fastmcp_server(
        "windtunnel.scenarios.dim_silent_failure.mock_mcp.server",
        port=8096,
        extra_env=extra_env,
    )


PACK = ScenarioPack(
    name="silent_failure",
    scenarios=list(SILENT_FAILURE_SCENARIOS),
    mcp_factory=_silent_failure_server,
)
