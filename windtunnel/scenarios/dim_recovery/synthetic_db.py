"""Deterministic synthetic back-office ops dataset for dim_recovery.

Reuses the same client/order/product data shape as dim_tool_affordance but
with an extended ORDERS table so pagination scenarios have enough rows to
demonstrate hasMore=true naturally.

Recovery scenarios need:
  1. wrong_tool_then_correct  — client ACC-BLWG-001, order quantity total = 20
  2. bad_arg_then_retry       — client ACC-BLWG-001, stage "Intake" = 12 quantity
  3. empty_result_then_alternate_lookup — client Portland Pickles (ACC-PORT-001),
                                          currently 0 orders (just resolved id matters)
  4. partial_result_then_clarify — ACC-BLWG-001 has 2 order rows (ORD-1001 + ORD-1002)
                                   so limit=1 produces hasMore=true, matchedCount=2
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
]

# ACC-BLWG-001: 2 order rows (ORD-1001=12 + ORD-1002=8 = 20 total)
# Critical for partial_result_then_clarify: limit=1 returns ORD-1001 with hasMore=true
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
    # ACC-CHIC-001: distraction data
    {
        "orderId": "ORD-4001",
        "sku": "B005EEE",
        "itemName": "Cubs World Series Pennant",
        "client": "ACC-CHIC-001",
        "clientName": "Chicago Cubs",
        "stage": "Storage",
        "quantity": 100,
        "workQueue": "Q-STORAGE-3",
        "batchLocation": "BATCH-2026-06",
    },
]

# Portland Pickles has NO orders — empty_result_then_alternate_lookup relies on this.
# The model gets 0 results with the raw name, must resolve to ACC-PORT-001,
# then gets 0 results again (correctly) — proving it recovered by trying the
# right approach, not by finding hidden data.

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

# Valid stage enum values for ops_order_query.
# bad_arg_then_retry scenario injects "Incoming" (invalid) as the bad arg.
VALID_STAGES = [
    "Intake",
    "Checked In",
    "Storage",
    "Client Outbound",
    "Shipped",
]


def find_clients(
    query: str = "",
    client_status: str | None = None,
    exclude_archived: bool = False,
) -> list[dict[str, Any]]:
    """LENIENT match: case-insensitive substring against id OR name."""
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
    """STRICT: match client by exact id only (case-insensitive)."""
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
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """STRICT: `client` must be an exact client id.

    Passing a raw name (e.g. "Portland Pickles") returns [].
    stage must be one of VALID_STAGES; invalid value returns [].
    """
    # Stage validation — invalid enum → empty result (schema-rejection behavior)
    if stage is not None and stage not in VALID_STAGES:
        return []

    target_id = _exact_client_id(client) if client else None
    if client and target_id is None:
        return []  # Raw name → no match

    out = []
    for o in ORDERS:
        if target_id and o["client"] != target_id:
            continue
        if sku and o["sku"] != sku:
            continue
        if stage and o["stage"] != stage:
            continue
        out.append(o)
    return out[offset: offset + limit]


def order_report(
    sku: str | None = None,
    client: str | None = None,
) -> dict[str, dict[str, int]]:
    """STRICT: `client` must be an exact client id. Returns all-zeros for raw name."""
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
