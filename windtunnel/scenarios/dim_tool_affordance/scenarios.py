"""Dim tool_affordance — 6 scenario objects.

Each scenario exercises a distinct tool-affordance failure mode:

  lookup_before_action  — model must chain client_lookup → order_query
                          (raw-name shortcut returns 0 due to strict fetch)
  field_scope_inference — contact email IS in scope via client_lookup;
                          model must NOT refuse as "external info"
                          (reproduces the specific_field_lookup regression)
  wrong_tool_correction — catalog info = product_lookup, not order tools;
                          model must re-pick despite the SKU being present
                          in the order data too
  investigate_before_export — resolve the client THEN export it via CSV
  export_customer_products  — resolve → export the client's items (scoped) via CSV
  bulk_table_to_csv         — a bulk "give me a table" request must be delivered
                          via deliver_as="csv" (a download link follow-up), NOT
                          an unrenderable markdown table (a real production
                          failure)

All scenarios are tagged "dim:tool_affordance" for the failure taxonomy.

Scenario tool names are CANONICAL bare names (e.g. client_lookup). Platforms
decorate them with prefix chains before the model sees them — e.g. the Acme
chain for this dim is `client_lookup` → `ops.client_lookup` →
`mcp_acme_ops_client_lookup` — and the trajectory evaluator matches the
decorated variants against the bare names.

CSV EXPORT FIDELITY: production has NO csv_export tool. CSV delivery is the
transport-level deliver_as="csv" parameter the REAL platform MCP server
injects onto every read-only tool and handles itself (deferred async export →
download link posted to the thread as a follow-up). The export scenarios below
score that real mechanism, not the old phantom csv_export tool (removed from the
mock). See CSV_DELIVERY_MARKERS.
"""
from __future__ import annotations

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import FailureCost

DIM_TAG = "dim:tool_affordance"

# Markers that prove the model delivered via the REAL CSV path (deliver_as="csv")
# rather than dumping a markdown table the chat surface can't render, or
# fabricating an inline link. On deliver_as="csv" the server returns a DEFERRED
# async ack — "I've started the export. The file will arrive as a follow-up
# message when it's ready — please don't expect inline data in my next reply."
# — and the download URL is posted to the thread LATER by the system. So a
# correct reply relays that async/follow-up framing; a markdown table or
# present-tense inline link hits NONE of these. (Seeded from the server ack
# wording; tune against live output.)
CSV_DELIVERY_MARKERS = [
    "follow-up", "follow up", "as a follow", "arrive", "shortly",
    "started the export", "export has started", "export started",
    "i've started", "i started", "started exporting", "kicked off",
    "be ready", "when it's ready", "when it is ready", "separate message",
    "be sent", "be posted", "will be posted", "posted to the", "preparing",
    "on its way", "momentarily", "queued", "exporting", "in a moment",
]

# ─── Scenario 1: lookup_before_action ────────────────────────────────────────

lookup_before_action = Scenario(
    name="lookup_before_action",
    prompt=(
        "How many total items does Bluewing Logistics have on order "
        "across all stages?"
    ),
    # Outcome: answer must mention the client id or name AND the total (20)
    target_facts=[
        ["ACC-BLWG-001", "Bluewing Logistics", "Bluewing Logistics"],
        ["20"],
    ],
    requires_tool_use=True,
    # Trajectory: client_lookup must be called BEFORE the fetch tool.
    # order_matters=True enforces the subsequence: client_lookup comes first.
    must_call=[
        "client_lookup",
        "order_query",
    ],
    order_matters=True,
    # Constraint: none for this dim
    policies=[],
    # Perturbations: none for this dim
    perturbations=[],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)


# ─── Scenario 2: field_scope_inference ───────────────────────────────────────

field_scope_inference = Scenario(
    name="field_scope_inference",
    prompt=(
        "What's the contact email for Bluewing Logistics? "
        "I need to send them a note about their pending orders."
    ),
    # Outcome: must return the exact email from the mock DB
    target_facts=[
        ["ops@bluewing.example"],
    ],
    requires_tool_use=True,
    # Trajectory: client_lookup must be called (it's the only tool that
    # returns clientEmail). No ordering constraint needed — it's the only
    # relevant tool.
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        severity="low",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 3: wrong_tool_correction ───────────────────────────────────────

wrong_tool_correction = Scenario(
    name="wrong_tool_correction",
    prompt=(
        "Show me the catalog info for SKU B001AAA — "
        "I need the product description and brand."
    ),
    # Outcome: must return catalog metadata (name, description, brand)
    # NOT order counts. A passing answer contains product description.
    target_facts=[
        ["Bluewing Jersey - Home", "Bluewing Jersey"],
        ["machine washable", "BluewingGear"],
    ],
    requires_tool_use=True,
    # Trajectory: product_lookup must be called.
    # order_report and order_query are FORBIDDEN — they return order
    # counts, not catalog metadata. Calling them is the failure mode.
    must_call=["product_lookup"],
    forbidden_calls=[
        "order_report",
        "order_query",
    ],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        severity="low",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 4: investigate_before_export (CSV via deliver_as="csv") ─────────

investigate_before_export = Scenario(
    name="investigate_before_export",
    prompt=(
        "Export the full order list for Bluewing Logistics to a CSV "
        "file and give me the download link."
    ),
    # Outcome (AND-of-OR): (g0) resolved the client, AND (g1) delivered via the
    # real CSV path — i.e. relayed the deferred async export ack. There is NO
    # csv_export tool: the model must call a read-only tool (order query/report)
    # with deliver_as="csv"; the system runs the export async and posts the link
    # as a follow-up, so the agent only gets an ack and must NOT promise an
    # inline link or paste a (fabricated) URL. A markdown table or made-up
    # inline link hits g1's markers not at all.
    target_facts=[
        ["ACC-BLWG-001", "Bluewing Logistics", "Bluewing Logistics"],
        CSV_DELIVERY_MARKERS,
    ],
    requires_tool_use=True,
    # Resolve the client before exporting (don't export from a raw name). The
    # export itself is now a deliver_as flag on a read-only tool, not a distinct
    # tool call, so we don't pin which data tool — g1 gates that it was a CSV
    # delivery, not an inline dump.
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        # Exporting the wrong/empty file is customer-visible and a wasted action.
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 5: export_customer_products (resolve → export, client-scoped) ────

export_customer_products = Scenario(
    name="export_customer_products",
    prompt=(
        "Export all the products that Bluewing Logistics manages in the "
        "ops suite — I want a CSV of just their items, not the whole catalog."
    ),
    # Outcome: (g0) resolved the client AND (g1) delivered via the real CSV path
    # (deferred async ack). Scoping to the client is via the `client` arg on the
    # read-only tool the model exports from; g0 proves it resolved that id first.
    target_facts=[
        ["ACC-BLWG-001", "Bluewing Logistics", "Bluewing Logistics"],
        CSV_DELIVERY_MARKERS,
    ],
    requires_tool_use=True,
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        # Exporting the wrong client's items / the whole catalog is customer-visible.
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 6: bulk_table_to_csv (a real production failure) ────────────────

bulk_table_to_csv = Scenario(
    name="bulk_table_to_csv",
    prompt=(
        "Please give me a complete list of every SKU and its quantity in "
        "Storage for the Seattle Steelheads account. Format it as a clear table "
        "with columns: SKU, Quantity in Storage."
    ),
    # The real production failure: a user asked for a TABLE (never said "CSV");
    # the agent dumped a markdown pipe-table — which does NOT render in the chat
    # surface. Seattle Steelheads has ~20 SKUs in Storage, so this is a genuine
    # bulk table: the right move is to recognize a bulk tabular/list request and
    # DELIVER VIA CSV (deliver_as="csv" → the system posts a download link
    # follow-up), not inline markdown. PASS (AND-of-OR): (g0) resolved the
    # client AND (g1) relayed the async CSV-export ack. FAIL = a markdown/inline
    # table — the observed failure — which hits none of g1's markers.
    target_facts=[
        ["ACC-STLH-001", "Seattle Steelheads", "Steelheads"],
        CSV_DELIVERY_MARKERS,
    ],
    requires_tool_use=True,
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        # An unrenderable markdown table for 20 rows = useless customer-visible output.
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Exported scenario set ────────────────────────────────────────────────────

TOOL_AFFORDANCE_SCENARIOS: list[Scenario] = [
    lookup_before_action,
    field_scope_inference,
    wrong_tool_correction,
    investigate_before_export,
    export_customer_products,
    bulk_table_to_csv,
]
