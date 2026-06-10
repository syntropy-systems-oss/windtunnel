"""Mock back-office ops MCP server for the dim_tool_affordance eval dimension.

Contains only the tools these 3 scenarios need:
  ops_client_lookup   — lenient search (by name substring or exact id)
  ops_order_query     — strict fetch (exact client id required)
  ops_order_report    — strict fetch (exact client id required)
  ops_product_lookup  — catalog lookup by SKU

CRITICAL CONTRACT RULE:
  The tool DESCRIPTION must match the implementation behaviour exactly.
  If the description advertises lenient name search but the code does
  strict id matching, the model follows the description, gets zero results
  for valid queries, and the failure is the bench's fault — not the model's.

  In this server:
    client_lookup:  description says "search by name or id" → lenient ✓
    order_query:    description says "requires exact client id" → strict ✓
    order_report:   description says "requires exact client id" → strict ✓
    product_lookup: description says "look up by SKU" → by SKU ✓

Run locally (for testing):
    pip install "mcp[cli]>=1.0"
    python server.py
    # listens on http://0.0.0.0:8080/mcp

Run in docker on the runtime driver's compose network (see runner.py):
    docker run -d \\
        --network <runtime-compose-network> \\
        --name mock-ops-mcp-tool-affordance \\
        -p 8091:8080 \\
        mock-ops-mcp-tool-affordance:latest
    # the chat harness config points the platform MCP URL →
    # http://mock-ops-mcp-tool-affordance:8080/mcp

The server name MUST match the chat harness config's mcp_servers alias.
Tools appear to the model as mcp_<server_name>_<tool_name>.
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
    PRODUCTS,
    find_clients,
    order_report,
    query_orders,
)

# ---------------------------------------------------------------------------
# Tool name prefix
#
# The direct-to-mock dim_tool_affordance path points the chat harness straight
# at this mock and expects tool names like `ops_client_lookup`.  That path
# leaves TOOL_PREFIX unset so the default "ops_" applies.
#
# The FAITHFUL path runs this mock as the *upstream* behind the real platform
# MCP gateway.  The gateway prepends its own "ops." integration prefix, so the
# upstream tools must be named without the "ops_" redundancy:
#   upstream: client_lookup → gateway: ops.client_lookup → harness: mcp_acme_ops_client_lookup
# Deployments on that path set TOOL_PREFIX="" on the upstream mock to achieve this.
# ---------------------------------------------------------------------------
TOOL_PREFIX: str = os.environ.get("TOOL_PREFIX", "ops_")

# Server name MUST match the chat harness config's `mcp_servers.<name>`.
# Platforms decorate bare tool names before the model sees them (Acme
# example: `client_lookup` → `mcp_acme_ops_client_lookup`). Scenarios
# declare BARE names; the trajectory evaluator matches decorated variants.
mcp = FastMCP("windtunnel")


def _envelope(
    inner: Any,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
) -> dict[str, Any]:
    """Production response envelope: `result` is a JSON STRING, not a nested dict.

    This is the MCP text+structured hybrid pattern from real production session
    captures. The _envelope() helper enforces it — don't return raw dicts from
    tool handlers.

    Tabular fields (`columns`/`rows`) are placed at the TOP LEVEL of the returned
    dict, NOT under a nested `structuredContent` key. Why: FastMCP serializes a
    tool's entire return dict AS the MCP-protocol `structuredContent` field. A
    nested `{"structuredContent": {...}}` key would therefore land double-nested
    (`structuredContent.structuredContent.columns`), and the real platform
    server's deliver_as=csv path — canonical result JSON → tabular parse — reads
    TOP-LEVEL `columns`/`rows`. Double-nesting makes it reject the result as
    `result_not_tabular`, so the deferred CSV export fails and no file/link is
    produced (observed in a real production failure). Flattening lets the
    tabular parser find the columns/rows (it ignores extra keys like
    `result`/`schemaVersion`), so deliver_as=csv generates a real CSV exactly
    as in production.
    """
    out: dict[str, Any] = {"result": json.dumps(inner)}
    if columns is not None and rows is not None:
        out["columns"] = columns
        out["rows"] = rows
        out["schemaVersion"] = 1
    return out


# ─── 1. client_lookup — LENIENT ──────────────────────────────────────────────

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
    sliced = matches[offset : offset + limit]
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


# ─── 2. order_query — STRICT ─────────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_query",
    description=(
        "Query order records with flexible filters. All parameters optional. "
        "IMPORTANT: the `client` parameter requires an EXACT client id "
        "(e.g. 'ACC-BLWG-001') — passing a client name will return zero results. "
        "Use ops_client_lookup first to resolve a client name to its id. "
        "Supports filtering by client id, SKU, and order stage. "
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
    matched = query_orders(client=client, sku=sku, stage=stage)
    sliced = matched[offset : offset + limit]
    total_qty = sum(o["quantity"] for o in matched)
    buckets: dict[str, dict[str, int]] = {}
    if groupBy == "stage":
        for o in matched:
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
            "total": {"count": len(matched), "quantity": total_qty},
            "bucketsTruncated": False,
        },
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
            else f"Matched {len(matched)} order row(s)."
        ),
    }
    columns = ["orderId", "sku", "itemName", "stage", "quantity", "clientName"]
    rows = [[o.get(k, "") for k in columns] for o in sliced]
    return _envelope(inner, columns, rows)


# ─── 3. order_report — STRICT ────────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}order_report",
    description=(
        "Look up order counts by SKU and/or client. "
        "Returns a summary of order counts grouped by stage "
        "(Intake, Checked In, Storage, Client Outbound, Shipped). "
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
            else f"Total {total_qty} item(s) across all stages."
        ),
    }
    columns = ["stage", "count", "quantity"]
    rows = [[s, b["count"], b["quantity"]] for s, b in by_stage.items()]
    return _envelope(inner, columns, rows)


# ─── 4. product_lookup — by SKU ──────────────────────────────────────────────

@mcp.tool(
    name=f"{TOOL_PREFIX}product_lookup",
    description=(
        "Look up a product in the CATALOG by SKU. "
        "Returns product metadata: name, description, brand, distributor, and "
        "whether it is a bundle. This is CATALOG information — not order "
        "records or counts. For order quantities by stage, use "
        "ops_order_report or ops_order_query instead."
    ),
)
def ops_product_lookup(
    sku: str,
    deliver_as: str | None = None,
) -> dict[str, Any]:
    p = PRODUCTS.get(sku)
    if not p:
        inner = {
            "sku": sku,
            "found": False,
            "note": f"No product found in catalog with SKU {sku}.",
        }
        return _envelope(inner)
    return _envelope({"sku": sku, "found": True, "product": p})


# ─── CSV export is NOT a tool here — it's the `deliver_as="csv"` parameter ────
#
# FIDELITY: production has NO `csv_export` tool. CSV delivery is a
# transport-level `deliver_as` meta-option that the real platform server
# INJECTS into every read-only (CSV-eligible) tool's schema and HANDLES
# itself: on `deliver_as="csv"` it enqueues a deferred export and returns an
# async ack ("I've started the export. The file will arrive as a follow-up
# message…") — the download URL is posted to the thread later by the system,
# NOT returned inline.
#
# This dim runs the FAITHFUL path (chat harness → real platform MCP → this
# upstream mock), so the real platform server does that injection/handling on
# the read-only tools above; this mock just supplies the raw data. Each
# read-only tool keeps a `deliver_as` kwarg only so the rare direct-to-mock
# path doesn't reject the arg — the mock itself does nothing with it.
#
# An earlier version exposed a synchronous `csv_export` tool returning a
# downloadUrl. That tool does NOT exist in production; advertising it gave the
# model an easy export affordance production lacks and MASKED the real failure
# (the agent dumping an unrenderable markdown table because it never reached
# for `deliver_as="csv"`). Removed for fidelity. Export behaviour is now
# exercised via deliver_as in the scenarios: investigate_before_export /
# export_customer_products / bulk_table_to_csv.


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("MOCK_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_MCP_PORT", "8080"))
    print(
        f"[mock-ops-mcp-tool-affordance] starting streamable HTTP on {host}:{port}/mcp",
        flush=True,
    )
    mcp.settings.host = host
    mcp.settings.port = port
    # DNS-rebinding protection disabled: we run on a private Docker bridge network.
    # The chat harness calls us via http://mock-ops-mcp-tool-affordance:8080/mcp —
    # that host header would 421 with the default SDK allowlist (localhost-only).
    # DO NOT copy this pattern to any internet-reachable server.
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    mcp.run(transport="streamable-http")
