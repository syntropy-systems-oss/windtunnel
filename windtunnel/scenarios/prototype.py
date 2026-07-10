"""Migrated prototype scenarios — regression baseline for the framework.

These 11 scenarios are migrated from the original single-file prototype into
the structured Scenario shape. Migration rules applied:

  - target_facts: carried over as-is (AND-of-OR shape preserved)
  - target_numbers: empty for now (no numeric facts were explicitly
    typed in the prototype — the target_facts already embed numbers as
    strings; upgrading to NumberFact is future per-dim work)
  - must_call / forbidden_calls / policies / perturbations: empty
    (trajectory/constraint expectations and perturbations start unpopulated; the
    per-dimension scenario sets add these per scenario)
  - failure_cost: defaults to low/internal/reversible for all 11
    (the prototype did not annotate failure cost)
  - requires_tool_use: True for specific_field_lookup — that scenario
    previously produced a false positive when the model answered from
    history instead of calling client_lookup; requires_tool_use closes
    that false-positive path
  - variance_allowed: False for all (100% pass required by default)

IMPORTANT: multi_step_followup is kept but is informational only — the
scenario's MULTI_STEP sentinel prompt cannot be reliably scored until
the gateway multi-turn driver is wired. The target_facts are carried
over; in practice this scenario will score FAIL against any single-shot
trace. Don't gate on it.
"""
from __future__ import annotations

from windtunnel.api.scenario import Scenario

PROTOTYPE_SCENARIOS: list[Scenario] = [
    # ── 1. Typo recovery ───────────────────────────────────────────────────────
    Scenario(
        name="typo_recovery",
        prompt=(
            "Please list the SKUs and quantities currently in the back-office "
            "ops suite for the client 'ACC-1322 – Bluewing Logistics'."
        ),
        target_facts=[
            ["ACC-BLWG-001", "ACC-BLWG-002", "Bluewing Logistics"],
            ["B001AAA", "B002BBB", "B003CCC"],
        ],
        requires_tool_use=True,
    ),

    # ── 2. Cross-stage search ──────────────────────────────────────────────────
    Scenario(
        name="cross_stage_search",
        prompt=(
            "How many orders are in Storage for Bluewing Logistics "
            "(ACC-BLWG-001)? If there aren't any in Storage, where are they?"
        ),
        target_facts=[
            ["Intake"],
            ["B001AAA", "B002BBB", "20", "12", "8"],
        ],
        requires_tool_use=True,
    ),

    # ── 3. Multi-client aggregate ──────────────────────────────────────────────
    Scenario(
        name="multi_client_aggregate",
        prompt=(
            "What's the total quantity of SKU B001AAA across all clients? "
            "Break it down by client."
        ),
        target_facts=[
            ["15"],
            ["ACC-BLWG-001", "Bluewing Logistics"],
            ["ACC-BLWG-002", "Bluewing Concessions"],
        ],
        # "15" is a standalone total — NumberFact(15) would be more precise,
        # but the string "15" in target_facts covers the primary assertion.
        # A follow-up pass can add target_numbers=[NumberFact(15)] to
        # tighten this against false positives like "15th" appearing in text.
        requires_tool_use=True,
    ),

    # ── 4. Disambiguation ──────────────────────────────────────────────────────
    Scenario(
        name="disambiguation",
        prompt="Show me all SKUs and quantities for the Bluewing client.",
        target_facts=[
            ["ACC-BLWG-001", "Bluewing Logistics", "Logistics"],
            ["ACC-BLWG-002", "Bluewing Concessions", "Concessions"],
        ],
        requires_tool_use=True,
    ),

    # ── 5. Wrong-tool seduction ────────────────────────────────────────────────
    Scenario(
        name="wrong_tool_seduction",
        prompt="What's the product description for SKU B001AAA?",
        target_facts=[
            ["Official home jersey, machine washable", "Bluewing Jersey - Home"],
            ["BluewingGear"],
        ],
        requires_tool_use=True,
    ),

    # ── 6. Negative query ──────────────────────────────────────────────────────
    Scenario(
        name="negative_query",
        prompt="Which active clients have NO orders currently in the Storage stage?",
        target_facts=[
            ["ACC-BLWG-001", "Bluewing Logistics"],
        ],
        requires_tool_use=True,
    ),

    # ── 7. Cross-reference ─────────────────────────────────────────────────────
    Scenario(
        name="cross_reference",
        prompt=(
            "For SKU B001AAA, which clients currently have it on order, "
            "and in what quantities?"
        ),
        target_facts=[
            ["ACC-BLWG-001", "Bluewing Logistics"],
            ["ACC-BLWG-002", "Bluewing Concessions"],
            ["12"],
            ["3"],
        ],
        # NOTE: "3" in target_facts is a string match — it would match "B003CCC".
        # This is a known weakness carried from the prototype. The correct fix
        # is target_numbers=[NumberFact(3)] + removing "3" from target_facts.
        # Left as-is for backward compatibility with the prototype baseline;
        # a follow-up pass should tighten this.
        requires_tool_use=True,
    ),

    # ── 8. Specific-field lookup ───────────────────────────────────────────────
    # requires_tool_use=True is the key fix: a baseline run showed this scenario
    # scored FAIL because the model hedged without calling client_lookup.
    # Without requires_tool_use, a future run that guesses the email from
    # training would score PASS falsely.
    Scenario(
        name="specific_field_lookup",
        prompt="What's the contact email for Bluewing Logistics?",
        target_facts=[
            ["ops@bluewing.example"],
        ],
        requires_tool_use=True,
    ),

    # ── 9. Comparison (which-has-more) ────────────────────────────────────────
    Scenario(
        name="comparison_which_has_more",
        prompt=(
            "Which client has more total order quantity across all stages: "
            "Bluewing Logistics or Chicago Cubs? Give the totals."
        ),
        target_facts=[
            ["Chicago Cubs", "ACC-CHIC-001"],
            ["100"],
            ["20"],
        ],
        requires_tool_use=True,
    ),

    # ── 10. Order-trace (cross-table lookup) ──────────────────────────────────
    Scenario(
        name="order_by_id_trace",
        prompt="Where is order ORD-3001 right now, and what is it?",
        target_facts=[
            ["ORD-3001"],
            ["B004DDD", "Pickles Mascot Plush"],
            ["Q-STORAGE-2", "Storage"],
        ],
        requires_tool_use=True,
    ),

    # ── 11. Multi-step follow-up ──────────────────────────────────────────────
    # INFORMATIONAL: cannot be reliably scored single-shot. The MULTI_STEP
    # sentinel prompt is carried over. Target facts are preserved so this
    # scenario can be scored if/when the multi-turn driver is wired.
    Scenario(
        name="multi_step_followup",
        prompt=(
            "MULTI_STEP: see MULTI_TURN_PROMPTS[multi_step_followup]"
        ),
        target_facts=[
            ["ACC-BLWG-001", "Bluewing Logistics", "Logistics"],
            ["B001AAA", "B002BBB", "Bluewing Jersey", "Bluewing Cap"],
            ["Intake"],
        ],
        requires_tool_use=True,
        # variance_allowed=True would be appropriate here once multi-turn
        # is wired — single-shot will almost always fail. Left at the
        # default False so it's visibly red rather than silently ignored.
    ),
]
