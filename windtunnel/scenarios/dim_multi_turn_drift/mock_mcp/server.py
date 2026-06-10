"""Mock ops-suite MCP server for the dim_multi_turn_drift eval dimension.

Tools exposed (same surface as dim_tool_affordance — context tracking is the
variable being tested, not exotic tool discovery):
  ops_client_lookup      — lenient search by name or id; returns clientEmail
  ops_order_query        — strict fetch by exact client id
  ops_order_report       — strict fetch; returns stage-by-stage summary

CRITICAL CONTRACT RULE (carried forward from dim_tool_affordance):
  The tool DESCRIPTION must match the implementation behaviour exactly.
  Description says "search by name or id" → lenient ✓
  Description says "requires exact client id" → strict ✓

This server uses the dim_multi_turn_drift synthetic_db which has:
  ACC-BLWG-001  Bluewing Logistics  —  20 orders (below 50 threshold)
  ACC-BLWG-002  Bluewing Concessions    —   3 orders (below 50 threshold)
  ACC-PORT-001  Portland Pickles               —  75 orders (above 50 threshold)
  ACC-CHIC-001  Chicago Cubs                   — 100 orders (above 50 threshold)

Run locally (for testing):
    pip install "mcp[cli]>=1.0"
    python server.py
    # listens on http://0.0.0.0:8084/mcp

Run in docker on the runtime driver's compose network (see runner.py):
    docker run -d \\
        --network <runtime-compose-network> \\
        --name mock-ops-mcp-multi-turn-drift \\
        -p 8084:8084 \\
        mock-ops-mcp-multi-turn-drift:latest
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

# Add parent dir so we can import synthetic_db whether run directly or via docker
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from synthetic_db import (  # type: ignore[import-not-found]
    find_clients,
    order_total,
    query_orders,
)

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


# ─── 1. ops_client_lookup — LENIENT ──────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}client_lookup",
    description=(
        "Search the client directory by name or client id. "
        "All parameters optional; call with {} to list all clients. "
        "When `query` is provided, performs a case-insensitive substring match "
        "against both the client id AND the client name — so partial names like "
        "'Bluewing' or 'Portland' work. Returns client records including id, name, "
        "status, clientContactName, clientEmail, and clientPhone. "
        "Also returns the client's total open order count (orderTotal). "
        "Use ops_client_lookup first to resolve a client name to an id before "
        "calling ops_order_query, which requires an exact id."
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
    # Annotate each match with its order total
    annotated = []
    for c in matches:
        row = dict(c)
        row["orderTotal"] = order_total(c["id"])
        annotated.append(row)

    sliced = annotated[offset: offset + limit]
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
    columns = ["id", "name", "status", "clientContactName", "clientEmail",
               "clientPhone", "orderTotal"]
    rows = [[c.get(k, "") for k in columns] for c in sliced]
    return _envelope(inner, columns, rows)


# ─── 2. ops_order_query — STRICT ─────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_query",
    description=(
        "Query order records with flexible filters. All parameters optional. "
        "IMPORTANT: the `client` parameter requires an EXACT client id "
        "(e.g. 'ACC-PORT-001') — passing a client name will return zero results. "
        "Use ops_client_lookup first to resolve a client name to its id. "
        "Supports filtering by client id, SKU, and work queue stage. "
        "Returns order records with quantity, stage, work queue, and item details."
    ),
)
def ops_order_query(
    client: str | None = None,
    sku: str | None = None,
    stage: str | None = None,
    limit: int = 50,
    offset: int = 0,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    matched = query_orders(client=client, sku=sku, stage=stage)
    sliced = matched[offset: offset + limit]
    total_qty = sum(o["quantity"] for o in matched)
    inner = {
        "filter": {"sku": sku, "client": client, "stage": stage},
        "summary": {"total": {"count": len(matched), "quantity": total_qty}},
        "orders": sliced,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(sliced),
            "matchedCount": len(matched),
            "hasMore": (offset + limit) < len(matched),
        },
        "note": (
            "No orders matched. If you passed a client name instead of an id, "
            "use ops_client_lookup first to get the exact client id."
            if not matched
            else f"Matched {len(matched)} order row(s), total {total_qty} orders."
        ),
    }
    columns = ["orderId", "sku", "itemName", "stage", "quantity", "clientName"]
    rows = [[o.get(k, "") for k in columns] for o in sliced]
    return _envelope(inner, columns, rows)


# ─── 3. ops_order_report — STRICT ────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_report",
    description=(
        "Look up order counts by client and/or SKU. "
        "Returns a summary of order counts grouped by work queue stage. "
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
    matched = query_orders(client=client, sku=sku)
    stages: dict[str, dict[str, int]] = {}
    for o in matched:
        s = stages.setdefault(o["stage"], {"count": 0, "quantity": 0})
        s["count"] += 1
        s["quantity"] += o["quantity"]
    total_qty = sum(s["quantity"] for s in stages.values())
    inner = {
        "filter": {"client": client, "sku": sku},
        "byStage": stages,
        "total": {"quantity": total_qty},
        "note": (
            "All stages show zero. If you passed a client name, use "
            "ops_client_lookup to get the exact client id and retry."
            if total_qty == 0
            else f"Total {total_qty} order(s) across all stages."
        ),
    }
    columns = ["stage", "count", "quantity"]
    rows = [[s, v["count"], v["quantity"]] for s, v in stages.items()]
    return _envelope(inner, columns, rows)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("MOCK_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_MCP_PORT", "8084"))
    print(
        f"[mock-ops-mcp-multi-turn-drift] starting streamable HTTP on {host}:{port}/mcp",
        flush=True,
    )
    mcp.settings.host = host
    mcp.settings.port = port
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    mcp.run(transport="streamable-http")
