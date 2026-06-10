"""Mock back-office ops MCP server for the dim_sampler_sensitivity eval dimension.

Tools exposed (matching the three scenarios in scenarios.py):
  ops_client_lookup     — lenient search by name or id; used by typo_recovery
                          and multi_step_followup
  ops_order_report      — strict client-id fetch; used by comparison_which_has_more

Tool descriptions deliberately match the implementation contracts so that
model failures are attributable to sampler variance, not description mismatch.
Same contract rule as dim_tool_affordance: description must match behaviour.

Run locally:
    pip install "mcp[cli]>=1.0"
    python server.py

Run in docker on the runtime driver's compose network (see runner.py):
    docker run -d \\
        --network <runtime-compose-network> \\
        --name mock-ops-mcp-sampler-sensitivity \\
        -p 8080:8080 \\
        mock-ops-mcp-sampler-sensitivity:latest
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from synthetic_db import (  # type: ignore[import-not-found]
    find_clients,
    order_report,
)

mcp = FastMCP("windtunnel")
TOOL_PREFIX = os.environ.get("TOOL_PREFIX", "ops_")


def _envelope(
    inner: Any,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
) -> dict[str, Any]:
    """Production ops-suite response envelope: result is a JSON string, not a nested dict."""
    out: dict[str, Any] = {"result": json.dumps(inner)}
    if columns is not None and rows is not None:
        out["structuredContent"] = {
            "columns": columns,
            "rows": rows,
            "schemaVersion": 1,
        }
    return out


# ─── 1. ops_client_lookup — LENIENT ──────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}client_lookup",
    description=(
        "Search the client directory by name or client id. "
        "All parameters optional; call with {} to list all clients. "
        "When `query` is provided, performs a case-insensitive substring match "
        "against both the client id AND the client name — so partial names like "
        "'Bluewing' or 'Logistics' work. Returns client records including id, name, "
        "status, clientContactName, clientEmail, and clientPhone. "
        "Use this tool to resolve a client name to an id before calling "
        "ops_order_report, which requires an exact id."
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
            "No clients matched your filter. Try a shorter query or different "
            "clientStatus."
            if not matches
            else f"Found {len(matches)} client(s) matching the filter."
        ),
    }
    columns = ["id", "name", "status", "clientContactName", "clientEmail", "clientPhone"]
    rows = [[c.get(k, "") for k in columns] for c in sliced]
    return _envelope(inner, columns, rows)


# ─── 2. ops_order_report — STRICT ────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_report",
    description=(
        "Look up order totals by client id. "
        "Returns a summary of order counts grouped by workflow stage "
        "(Intake, Checked In, Storage, Client Outbound, Shipped). "
        "IMPORTANT: the `client` parameter requires an EXACT client id "
        "(e.g. 'ACC-BLWG-001') — passing a client name will return all-zero counts. "
        "Use ops_client_lookup first to resolve a client name to its id."
    ),
)
def ops_order_report(
    client: str | None = None,
    sku: str | None = None,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    by_stage = order_report(client=client, sku=sku)
    total_qty = sum(b["quantity"] for b in by_stage.values())
    total_count = sum(b["count"] for b in by_stage.values())
    inner = {
        "filter": {"client": client, "sku": sku},
        "byStage": by_stage,
        "total": {"count": total_count, "quantity": total_qty},
        "note": (
            "All stages show zero. If you passed a client name, use "
            "ops_client_lookup to get the exact client id and retry."
            if total_qty == 0
            else f"Total quantity {total_qty} across all stages."
        ),
    }
    columns = ["stage", "count", "quantity"]
    rows = [[s, b["count"], b["quantity"]] for s, b in by_stage.items()]
    return _envelope(inner, columns, rows)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("MOCK_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_MCP_PORT", "8080"))
    print(
        f"[mock-ops-mcp-sampler-sensitivity] starting streamable HTTP on {host}:{port}/mcp",
        flush=True,
    )
    mcp.settings.host = host
    mcp.settings.port = port
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    mcp.run(transport="streamable-http")
