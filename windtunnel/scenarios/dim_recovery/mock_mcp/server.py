"""Mock back-office ops MCP server for the dim_recovery eval dimension.

Tools exposed (same surface as dim_tool_affordance but with recovery-specific
behavior tuned in synthetic_db.py):
  ops_client_lookup      — lenient search (by name substring or exact id)
  ops_order_query        — strict fetch (exact client id required; invalid
                           stage enum returns empty — feeds bad_arg_then_retry)
  ops_order_report       — strict fetch (exact client id required)
  ops_product_lookup     — catalog lookup by SKU

Recovery scenarios use these same ops-suite tools. The recovery dim tests what
the model does AFTER a prior turn went wrong — not the tools themselves. The
mock MCP just needs to return realistic data so the model can self-correct.

The tool surface intentionally matches dim_tool_affordance — the dims need the
same ops-suite tools; per-dim copies keep the dims independent, and a separate
mock server instance keeps each dim's eval state isolated.

CRITICAL CONTRACT: tool descriptions must match synthetic_db.py behaviour.
See dim_tool_affordance/mock_mcp/server.py for the pattern.

Run locally:
    pip install "mcp[cli]>=1.0"
    python server.py

Run in docker on the runtime driver's compose network (see runner.py):
    docker run -d \\
        --network <runtime-compose-network> \\
        --name mock-ops-mcp-recovery \\
        -p 8083:8080 \\
        mock-ops-mcp-recovery:latest
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from synthetic_db import (  # type: ignore[import-not-found]
    PRODUCTS,
    VALID_STAGES,
    find_clients,
    order_report,
    query_orders,
)

mcp = FastMCP("windtunnel")
TOOL_PREFIX = os.environ.get("TOOL_PREFIX", "ops_")


def _envelope(
    inner: Any,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
) -> dict[str, Any]:
    """Ops-suite response envelope: result is a JSON string, not a nested dict."""
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
        "'Portland' or 'Bluewing' work. Returns client records including id, name, "
        "status, clientContactName, clientEmail, and clientPhone. "
        "Use this tool to resolve a client name to an id before calling "
        "ops_order_query or ops_order_report, which require an exact id."
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


# ─── 2. ops_order_query — STRICT ─────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_query",
    description=(
        "Query order records with flexible filters. All parameters optional. "
        "IMPORTANT: the `client` parameter requires an EXACT client id "
        "(e.g. 'ACC-BLWG-001') — passing a client name will return zero results. "
        "Use ops_client_lookup first to resolve a client name to its id. "
        f"The `stage` parameter must be one of: {', '.join(VALID_STAGES)}. "
        "An invalid stage value returns zero results. "
        "Supports pagination via `limit` and `offset` parameters — check "
        "`pagination.hasMore` in the response to determine if more pages exist. "
        "Returns order records with quantity, stage, work queue, and item details."
    ),
)
def ops_order_query(
    client: str | None = None,
    sku: str | None = None,
    stage: str | None = None,
    clientStatus: str | None = None,
    excludeArchived: bool = False,
    groupBy: str = "none",
    metric: str = "quantity",
    limit: int = 50,
    offset: int = 0,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    # Validate stage before querying (schema rejection behavior for bad_arg_then_retry)
    if stage is not None and stage not in VALID_STAGES:
        return _envelope({
            "error": (
                f"Invalid value for stage: '{stage}'. "
                f"Valid values: {', '.join(VALID_STAGES)}"
            ),
            "orders": [],
            "summary": {"total": {"count": 0, "quantity": 0}},
        })

    matched_all = query_orders(client=client, sku=sku, stage=stage, limit=999999)
    matched = query_orders(client=client, sku=sku, stage=stage, limit=limit, offset=offset)
    total_qty = sum(o["quantity"] for o in matched_all)

    buckets: dict[str, dict[str, int]] = {}
    if groupBy == "stage":
        for o in matched_all:
            b = buckets.setdefault(o["stage"], {"count": 0, "quantity": 0})
            b["count"] += 1
            b["quantity"] += o["quantity"]

    inner = {
        "filter": {
            "sku": sku,
            "client": client,
            "stage": stage,
            "clientStatus": clientStatus,
            "excludeArchived": excludeArchived,
        },
        "groupBy": groupBy,
        "metric": metric,
        "summary": {
            "by": groupBy if groupBy != "none" else None,
            "buckets": buckets,
            "total": {"count": len(matched_all), "quantity": total_qty},
            "bucketsTruncated": False,
        },
        "orders": matched,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(matched),
            "matchedCount": len(matched_all),
            "hasMore": (offset + limit) < len(matched_all),
        },
        "note": (
            "No orders matched. If you passed a client name instead of an id, "
            "use ops_client_lookup first to get the exact client id."
            if not matched_all
            else (
                f"Matched {len(matched_all)} order row(s). "
                + ("Use offset to page through." if (offset + limit) < len(matched_all) else "")
            ).strip()
        ),
    }
    columns = ["orderId", "sku", "itemName", "stage", "quantity", "clientName"]
    rows = [[o.get(k, "") for k in columns] for o in matched]
    return _envelope(inner, columns, rows)


# ─── 3. ops_order_report — STRICT ────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_report",
    description=(
        "Look up order totals by SKU and/or client. "
        "Returns a summary of order counts grouped by workflow stage. "
        "IMPORTANT: the `client` parameter requires an EXACT client id "
        "(e.g. 'ACC-BLWG-001') — passing a client name will return all-zero counts. "
        "Use ops_client_lookup first to resolve a client name to its id."
    ),
)
def ops_order_report(
    sku: str | None = None,
    client: str | None = None,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    by_stage = order_report(sku=sku, client=client)
    total_qty = sum(b["quantity"] for b in by_stage.values())
    total_count = sum(b["count"] for b in by_stage.values())
    inner = {
        "filter": {"sku": sku, "client": client},
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


# ─── 4. ops_product_lookup — by SKU ──────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}product_lookup",
    description=(
        "Look up a product in the CATALOG by SKU. "
        "Returns product metadata: name, description, brand, distributor, and "
        "whether it is a bundle. This is CATALOG information — not order "
        "counts. For order quantities by stage, use "
        "ops_order_report or ops_order_query instead."
    ),
)
def ops_product_lookup(
    sku: str,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    p = PRODUCTS.get(sku)
    if not p:
        return _envelope({
            "sku": sku,
            "found": False,
            "note": f"No product found in catalog with SKU {sku}.",
        })
    return _envelope({"sku": sku, "found": True, "product": p})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("MOCK_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_MCP_PORT", "8080"))
    print(
        f"[mock-ops-mcp-recovery] starting streamable HTTP on {host}:{port}/mcp",
        flush=True,
    )
    mcp.settings.host = host
    mcp.settings.port = port
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    mcp.run(transport="streamable-http")
