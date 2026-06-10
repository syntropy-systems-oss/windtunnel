"""Synthetic back-office ops data for dim_sampler_sensitivity scenarios.

Three scenarios in this dim all reuse the same client directory and order
data — the dim tests how sampler settings (temp, top_p) affect model behaviour
on identical prompts, not tool diversity. Same data across all cells ensures
any variance observed is attributable to sampling, not input change.

Deliberate design choices:
  - typo_recovery: prompt contains a deliberate typo; the model must fuzzy-
    resolve it via ops_client_lookup. At high temperature, it sometimes gives up
    or hallucinates; at low temperature it reliably calls the lookup.
  - comparison_which_has_more: two clients with clearly different order
    volumes (20 vs 5). Low temp reliably answers correctly; high temp sometimes
    confabulates the comparison direction.
  - multi_step_followup: requires client_lookup → extract email field. Low temp
    completes the chain; high temp sometimes skips the second step.
"""
from __future__ import annotations

from typing import Any

# ─── Client dataset ───────────────────────────────────────────────────────────

CLIENTS: list[dict[str, Any]] = [
    {
        "id": "ACC-BLWG-001",
        "name": "Bluewing Logistics",
        "status": "active",
        "clientContactName": "Joe Marsh",
        "clientEmail": "ops@bluewing.example",
        "clientPhone": "978-555-0101",
        "order_qty": 20,
    },
    {
        "id": "ACC-CHIC-001",
        "name": "Chicago Ice Hockey Association",
        "status": "active",
        "clientContactName": "Maria Frost",
        "clientEmail": "maria@chicagoice.example",
        "clientPhone": "312-555-0202",
        "order_qty": 5,
    },
    {
        "id": "ACC-PORT-001",
        "name": "Portland Pickles Baseball",
        "status": "active",
        "clientContactName": "Sam Pickle",
        "clientEmail": "sam@portlandpickles.example",
        "clientPhone": "503-555-0303",
        "order_qty": 12,
    },
]

# ─── Lookup log (sentinel for test assertions) ────────────────────────────────

_lookup_log: list[dict[str, Any]] = []


def reset_lookup_log() -> None:
    """Reset the in-memory lookup log between test runs."""
    _lookup_log.clear()


def get_lookup_log() -> list[dict[str, Any]]:
    return list(_lookup_log)


# ─── Client lookup — lenient substring match ──────────────────────────────────

def find_clients(
    query: str = "",
    client_status: str | None = None,
    exclude_archived: bool = False,
) -> list[dict[str, Any]]:
    """Lenient substring search against client name or id.

    Replicates ops_client_lookup semantics from dim_tool_affordance:
    partial names work, exact ids work. Status filter is advisory.
    """
    _lookup_log.append({"op": "find_clients", "query": query})
    q = query.lower().strip()
    results = []
    for c in CLIENTS:
        if client_status and c.get("status") != client_status:
            continue
        if q and q not in c["name"].lower() and q not in c["id"].lower():
            continue
        results.append({k: v for k, v in c.items() if k != "order_qty"})
    return results


# ─── Comparison data — both clients returned with order volumes ───────────────

def find_comparison_clients() -> list[dict[str, Any]]:
    """Return the two clients used in the comparison scenario with order volumes."""
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "order_qty": c["order_qty"],
        }
        for c in CLIENTS
        if c["id"] in ("ACC-BLWG-001", "ACC-CHIC-001")
    ]


# ─── Order report — strict client id required ─────────────────────────────────

def order_report(
    client: str | None = None,
    sku: str | None = None,
) -> dict[str, dict[str, int]]:
    """Return per-stage order counts.

    Strict: client must be an exact id. Raw name returns all zeros.
    """
    _lookup_log.append({"op": "order_report", "client": client})
    stages = ["Intake", "Checked In", "Storage", "Client Outbound", "Shipped"]
    zeros = {s: {"count": 0, "quantity": 0} for s in stages}

    if not client:
        return zeros

    matched = [c for c in CLIENTS if c["id"] == client]
    if not matched:
        return zeros

    c = matched[0]
    # Distribute order quantity across stages for realism
    qty = c["order_qty"]
    return {
        "Intake": {"count": max(0, qty // 4), "quantity": max(0, qty // 4)},
        "Checked In": {"count": max(0, qty // 2), "quantity": max(0, qty // 2)},
        "Storage": {"count": max(0, qty - qty // 4 - qty // 2), "quantity": max(0, qty - qty // 4 - qty // 2)},
        "Client Outbound": {"count": 0, "quantity": 0},
        "Shipped": {"count": 0, "quantity": 0},
    }
