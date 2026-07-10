"""Deterministic synthetic ops-suite dataset for the dim_multi_turn_drift eval.

Calibrated for 3 multi-turn drift scenarios:

  1. constraint_change_mid_flow
     Turn 1: "show me active clients" → all 4 active clients returned
     Turn 3: "actually just the ones with more than 50 open orders"
     Turn 4 must: mention Portland Pickles (75 orders) and Chicago Cubs (100 orders),
              NOT mention Bluewing Logistics (20 orders) or
              Bluewing Concessions (3 orders)

  2. pronoun_resolution
     Turn 1: look up Bluewing Logistics → id=ACC-BLWG-001,
             email=ops@bluewing.example
     Turn 3: "what's their contact email?"
     Turn 4 must: return ops@bluewing.example (not accounts@bluewing.example)

  3. topic_switch_and_return
     Turns 1-2: about Portland Pickles (ACC-PORT-001), B001AAA orders
     Turns 3-4: off-topic weather question (model should decline)
     Turn 5: "what about their B001 orders?" (their = Portland Pickles)
     Must return Portland Pickles' B001AAA count (5 orders), not confuse with
     Bluewing Logistics' B001AAA (12 orders in Intake).

Order totals per client:
  ACC-BLWG-001  Bluewing Logistics  →  20  orders (below 50 threshold)
  ACC-BLWG-002  Bluewing Concessions    →   3  orders (below 50 threshold)
  ACC-PORT-001  Portland Pickles               →  75  orders (above 50 threshold)
  ACC-CHIC-001  Chicago Cubs                   → 100  orders (above 50 threshold)
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

# Order records keyed by client id with order quantities.
#
# ACC-BLWG-001: 20 orders total — BELOW 50 threshold
#   ORD-1001: B001AAA  12 orders  Intake
#   ORD-1002: B002BBB   8 orders  Intake
#
# ACC-BLWG-002: 3 orders total — BELOW 50 threshold
#   ORD-2001: B001AAA   3 orders  Storage
#
# ACC-PORT-001: 75 orders total — ABOVE 50 threshold
#   ORD-3001: B001AAA   5 orders  Storage        ← used in topic_switch_and_return
#   ORD-3002: B003CCC  70 orders  Intake
#
# ACC-CHIC-001: 100 orders total — ABOVE 50 threshold
#   ORD-4001: B005EEE 100 orders  Storage
ORDERS: list[dict[str, Any]] = [
    # Bluewing Logistics — 20 total
    {
        "orderId": "ORD-1001",
        "sku": "B001AAA",
        "itemName": "Bluewing Jersey - Home",
        "client": "ACC-BLWG-001",
        "clientName": "Bluewing Logistics",
        "stage": "Intake",
        "quantity": 12,
        "workQueue": "Q-INTAKE-1",
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
    },
    # Bluewing Concessions — 3 total
    {
        "orderId": "ORD-2001",
        "sku": "B001AAA",
        "itemName": "Bluewing Jersey - Home",
        "client": "ACC-BLWG-002",
        "clientName": "Bluewing Concessions",
        "stage": "Storage",
        "quantity": 3,
        "workQueue": "Q-STORAGE-1",
    },
    # Portland Pickles — 75 total
    {
        "orderId": "ORD-3001",
        "sku": "B001AAA",
        "itemName": "Bluewing Jersey - Home",
        "client": "ACC-PORT-001",
        "clientName": "Portland Pickles",
        "stage": "Storage",
        "quantity": 5,
        "workQueue": "Q-STORAGE-2",
    },
    {
        "orderId": "ORD-3002",
        "sku": "B003CCC",
        "itemName": "Pickles Pennant - Classic",
        "client": "ACC-PORT-001",
        "clientName": "Portland Pickles",
        "stage": "Intake",
        "quantity": 70,
        "workQueue": "Q-INTAKE-3",
    },
    # Chicago Cubs — 100 total
    {
        "orderId": "ORD-4001",
        "sku": "B005EEE",
        "itemName": "Cubs World Series Pennant",
        "client": "ACC-CHIC-001",
        "clientName": "Chicago Cubs",
        "stage": "Storage",
        "quantity": 100,
        "workQueue": "Q-STORAGE-3",
    },
]

# Pre-computed order totals by client id — used for constraint_change_mid_flow.
ORDER_TOTALS: dict[str, int] = {
    "ACC-BLWG-001": 20,
    "ACC-BLWG-002": 3,
    "ACC-PORT-001": 75,
    "ACC-CHIC-001": 100,
}

# Clients with more than 50 open orders — the expected filtered set.
CLIENTS_ABOVE_50 = {"ACC-PORT-001", "ACC-CHIC-001"}
CLIENTS_BELOW_50 = {"ACC-BLWG-001", "ACC-BLWG-002"}


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
            return str(c["id"])
    return None


def query_orders(
    client: str | None = None,
    sku: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    """STRICT: `client` must be an exact client id."""
    target_id = _exact_client_id(client) if client else None
    if client and target_id is None:
        return []
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


def order_total(client_id: str) -> int:
    """Return total order quantity for an exact client id."""
    return ORDER_TOTALS.get(client_id, 0)
