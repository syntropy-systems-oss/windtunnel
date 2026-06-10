"""Dim recovery — 4 scenario objects.

Each scenario exercises a distinct recovery failure mode:

  wrong_tool_then_correct       — prior assistant turn called product_lookup
                                  when order data was needed. Pass = next
                                  turn recognizes + calls order_query/report.
                                  Fail = doubles down or hallucinates.

  bad_arg_then_retry            — prior tool call used invalid stage enum
                                  "Incoming" (rejected). Pass = next turn
                                  retries with valid stage "Intake".
                                  Fail = gives up or claims success.

  empty_result_then_alternate_lookup — prior order_query used raw client name
                                  (strict tool → 0 results). User clearly
                                  expected data. Pass = next turn calls
                                  client_lookup to resolve id. Fail = reports
                                  "nothing found" with confidence.

  partial_result_then_clarify   — prior order_query returned 1/2 rows with
                                  hasMore=true. Pass = next turn paginates
                                  (offset=1) or asks. Fail = answers as if
                                  partial data is complete.

All tagged "dim:recovery" for the failure taxonomy.

Perturbation use:
  wrong_tool_then_correct       → InjectWrongPriorToolCall
  bad_arg_then_retry            → InjectSchemaRejectedCall
  empty_result_then_alternate_lookup → InjectWrongPriorToolCall (wrong-arg variant:
                                  raw name on strict tool → plausible-but-0 result)
  partial_result_then_clarify   → InjectPaginationTruncation

Recovery dim distinction from ICL poisoning:
  The ICL-poisoning perturbations (BlankAssistantContent, FallbackRenderLeak,
  MalformedToolCall) simulate CORRUPTED-AT-SERIALIZATION — the turn looks
  broken in history. The recovery perturbations simulate REALISTIC-BUT-WRONG —
  the turn looks normal (correct tool call structure, plausible result) but the
  model chose wrong. They fail differently and need different fixes:
  ICL poisoning → serializer/template fix, recovery → prompt "review prior
  turn before continuing" affordance.
"""
from __future__ import annotations

from windtunnel.api.perturbations import (
    InjectPaginationTruncation,
    InjectSchemaRejectedCall,
    InjectWrongPriorToolCall,
)
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import FailureCost

DIM_TAG = "dim:recovery"

# ─── Scenario 1: wrong_tool_then_correct ─────────────────────────────────────

wrong_tool_then_correct = Scenario(
    name="wrong_tool_then_correct",
    prompt=(
        "What's the total order quantity Bluewing Logistics has on file? "
        "I need the total count across all stages."
    ),
    # Outcome: must return the total quantity (20) for ACC-BLWG-001.
    # The prior wrong turn called product_lookup (returns catalog, not counts).
    # Recovery = model recognizes and calls order_query or order_report.
    target_facts=[
        ["20"],
        ["ACC-BLWG-001", "Bluewing Logistics", "Bluewing Logistics"],
    ],
    requires_tool_use=True,
    # Trajectory: must call order_query or order_report (the correct tools).
    # product_lookup is NOT in forbidden_calls: the perturbation injects a prior
    # wrong product_lookup into the trace history. The trajectory check only looks
    # at what the model calls going forward — it must call order_query to recover.
    must_call=[
        "order_query",
    ],
    forbidden_calls=[],
    order_matters=False,
    policies=[],
    # Perturbation: inject a prior wrong call to product_lookup before the prompt.
    # The fake_result is plausible catalog data (not an error), so the model must
    # recognize it's catalog info, not order counts.
    perturbations=[
        InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="product_lookup",
            fake_result=(
                '{"result": "{\\\"found\\\": true, \\\"product\\\": '
                '{\\\"name\\\": \\\"Bluewing Jersey - Home\\\", '
                '\\\"description\\\": \\\"Official home jersey, machine washable.\\\", '
                '\\\"brand\\\": \\\"BluewingGear\\\"}}"}'
            ),
        ),
    ],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 2: bad_arg_then_retry ──────────────────────────────────────────

bad_arg_then_retry = Scenario(
    name="bad_arg_then_retry",
    prompt=(
        "Show me all orders at the incoming stage for client ACC-BLWG-001. "
        "I need quantity by work queue."
    ),
    # Outcome: must return order data for Intake (the valid stage
    # that maps to what the user meant by "incoming"). Quantity 12 in ORD-1001.
    target_facts=[
        ["12", "Intake"],
        ["ACC-BLWG-001", "ORD-1001"],
    ],
    requires_tool_use=True,
    # Trajectory: must call order_query (the retry with valid args).
    must_call=["order_query"],
    order_matters=False,
    policies=[],
    # Perturbation: inject a prior order_query call with stage="Incoming" (invalid enum).
    # The error result tells the model the valid stage values.
    perturbations=[
        InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="order_query",
            bad_arg={"client": "ACC-BLWG-001", "stage": "Incoming"},
        ),
    ],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 3: empty_result_then_alternate_lookup ──────────────────────────

empty_result_then_alternate_lookup = Scenario(
    name="empty_result_then_alternate_lookup",
    prompt=(
        "Can you show me all orders for Portland Pickles? "
        "They should have some on file from last month's batch."
    ),
    # Outcome: must mention ACC-PORT-001 (the resolved client id) — proving the
    # model recovered by calling client_lookup and surfacing the id. The failing
    # trace only says "Portland Pickles has no orders" (name only, no lookup).
    # Requiring the ID ensures the trajectory (client_lookup call) is also
    # reflected in the answer — a model that skips lookup won't have the id.
    target_facts=[
        ["ACC-PORT-001"],
    ],
    requires_tool_use=True,
    # Trajectory: must call client_lookup to resolve the id.
    # Skipping client_lookup and just reporting "nothing found" is the failure.
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    # Perturbation: inject a prior order_query with raw client name "Portland Pickles"
    # which returns 0 results (strict tool rejects names). The fake_result is
    # a plausible zero-result response — not an error.
    perturbations=[
        InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="order_query",
            fake_result=(
                '{"result": "{\\\"orders\\\": [], \\\"summary\\\": '
                '{\\\"total\\\": {\\\"quantity\\\": 0, \\\"count\\\": 0}}, '
                '\\\"note\\\": \\\"No orders matched. If you passed a client name '
                'instead of an id, use ops_client_lookup first to get the exact '
                'client id.\\\"}"}'
            ),
        ),
    ],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Scenario 4: partial_result_then_clarify ─────────────────────────────────

partial_result_then_clarify = Scenario(
    name="partial_result_then_clarify",
    prompt=(
        "List ALL orders for ACC-BLWG-001. I need a complete count "
        "for their end-of-month report."
    ),
    # Outcome: must mention both ORD-1001 (qty 12) AND ORD-1002 (qty 8).
    # A model that answers as if the partial result is complete will only
    # mention ORD-1001 and miss ORD-1002.
    target_facts=[
        ["ORD-1001", "12"],
        ["ORD-1002", "8"],
    ],
    requires_tool_use=True,
    # Trajectory: order_query must be called (for the paginated follow-up).
    must_call=["order_query"],
    order_matters=False,
    policies=[],
    # Perturbation: the prior order_query result turn (at index 2, after user
    # + assistant turns) is truncated to show only 1 item with hasMore=true.
    # turn_idx=2 targets the tool result turn in a 3-turn base trace:
    #   [0] user, [1] assistant (tool call), [2] tool result
    perturbations=[
        InjectPaginationTruncation(
            turn_idx=2,
            truncate_at=1,
        ),
    ],
    failure_cost=FailureCost(
        severity="high",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

# ─── Exported scenario set ────────────────────────────────────────────────────

RECOVERY_SCENARIOS: list[Scenario] = [
    wrong_tool_then_correct,
    bad_arg_then_retry,
    empty_result_then_alternate_lookup,
    partial_result_then_clarify,
]
