"""Mock ops-suite MCP server for the dim_memory_conflict eval dimension.

Contains only ops_client_lookup — the one tool needed for stale_vs_current.

CRITICAL CONTRACT RULE (carried forward from dim_tool_affordance):
  The tool DESCRIPTION must match the implementation behaviour exactly.
  Here: client_lookup is LENIENT (substring by name or id) because the
  memory-conflict dim tests memory preference, not tool chaining mechanics.
  We don't want the model to fail on a lookup discipline issue when the
  dim is testing whether it trusts memory vs. the live tool result.

  The key data point: Bluewing Logistics' clientEmail is outlook.com.
  The seeded memory says Gmail. The model must call this tool and use
  the outlook.com result to pass stale_vs_current.

Run locally (for testing):
    pip install "mcp[cli]>=1.0"
    python server.py
    # listens on http://0.0.0.0:8085/mcp

Run in docker on the runtime driver's compose network (see runner.py):
    docker run -d \\
        --network <runtime-compose-network> \\
        --name mock-ops-mcp-memory-conflict \\
        -p 8085:8085 \\
        mock-ops-mcp-memory-conflict:latest
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from synthetic_db import find_clients  # type: ignore[import-not-found]

# Server name MUST match the agent platform's configured `mcp_servers.<name>`.
# Platforms decorate bare tool names before the model sees them (Acme
# example: `client_lookup` → `mcp_acme_ops_client_lookup`). Scenarios
# declare BARE names; the trajectory evaluator matches decorated variants.
mcp = FastMCP("windtunnel")
TOOL_PREFIX = os.environ.get("TOOL_PREFIX", "ops_")


def _envelope(
    inner: Any,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
) -> dict[str, Any]:
    """Production ops-suite response envelope: `result` is a JSON STRING, not a nested dict."""
    out: dict[str, Any] = {"result": json.dumps(inner)}
    if columns is not None and rows is not None:
        out["structuredContent"] = {
            "columns": columns,
            "rows": rows,
            "schemaVersion": 1,
        }
    return out


# ─── ops_client_lookup — LENIENT ─────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}client_lookup",
    description=(
        "Use this to get a client's CURRENT contact details — email "
        "(clientEmail), phone (clientPhone), and contact name. It is the "
        "AUTHORITATIVE source for a client's email/contact info and overrides "
        "any prior memory or assumption you have about it; when asked what "
        "email or phone a client uses, look it up here rather than answering "
        "from memory. "
        "Search by name or client id (case-insensitive substring on both id AND "
        "name, so partial names like 'Bluewing' or 'Logistics' work). All "
        "parameters optional; call with {} to list all clients. Returns client "
        "records: id, name, status, clientContactName, clientEmail, clientPhone."
    ),
)
def ops_client_lookup(
    query: str = "",
    clientStatus: str | None = None,
    excludeArchived: bool = False,
    limit: int = 10,
    offset: int = 0,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    matches = find_clients(
        query=query,
        client_status=clientStatus,
        exclude_archived=excludeArchived,
    )
    sliced = matches[offset: offset + limit]
    inner = {
        "matches": sliced,
        "filter": {
            "query": query or None,
            "clientStatus": clientStatus,
            "excludeArchived": excludeArchived,
        },
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(sliced),
            "matchedCount": len(matches),
            "hasMore": (offset + limit) < len(matches),
        },
        "note": (
            "No clients matched your filter. Try a shorter query or different clientStatus."
            if not matches
            else f"Found {len(matches)} client(s) matching the filter."
        ),
    }
    columns = ["id", "name", "status", "clientContactName", "clientEmail", "clientPhone"]
    rows = [[c.get(k, "") for k in columns] for c in sliced]
    return _envelope(inner, columns, rows)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("MOCK_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_MCP_PORT", "8085"))
    print(
        f"[mock-ops-mcp-memory-conflict] starting streamable HTTP on {host}:{port}/mcp",
        flush=True,
    )
    mcp.settings.host = host
    mcp.settings.port = port
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    mcp.run(transport="streamable-http")
