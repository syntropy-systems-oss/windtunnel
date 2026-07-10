"""Runtime evidence collection and evidence-surface normalization."""
from __future__ import annotations

import json
from typing import Any

from windtunnel.api._evidence import (
    MCP_EVIDENCE_AVAILABLE,
    MCP_EVIDENCE_UNAVAILABLE_PREFIX,
)
from windtunnel.api.trace import Hash, compute_hash
from windtunnel.spi.agent_runtime import AgentHandle
from windtunnel.spi.mcp_server import MCPHandle
from windtunnel.spi.state_probe import StateProbe


def collect_mcp_evidence(
    mcp_handles: list[MCPHandle],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drain every MCP call log into trace-serializable dictionaries."""
    calls: list[dict[str, Any]] = []
    warnings: list[str] = []
    unavailable: list[str] = []
    for mcp in mcp_handles:
        try:
            log = mcp.call_log()
        except Exception as exc:
            unavailable.append(f"{mcp_handle_label(mcp)}: {type(exc).__name__}: {exc}")
            continue
        for call in log:
            result = call.result
            try:
                json.dumps(result)
            except (TypeError, ValueError):
                result = repr(result)
            call_dict = {
                "tool_name": call.tool_name,
                "args": call.args,
                "result": result,
                "timestamp_ms": call.timestamp_ms,
            }
            if call.extra:
                extra = call.extra
                try:
                    json.dumps(extra)
                except (TypeError, ValueError):
                    extra = json.loads(json.dumps(extra, default=repr))
                call_dict["extra"] = extra
                divergence = call.extra.get("divergence")
                if isinstance(divergence, dict):
                    policy = divergence.get("policy")
                    if isinstance(policy, str):
                        warnings.append(
                            f"universe_divergence: tool={call.tool_name} policy={policy}"
                        )
            calls.append(call_dict)
    calls.sort(key=lambda call: call.get("timestamp_ms") or 0.0)
    if unavailable:
        warnings.append(
            f"{MCP_EVIDENCE_UNAVAILABLE_PREFIX} ({'; '.join(unavailable)})"
        )
    elif mcp_handles:
        warnings.append(MCP_EVIDENCE_AVAILABLE)
    return calls, warnings


def mcp_handle_label(mcp: MCPHandle) -> str:
    """Return a bounded diagnostic label without trusting handle properties."""
    try:
        url = mcp.url
    except Exception:
        url = None
    return str(url or type(mcp).__name__)


def capture_observations(
    state_probe: StateProbe | None,
) -> tuple[dict[str, Any], list[str]]:
    """Capture a JSON-safe external-state snapshot and diagnostic warnings."""
    if state_probe is None:
        return {}, []
    try:
        observations = state_probe.capture()
    except Exception as exc:
        return {}, [f"probe_error: capture failed: {exc}"]
    if not isinstance(observations, dict):
        return {}, [
            f"probe_error: capture() returned {type(observations).__name__}, expected dict",
        ]
    try:
        json.dumps(observations)
    except (TypeError, ValueError):
        try:
            observations = json.loads(json.dumps(observations, default=repr))
        except (TypeError, ValueError) as exc:
            return {}, [f"probe_error: snapshot not JSON-serializable: {exc}"]
    return observations, []


def tool_schema_hash(mcp_handles: list[MCPHandle]) -> Hash | None:
    """Hash the complete offered tool surface, or return None when unknown."""
    entries: list[dict[str, Any]] = []
    try:
        for mcp in mcp_handles:
            definitions = getattr(mcp, "served_tool_definitions", None)
            if callable(definitions):
                entries.extend(definitions())
                continue
            names = getattr(mcp, "served_tools", None)
            if callable(names):
                entries.extend({"name": name} for name in names())
                continue
            return None
        return compute_hash(json.dumps(entries, sort_keys=True, ensure_ascii=False))
    except Exception:
        return None


def capture_surface(handle: AgentHandle) -> tuple[dict[str, Any] | None, list[str]]:
    """Probe a prompt surface and normalize absence or invalid responses."""
    describe = getattr(handle, "describe_surface", None)
    if not callable(describe):
        return None, []
    try:
        block = describe()
    except Exception as exc:
        detail = f"describe_surface raised: {exc}"
        return {"status": "invalid", "detail": detail}, [f"surface_invalid: {detail}"]
    if not isinstance(block, dict) or not isinstance(block.get("status"), str):
        detail = "describe_surface returned a non-conforming block"
        return {"status": "invalid", "detail": detail}, [f"surface_invalid: {detail}"]
    if block["status"] == "invalid":
        return block, [f"surface_invalid: {block.get('detail') or 'unspecified'}"]
    return block, []
