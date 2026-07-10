"""Wind Tunnel SPI — contracts for runtime/platform implementers.

Runtime authors implement AgentRuntime + AgentHandle (agent_runtime.py)
and MCPServer + MCPHandle (mcp_server.py) to plug their platform into the
framework, and RuntimePlugin (runtime_plugin.py) to make it discoverable
by the `wt` CLI. Scenario authors never import from this module.
"""
from windtunnel.spi.agent_runtime import (
    AgentConfig,
    AgentHandle,
    AgentRuntime,
    Message,
    ModelSpec,
    Response,
    RunnerMCPConfigurableRuntime,
    SamplingConfig,
    SurfaceIntrospectableAgentHandle,
)
from windtunnel.spi.hooks import Hook, HookArtifact, HookContext
from windtunnel.spi.mcp_server import (
    FailureInjectableMCPHandle,
    MCPCall,
    MCPHandle,
    MCPServer,
    MCPSpec,
    ToolDefinitionIntrospectableMCPHandle,
    ToolIntrospectableMCPHandle,
)
from windtunnel.spi.runtime_plugin import RuntimePlugin
from windtunnel.spi.state_probe import StateProbe

__all__ = [
    "AgentConfig", "AgentHandle", "AgentRuntime", "Message", "ModelSpec",
    "Response", "RunnerMCPConfigurableRuntime", "SamplingConfig",
    "SurfaceIntrospectableAgentHandle",
    "Hook", "HookArtifact", "HookContext",
    "FailureInjectableMCPHandle", "MCPCall", "MCPHandle", "MCPServer", "MCPSpec",
    "ToolDefinitionIntrospectableMCPHandle", "ToolIntrospectableMCPHandle",
    "RuntimePlugin",
    "StateProbe",
]
