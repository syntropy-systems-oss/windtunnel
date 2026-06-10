"""Deterministic synthetic back-office ops dataset for the dim_tool_affordance eval.

STRICT / LENIENT SPLIT — the core mechanic:
  ops_client_lookup  →  LENIENT (substring search by name OR exact id)
  ops_order_query    →  STRICT  (exact client id only — returns [] for raw name)
  ops_order_report   →  STRICT  (exact client id only — returns zeros for raw name)
  ops_product_lookup →  by SKU (no client dimension)

This split FORCES the model to chain: lookup-by-name → get id → query-by-id.
Without it, lenient-everywhere lets the model one-shot queries and the
lookup_before_action and field_scope_inference scenarios become trivial.

CRITICAL CONTRACT RULE: the tool DESCRIPTION in mock_mcp/server.py
MUST match the behaviour here. If the description says "search by name"
but the impl requires exact id, the model follows the description and
gets failures that are the bench's fault, not the model's.

Tool→description contract:
  ops_client_lookup:  "search by name or id" → lenient OK
  ops_order_query:    "requires exact client id" → strict OK
  ops_order_report:   "requires exact client id" → strict OK
  ops_product_lookup: "look up by SKU" → by sku OK

Calibrated for 3 dim_tool_affordance scenarios:

  1. lookup_before_action
     "How many items does Bluewing Logistics have on order?"
     Pass = client_lookup("Bluewing Logistics") → ACC-BLWG-001
            → order_query(client="ACC-BLWG-001") → 20 items

  2. field_scope_inference
     "What's the contact email for Bluewing Logistics?"
     Pass = client_lookup("Bluewing Logistics")
            → clientEmail = ops@bluewing.example

  3. wrong_tool_correction
     "Show me catalog info for SKU B001AAA"
     Pass = product_lookup(sku="B001AAA")
            → name="Bluewing Jersey - Home", brand="BluewingGear"
     Fail = order_report or order_query (returns order counts, not catalog)
"""
from __future__ import annotations

from typing import Any

CLIENTS: list[dict[str, Any]] = [
    {
        "id": "ACC-BLWG-001",
        "name": "Bluewing Logistics",
        "status": "active",
        "clientContactName": "Joe Marsh",
        "clientEmail": "ops@bluewing.example",
        "clientPhone": "+1-555-0101",
        "archived": False,
    },
    {
        "id": "ACC-BLWG-002",
        "name": "Bluewing Concessions",
        "status": "active",
        "clientContactName": "Jane Concession",
        "clientEmail": "accounts@bluewing.example",
        "clientPhone": "+1-555-0102",
        "archived": False,
    },
    {
        "id": "ACC-PORT-001",
        "name": "Portland Pickles",
        "status": "active",
        "clientContactName": "Pete Pickle",
        "clientEmail": "pete@portlandpickles.example",
        "clientPhone": "+1-555-0201",
        "archived": False,
    },
    {
        "id": "ACC-CHIC-001",
        "name": "Chicago Cubs",
        "status": "active",
        "clientContactName": "Ron Bleacher",
        "clientEmail": "ron@cubs.example",
        "clientPhone": "+1-555-0301",
        "archived": False,
    },
    {
        # BULK client (unique name, no Bluewing-style ambiguity). Has many SKUs in
        # Storage so "list every SKU + quantity" is a genuine large table — the
        # case that should be delivered via deliver_as="csv", not a markdown table.
        "id": "ACC-STLH-001",
        "name": "Seattle Steelheads",
        "status": "active",
        "clientContactName": "Sam Steel",
        "clientEmail": "sam@steelheads.example",
        "clientPhone": "+1-555-0401",
        "archived": False,
    },
]

# ACC-BLWG-001: 20 items total (12 + 8) in Intake
# Used by lookup_before_action: raw-name query returns 0, id-query returns 20
ORDERS: list[dict[str, Any]] = [
    {
        "orderId": "ORD-1001",
        "sku": "B001AAA",
        "itemName": "Bluewing Jersey - Home",
        "client": "ACC-BLWG-001",
        "clientName": "Bluewing Logistics",
        "stage": "Intake",
        "quantity": 12,
        "workQueue": "Q-INTAKE-1",
        "batchLocation": "BATCH-2026-04",
    },
    {
        "orderId": "ORD-1002",
        "sku": "B002BBB",
        "itemName": "Bluewing Cap - Adjustable",
        "client": "ACC-BLWG-001",
        "clientName": "Bluewing Logistics",
        "stage": "Intake",
        "quantity": 8,
        "workQueue": "Q-INTAKE-2",
        "batchLocation": "BATCH-2026-04",
    },
    # ACC-BLWG-002: small stack in Storage
    {
        "orderId": "ORD-2001",
        "sku": "B001AAA",
        "itemName": "Bluewing Jersey - Home",
        "client": "ACC-BLWG-002",
        "clientName": "Bluewing Concessions",
        "stage": "Storage",
        "quantity": 3,
        "workQueue": "Q-STORAGE-1",
        "batchLocation": "BATCH-2026-05",
    },
    # ACC-CHIC-001: large stack — distraction data
    {
        "orderId": "ORD-4001",
        "sku": "B005EEE",
        "itemName": "Cubs World Series Pennant",
        "client": "ACC-CHIC-001",
        "clientName": "Chicago Cubs",
        "stage": "Storage",
        "quantity": 100,
        "workQueue": "Q-STORAGE-2",
        "batchLocation": "BATCH-2026-06",
    },
]

# ─── ACC-STLH-001 (Seattle Steelheads): a BULK Storage stack ─────────────────
# 20 distinct SKUs in Storage so bulk_table_to_csv's "list every SKU +
# quantity in Storage" returns a genuinely large table — the case that should be
# delivered via deliver_as="csv", not an unrenderable markdown table. Generated
# deterministically (index-derived SKU/qty, no randomness) so runs stay stable.
_STLH_ITEMS = [
    "Steelheads Home Jersey", "Steelheads Away Jersey", "Steelheads Cap - Fitted",
    "Steelheads Cap - Snapback", "Steelheads Beanie", "Steelheads Hoodie - Navy",
    "Steelheads Hoodie - Gray", "Steelheads Tee - Logo", "Steelheads Tee - Retro",
    "Steelheads Socks", "Steelheads Scarf", "Steelheads Foam Finger",
    "Steelheads Pennant", "Steelheads Mini Bat", "Steelheads Bobblehead",
    "Steelheads Travel Mug", "Steelheads Water Bottle", "Steelheads Keychain",
    "Steelheads Sticker Pack", "Steelheads Tote Bag",
]
for _i, _name in enumerate(_STLH_ITEMS):
    ORDERS.append({
        "orderId": f"ORD-STL-{3001 + _i}",
        "sku": f"B2{_i:02d}STL",
        "itemName": _name,
        "client": "ACC-STLH-001",
        "clientName": "Seattle Steelheads",
        "stage": "Storage",
        "quantity": 5 + (_i * 7) % 43,
        "workQueue": f"Q-STORAGE-{_i:02d}",
        "batchLocation": "BATCH-2026-06",
    })

# Products keyed by SKU — used by wrong_tool_correction scenario.
# product_lookup returns catalog metadata (name, description, brand),
# NOT order records or counts. This is the distinction the scenario tests.
PRODUCTS: dict[str, dict[str, Any]] = {
    "B001AAA": {
        "sku": "B001AAA",
        "name": "Bluewing Jersey - Home",
        "description": "Official home jersey, machine washable.",
        "brand": "BluewingGear",
        "distributor": "Mike's Apparel",
        "bundle": False,
    },
    "B002BBB": {
        "sku": "B002BBB",
        "name": "Bluewing Cap - Adjustable",
        "description": "Adjustable strapback cap with team logo.",
        "brand": "BluewingGear",
        "distributor": "Mike's Apparel",
        "bundle": False,
    },
}


def find_clients(
    query: str = "",
    client_status: str | None = None,
    exclude_archived: bool = False,
) -> list[dict[str, Any]]:
    """LENIENT match: case-insensitive substring against id OR name.

    This is the 'find' tool — the model uses it to get the id it then
    passes to the strict fetch tools.
    """
    q = (query or "").strip().lower()
    out = []
    for c in CLIENTS:
        if exclude_archived and c.get("archived"):
            continue
        if client_status and c.get("status") != client_status:
            continue
        if q:
            hay = f"{c['id']} {c['name']}".lower()
            if q not in hay:
                continue
        out.append(c)
    return out


def _exact_client_id(query: str) -> str | None:
    """STRICT: match client by exact id only (case-insensitive).

    Used by query_orders() and order_report() — they are 'fetch' tools.
    If the caller passes a raw name, this returns None → zero results,
    forcing the model to chain through client_lookup first.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    for c in CLIENTS:
        if c["id"].lower() == q:
            return c["id"]
    return None


def query_orders(
    client: str | None = None,
    sku: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    """STRICT: `client` must be an exact client id (use find_clients first).

    Passing a raw name (e.g. "Bluewing Logistics") returns [].
    This is intentional — it forces the lookup→fetch chaining pattern.
    """
    target_id = _exact_client_id(client) if client else None
    if client and target_id is None:
        return []  # Raw name → no match → model must look up id first
    out = []
    for o in ORDERS:
        if target_id and o["client"] != target_id:
            continue
        if sku and o["sku"] != sku:
            continue
        if stage and o["stage"] != stage:
            continue
        out.append(o)
    return out


def order_report(
    sku: str | None = None,
    client: str | None = None,
) -> dict[str, dict[str, int]]:
    """STRICT: `client` must be an exact client id.

    Returns all-zeros for a raw name — same chaining requirement as query_orders.
    """
    stages = ["Intake", "Checked In", "Storage", "Client Outbound", "Shipped"]
    by_stage: dict[str, dict[str, int]] = {s: {"count": 0, "quantity": 0} for s in stages}
    target_id = _exact_client_id(client) if client else None
    if client and target_id is None:
        return by_stage  # All zeros — forces lookup-first
    for o in ORDERS:
        if target_id and o["client"] != target_id:
            continue
        if sku and o["sku"] != sku:
            continue
        if o["stage"] in by_stage:
            by_stage[o["stage"]]["count"] += 1
            by_stage[o["stage"]]["quantity"] += o["quantity"]
    return by_stage
