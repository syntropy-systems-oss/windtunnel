"""Private evidence-state markers shared by the runner and scorers.

``Trace.mcp_calls == []`` cannot say whether a logging server witnessed zero
calls or whether no server evidence existed.  These markers preserve that
distinction in the existing ``worker_warnings`` metadata channel without
changing the public Trace shape.
"""
from __future__ import annotations

from typing import Literal

MCP_EVIDENCE_AVAILABLE = "mcp_evidence: available"
MCP_EVIDENCE_UNAVAILABLE_PREFIX = "mcp_evidence: unavailable"

MCPEvidenceState = Literal["available", "unavailable", "absent"]


def mcp_evidence_state(worker_warnings: list[str]) -> MCPEvidenceState:
    """Return the persisted MCP evidence state for a trace.

    Old traces carry neither marker and retain the historical transcript
    fallback.  Unavailability wins over availability defensively if a malformed
    trace contains both markers.
    """
    if any(warning.startswith(MCP_EVIDENCE_UNAVAILABLE_PREFIX) for warning in worker_warnings):
        return "unavailable"
    if MCP_EVIDENCE_AVAILABLE in worker_warnings:
        return "available"
    return "absent"
