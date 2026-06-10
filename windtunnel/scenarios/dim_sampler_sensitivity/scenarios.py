"""Dim sampler_sensitivity — 3 scenario objects.

All three scenarios are known to be sampling-sensitive from baseline runs:
they are deterministically-pass at temp=0.0 but show variance at
higher temperatures. This is the expected pattern — the matrix runner
(runner.py) executes them across all temp × top_p cells to surface the
variance profile.

Key design choices:
  - variance_allowed=True on all three: the aggregator computes mean ± stddev
    instead of requiring 100% pass. A scenario that passes 80% at temp=0.7
    is informative, not a regression.
  - requires_tool_use=True on all three: outcome fails if no tool call is made.
    At high temperature the model sometimes confabulates without calling a tool.
  - DIM_TAG = "dim:sampler_sensitivity" for the failure taxonomy.

Scenarios:
  typo_recovery             — user sends a typo in the client name; model must
                              fuzzy-resolve via client_lookup. High temp can
                              cause confabulation or refusal.
  comparison_which_has_more — model must pull order totals for two clients
                              and compare them. High temp can reverse the
                              comparison direction.
  multi_step_followup       — model must client_lookup then extract email field.
                              High temp can skip the lookup step.
"""
from __future__ import annotations

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import FailureCost

DIM_TAG = "dim:sampler_sensitivity"

# ─── Scenario 1: typo_recovery ────────────────────────────────────────────────
# The user sends a prompt with a deliberate typo ("Logistcs" instead of
# "Logistics"). The model must call ops_client_lookup with a corrected or close
# enough query to retrieve the client record. At temp=0.0 the model reliably
# normalises the typo; at temp=1.0 it sometimes guesses or refuses.
#
# Variance signal: pass rate drops from ~100% at temp=0 to ~60–80% at temp=1
# in baseline runs. This is the canonical "known-flaky" scenario for this dim.

typo_recovery = Scenario(
    name="typo_recovery",
    prompt=(
        "Look up the account for Bluewing Logistcs "
        "and tell me the contact name."
    ),
    target_facts=[
        # Outcome: must return the correct client contact name
        ["Joe Marsh", "Joe"],
        # And must have resolved the correct client id or name
        ["Bluewing Logistics", "ACC-BLWG-001"],
    ],
    requires_tool_use=True,
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
    variance_allowed=True,
    tags=[DIM_TAG],
)

# ─── Scenario 2: comparison_which_has_more ────────────────────────────────────
# User asks which of two specific clients has more order volume. The model must
# call the order report for both, compare the totals, and report the winner
# correctly (ACC-BLWG-001 has quantity 20 vs ACC-CHIC-001 has quantity 5).
#
# Variance signal: at temp=0, model reliably calls both lookups and compares.
# At temp=1, it sometimes skips one lookup and guesses, or reverses the answer.

comparison_which_has_more = Scenario(
    name="comparison_which_has_more",
    prompt=(
        "Which client has more open order volume: ACC-BLWG-001 or ACC-CHIC-001? "
        "Check the order system and tell me the totals."
    ),
    target_facts=[
        # Outcome: must identify ACC-BLWG-001 as having more (20 vs 5)
        ["ACC-BLWG-001", "Bluewing Logistics"],
    ],
    requires_tool_use=True,
    must_call=["order_report"],
    order_matters=False,
    policies=[],
    perturbations=[],
    failure_cost=FailureCost(
        severity="medium",
        customer_visible=True,
        reversible=True,
        side_effect_performed=False,
    ),
    variance_allowed=True,
    tags=[DIM_TAG],
)

# ─── Scenario 3: multi_step_followup ─────────────────────────────────────────
# User asks for a contact email for a client identified by name. The model must
# call client_lookup, find the clientEmail field in the result, and return it.
# Multi-step because the email is not in the prompt — it requires a tool call
# to retrieve.
#
# Variance signal: at temp=0 the model always completes the lookup chain.
# At temp=1 it sometimes says "I don't have that information" or makes up an
# email address without calling the tool.

multi_step_followup = Scenario(
    name="multi_step_followup",
    prompt=(
        "What's the contact email address for the Bluewing Logistics client? "
        "I need to send them a follow-up message."
    ),
    target_facts=[
        # Outcome: must return the exact email from the mock DB
        ["ops@bluewing.example"],
    ],
    requires_tool_use=True,
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
    variance_allowed=True,
    tags=[DIM_TAG],
)

# ─── Exported scenario set ────────────────────────────────────────────────────

SAMPLER_SENSITIVITY_SCENARIOS: list[Scenario] = [
    typo_recovery,
    comparison_which_has_more,
    multi_step_followup,
]
