"""Tests for SPI Protocol definitions — agent_runtime.py + mcp_server.py.

Covers:
  - AgentRuntime Protocol with provision(), AgentHandle with send/reset_state/teardown
  - AgentConfig dataclass with all required fields
  - MCPServer Protocol with start()/stop(), MCPHandle with url/call_log/reset/configure
  - MCPCall dataclass with tool_name, args, result, timestamp_ms

These tests verify the SPI shape, not runtime behavior (that's conformance).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from windtunnel.spi.agent_runtime import (
    AgentConfig,
    AgentHandle,
    AgentRuntime,
    MCPSpec,
    Message,
    ModelSpec,
    Response,
    RunnerMCPConfigurableRuntime,
    SamplingConfig,
    SurfaceIntrospectableAgentHandle,
)
from windtunnel.spi.mcp_server import (
    FailureInjectableMCPHandle,
    MCPCall,
    MCPHandle,
    MCPServer,
    ToolDefinitionIntrospectableMCPHandle,
    ToolIntrospectableMCPHandle,
)

# ─── AgentConfig ─────────────────────────────────────────────────────────────

class TestAgentConfig:
    def test_defaults(self) -> None:
        cfg = AgentConfig()
        assert cfg.agent_id == "agent"
        assert cfg.variant_id == "default"
        assert cfg.system_prompt is None
        assert cfg.persona_doc is None
        assert cfg.skills == []
        assert cfg.mcp_servers == []
        assert cfg.model is None
        assert cfg.sampling is None

    def test_full_construction(self) -> None:
        spec = MCPSpec(name="acme", url="http://localhost:8080/mcp")
        model = ModelSpec(name="test-model", quant="4bit")
        sampling = SamplingConfig(temperature=0.7, top_p=0.95, max_tokens=512)
        cfg = AgentConfig(
            agent_id="eval-agent",
            variant_id="prod_v1",
            system_prompt="Be helpful.",
            persona_doc=Path("/tmp/SOUL.md"),
            skills=[Path("/tmp/skill.md")],
            mcp_servers=[spec],
            model=model,
            sampling=sampling,
        )
        assert cfg.agent_id == "eval-agent"
        assert cfg.variant_id == "prod_v1"
        assert cfg.system_prompt == "Be helpful."
        assert cfg.persona_doc == Path("/tmp/SOUL.md")
        assert cfg.skills == [Path("/tmp/skill.md")]
        assert cfg.mcp_servers == [spec]
        assert cfg.model == model
        assert cfg.sampling == sampling

    def test_dataclass_replace(self) -> None:
        """dataclasses.replace() must work for run_matrix cell cloning."""
        base = AgentConfig(agent_id="x", variant_id="v1")
        clone = dataclasses.replace(base, variant_id="v2")
        assert clone.agent_id == "x"
        assert clone.variant_id == "v2"
        assert base.variant_id == "v1"  # original unchanged


class TestModelSpec:
    def test_defaults(self) -> None:
        m = ModelSpec(name="test-model")
        assert m.quant == "unknown"

    def test_explicit_quant(self) -> None:
        m = ModelSpec(name="other-model", quant="4bit")
        assert m.quant == "4bit"


class TestSamplingConfig:
    def test_all_none_by_default(self) -> None:
        s = SamplingConfig()
        assert s.temperature is None
        assert s.top_p is None
        assert s.tool_choice is None
        assert s.max_tokens is None

    def test_explicit_values(self) -> None:
        s = SamplingConfig(temperature=0.0, top_p=1.0, tool_choice="auto", max_tokens=2048)
        assert s.temperature == 0.0
        assert s.top_p == 1.0
        assert s.tool_choice == "auto"
        assert s.max_tokens == 2048

    @pytest.mark.parametrize("temperature", [-0.1, 2.1])
    def test_temperature_range_is_enforced(self, temperature: float) -> None:
        with pytest.raises(ValueError, match="temperature"):
            SamplingConfig(temperature=temperature)

    @pytest.mark.parametrize("top_p", [0.0, -0.1, 1.1])
    def test_top_p_range_is_enforced(self, top_p: float) -> None:
        with pytest.raises(ValueError, match="top_p"):
            SamplingConfig(top_p=top_p)

    @pytest.mark.parametrize("max_tokens", [0, -1])
    def test_token_budget_must_be_positive(self, max_tokens: int) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            SamplingConfig(max_tokens=max_tokens)

    def test_tool_choice_must_not_be_blank(self) -> None:
        with pytest.raises(ValueError, match="tool_choice"):
            SamplingConfig(tool_choice="  ")


class TestMCPSpec:
    def test_construction(self) -> None:
        spec = MCPSpec(name="acme", url="http://localhost:8080/mcp")
        assert spec.name == "acme"
        assert spec.url == "http://localhost:8080/mcp"

    def test_all_spi_import_paths_share_one_class(self) -> None:
        from windtunnel.spi.agent_runtime import MCPSpec as AgentMCPSpec
        from windtunnel.spi.mcp_server import MCPSpec as ServerMCPSpec

        assert MCPSpec is AgentMCPSpec is ServerMCPSpec


# ─── AgentRuntime / AgentHandle Protocol ──────────────────────────────────────

class _ConcreteHandle:
    """Minimal concrete AgentHandle for protocol checking."""

    def send(self, messages: list[Message], session_id: str) -> Response:
        return {"choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": []}}]}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class _ConcreteRuntime:
    """Minimal concrete AgentRuntime for protocol checking."""

    def provision(self, config: AgentConfig) -> AgentHandle:
        return _ConcreteHandle()  # type: ignore[return-value]


class TestAgentRuntimeProtocol:
    def test_concrete_handle_satisfies_protocol(self) -> None:
        handle = _ConcreteHandle()
        assert isinstance(handle, AgentHandle)

    def test_concrete_runtime_satisfies_protocol(self) -> None:
        runtime = _ConcreteRuntime()
        assert isinstance(runtime, AgentRuntime)

    def test_provision_returns_handle(self) -> None:
        runtime = _ConcreteRuntime()
        handle = runtime.provision(AgentConfig())
        assert isinstance(handle, AgentHandle)

    def test_send_returns_response_dict(self) -> None:
        handle = _ConcreteHandle()
        resp = handle.send([{"role": "user", "content": "hello"}], "session-1")
        assert "choices" in resp
        assert resp["choices"][0]["message"]["role"] == "assistant"

    def test_send_accepts_session_id(self) -> None:
        """session_id is explicit — the SPI threads it, not the scenario."""
        handle = _ConcreteHandle()
        # Same session_id across turns (multi-turn contract)
        sid = "fixed-session-id"
        r1 = handle.send([{"role": "user", "content": "turn 1"}], sid)
        r2 = handle.send([{"role": "user", "content": "turn 1"},
                          {"role": "assistant", "content": "ok"},
                          {"role": "user", "content": "turn 2"}], sid)
        assert r1 is not None
        assert r2 is not None

    def test_reset_state_is_callable(self) -> None:
        handle = _ConcreteHandle()
        handle.reset_state()  # must not raise

    def test_teardown_is_idempotent(self) -> None:
        handle = _ConcreteHandle()
        handle.teardown()
        handle.teardown()  # second call must not raise


# ─── MCPCall ─────────────────────────────────────────────────────────────────

class TestMCPCall:
    def test_construction(self) -> None:
        call = MCPCall(
            tool_name="ops_client_lookup",
            args={"query": "Acme"},
            result={"result": "[]"},
            timestamp_ms=1_000_000.0,
        )
        assert call.tool_name == "ops_client_lookup"
        assert call.args == {"query": "Acme"}
        assert call.result == {"result": "[]"}
        assert call.timestamp_ms == 1_000_000.0

    def test_extra_defaults_empty(self) -> None:
        call = MCPCall(tool_name="t", args={}, result=None, timestamp_ms=0.0)
        assert call.extra == {}

    def test_both_arg_shapes_preserved(self) -> None:
        """Args stored as-received — both OpenAI wire and flat shapes."""
        openai_shape = {
            "function": {"name": "ops_client_lookup", "arguments": '{"query": "Acme"}'}
        }
        flat_shape = {"name": "ops_client_lookup", "args": {"query": "Acme"}}

        call_openai = MCPCall(tool_name="t", args=openai_shape, result=None, timestamp_ms=0.0)
        call_flat = MCPCall(tool_name="t", args=flat_shape, result=None, timestamp_ms=0.0)

        assert "function" in call_openai.args
        assert "args" in call_flat.args


# ─── MCPServer / MCPHandle Protocol ──────────────────────────────────────────

class _ConcreteMCPHandle:
    """Minimal concrete MCPHandle."""

    def __init__(self) -> None:
        self._url = "http://localhost:8080/mcp"
        self._log: list[MCPCall] = []
        self._mode: str | None = None

    @property
    def url(self) -> str:
        return self._url

    def call_log(self) -> list[MCPCall]:
        return list(self._log)

    def reset_call_log(self) -> None:
        self._log.clear()

    def configure_failure_mode(self, mode: str | None) -> None:
        self._mode = mode


class _ConcreteToolIntrospectableMCPHandle(_ConcreteMCPHandle):
    def served_tools(self) -> list[str]:
        return ["client_lookup"]


class _MinimalMCPHandle:
    """Minimal evidence handle without specialized failure injection."""

    @property
    def url(self) -> str:
        return "http://localhost:8080/mcp"

    def call_log(self) -> list[MCPCall]:
        return []

    def reset_call_log(self) -> None:
        pass


class _ConcreteMCPServer:
    """Minimal concrete MCPServer."""

    def start(self) -> MCPHandle:
        return _ConcreteMCPHandle()  # type: ignore[return-value]

    def stop(self) -> None:
        pass


class TestMCPServerProtocol:
    def test_concrete_handle_satisfies_protocol(self) -> None:
        handle = _ConcreteMCPHandle()
        assert isinstance(handle, MCPHandle)
        assert isinstance(handle, FailureInjectableMCPHandle)

    def test_failure_injection_is_not_required_by_minimal_handle(self) -> None:
        handle = _MinimalMCPHandle()
        assert isinstance(handle, MCPHandle)
        assert not isinstance(handle, FailureInjectableMCPHandle)

    def test_optional_protocols_are_exported_from_spi_root(self) -> None:
        from windtunnel import spi

        assert spi.RunnerMCPConfigurableRuntime is RunnerMCPConfigurableRuntime
        assert spi.SurfaceIntrospectableAgentHandle is SurfaceIntrospectableAgentHandle
        assert (
            spi.ToolDefinitionIntrospectableMCPHandle
            is ToolDefinitionIntrospectableMCPHandle
        )
        assert spi.FailureInjectableMCPHandle is FailureInjectableMCPHandle

    def test_served_tools_is_optional_on_mcp_handle(self) -> None:
        handle = _ConcreteMCPHandle()
        assert isinstance(handle, MCPHandle)
        assert not isinstance(handle, ToolIntrospectableMCPHandle)

    def test_tool_introspectable_handle_satisfies_optional_protocol(self) -> None:
        handle = _ConcreteToolIntrospectableMCPHandle()
        assert isinstance(handle, MCPHandle)
        assert isinstance(handle, ToolIntrospectableMCPHandle)
        assert handle.served_tools() == ["client_lookup"]

    def test_concrete_server_satisfies_protocol(self) -> None:
        server = _ConcreteMCPServer()
        assert isinstance(server, MCPServer)

    def test_start_returns_handle(self) -> None:
        server = _ConcreteMCPServer()
        handle = server.start()
        assert isinstance(handle, MCPHandle)

    def test_handle_url_is_string(self) -> None:
        handle = _ConcreteMCPHandle()
        assert isinstance(handle.url, str)

    def test_call_log_empty_initially(self) -> None:
        handle = _ConcreteMCPHandle()
        assert handle.call_log() == []

    def test_reset_call_log_clears(self) -> None:
        handle = _ConcreteMCPHandle()
        handle._log.append(
            MCPCall(tool_name="t", args={}, result=None, timestamp_ms=0.0)
        )
        handle.reset_call_log()
        assert handle.call_log() == []

    def test_configure_failure_mode_none_resets(self) -> None:
        handle = _ConcreteMCPHandle()
        handle.configure_failure_mode("malformed_json")
        handle.configure_failure_mode(None)
        assert handle._mode is None

    def test_stop_is_idempotent(self) -> None:
        server = _ConcreteMCPServer()
        server.stop()
        server.stop()  # second call must not raise


# ─── StateProbe Protocol ──────────────────────────────────────────────────────

class _ConcreteStateProbe:
    """Minimal concrete StateProbe — a dict standing in for a bench fixture."""

    def __init__(self) -> None:
        self._state: dict = {"prs": []}

    def capture(self) -> dict:
        return {"github": dict(self._state)}

    def reset(self) -> None:
        self._state = {"prs": []}


class TestStateProbeProtocol:
    def test_concrete_probe_satisfies_protocol(self) -> None:
        from windtunnel.spi.state_probe import StateProbe
        probe = _ConcreteStateProbe()
        assert isinstance(probe, StateProbe)

    def test_capture_returns_dict(self) -> None:
        probe = _ConcreteStateProbe()
        assert probe.capture() == {"github": {"prs": []}}

    def test_reset_is_idempotent(self) -> None:
        probe = _ConcreteStateProbe()
        probe._state["prs"].append({"base": "main"})
        probe.reset()
        probe.reset()  # second call must not raise
        assert probe.capture() == {"github": {"prs": []}}

    def test_exported_from_spi_package(self) -> None:
        """StateProbe is part of the public SPI surface."""
        from windtunnel.spi import StateProbe
        assert isinstance(_ConcreteStateProbe(), StateProbe)
