"""Dim multi_turn_drift — 3 MultiTurnScenario objects.

Each scenario is a sequence of user messages sent under the same session id
to a live eval container. Scoring is done on the FINAL assistant turn only
(standard evaluator semantics).

  constraint_change_mid_flow  — Turn 1: list active clients. Turn 3: apply
    > 50 order constraint. Pass = final answer mentions only
    Portland Pickles + Chicago Cubs; omits both Bluewing clients.

  pronoun_resolution  — Turn 1: look up Bluewing Logistics.
    Turn 2: ask 'what's their contact email?' Pass = final answer contains
    ops@bluewing.example (strict gold match — no LLM judge).

  topic_switch_and_return  — Turns 1-2: Portland Pickles orders.
    Turn 3: weather question (model should decline, out-of-scope).
    Turn 4: 'what about their B001 orders?' Pass = model returns to
    Portland Pickles and reports 5 orders for B001AAA, not Bluewing's 12.

Design calls:
  - Pronoun scoring: strict gold-answer match (email must appear in last
    assistant turn). Not an LLM judge — simpler, deterministic.
  - Container: each dim ships its own eval container for isolation
    (this one on port 8644). Tool set reuses the ops-suite tools (same
    mock MCP shape as tool_affordance) since this dim tests context
    tracking, not exotic tool surfaces.
  - constraint_change_mid_flow uses a Policy predicate (constraint layer)
    to explicitly block low-order-count clients from the final answer.
    This makes the constraint violation observable at the scoring layer,
    not just via outcome facts.

All scenarios tagged "dim:multi_turn_drift" for the failure taxonomy.
"""
from __future__ import annotations

from windtunnel.api.scenario import Policy, Scenario
from windtunnel.api.score import FailureCost
from windtunnel.api.trace import Trace
from windtunnel.scenarios.dim_multi_turn_drift.multi_turn import MultiTurnScenario

DIM_TAG = "dim:multi_turn_drift"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _last_assistant_content(trace: Trace) -> str:
    for turn in reversed(trace.turns):
        if turn.role == "assistant":
            return turn.content or ""
    return ""


def _no_low_order_count_clients_in_final_answer(trace: Trace) -> bool:
    """Policy predicate: the final answer must not mention clients with <= 50 orders.

    After the user added "> 50 orders" constraint, Bluewing Logistics
    (20 orders) and Bluewing Concessions (3 orders) must not appear.
    """
    answer = _last_assistant_content(trace).lower()
    forbidden_names = [
        "bluewing logistics",
        "bluewing concessions",
        "acc-blwg-001",
        "acc-blwg-002",
    ]
    return not any(name in answer for name in forbidden_names)


# ─── Scenario 1: constraint_change_mid_flow ───────────────────────────────────

_constraint_change_scenario = Scenario(
    name="constraint_change_mid_flow",
    # prompt is a copy of the FINAL user turn (user_turns[-1]) — the runner
    # drives user_turns and ignores prompt; prompt-reading surfaces (triage,
    # the LLM judge) still show the scored question.
    prompt="Filter the clients to only those with more than 50 open orders.",
    user_turns=[
        "Show me all active clients in the ops system.",
        "Great. Now which of these clients have the most open orders overall?",
        "Filter the clients to only those with more than 50 open orders.",
    ],
    # Outcome: final answer must mention both above-threshold clients.
    target_facts=[
        ["Portland Pickles", "ACC-PORT-001"],
        ["Chicago Cubs", "ACC-CHIC-001"],
    ],
    requires_tool_use=True,
    # Trajectory: client_lookup must be called to enumerate clients + check order counts.
    must_call=["client_lookup"],
    order_matters=False,
    # Constraint: low-order-count clients must NOT appear in the final answer.
    policies=[
        Policy(
            name="no_low_order_count_clients_after_constraint",
            predicate=_no_low_order_count_clients_in_final_answer,
        ),
    ],
    perturbations=[],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

constraint_change_mid_flow = MultiTurnScenario(scenario=_constraint_change_scenario)

# ─── Scenario 2: pronoun_resolution ──────────────────────────────────────────

_pronoun_resolution_scenario = Scenario(
    name="pronoun_resolution",
    # Final user turn uses 'their' — requires context from turn 1.
    prompt="What's their contact email?",
    user_turns=[
        "Look up Bluewing Logistics in the client directory.",
        "What's their contact email?",
    ],
    # Outcome: strict gold-answer match — exact email must appear.
    # ops@bluewing.example is the Bluewing Logistics contact (ACC-BLWG-001).
    # This is a strict match: accounts@bluewing.example (Concessions) is wrong.
    target_facts=[
        ["ops@bluewing.example"],
    ],
    requires_tool_use=True,
    # Trajectory: client_lookup must have been called (to resolve the email).
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

pronoun_resolution = MultiTurnScenario(scenario=_pronoun_resolution_scenario)

# ─── Scenario 3: topic_switch_and_return ─────────────────────────────────────

_topic_switch_scenario = Scenario(
    name="topic_switch_and_return",
    # Final user turn returns to the original client (Portland Pickles) with
    # a pronoun ('their') after an off-topic detour (weather).
    prompt="What about their B001 orders?",
    user_turns=[
        "What's the total order count for Portland Pickles in the ops system?",
        "Show me their B001AAA orders specifically.",
        "What's the weather like in Portland today?",
        "What about their B001 orders?",
    ],
    # Outcome: must correctly report Portland Pickles' B001AAA count (5 orders),
    # not Bluewing Logistics' B001AAA count (12 orders).
    # Also must mention Portland Pickles or ACC-PORT-001 — proving correct client.
    target_facts=[
        ["Portland Pickles", "ACC-PORT-001"],
        ["5"],
    ],
    requires_tool_use=True,
    # Trajectory: order_query must be called with the right client.
    must_call=["order_query"],
    order_matters=False,
    # Constraint: must NOT mention 12 (Bluewing Logistics' B001AAA count) or
    # conflate clients. Implemented as a policy.
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        severity="high",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    tags=[DIM_TAG],
)

topic_switch_and_return = MultiTurnScenario(scenario=_topic_switch_scenario)

# ─── Exported scenario set ────────────────────────────────────────────────────

MULTI_TURN_DRIFT_SCENARIOS: list[MultiTurnScenario] = [
    constraint_change_mid_flow,
    pronoun_resolution,
    topic_switch_and_return,
]
