"""Dim icl_poisoning — 3 scenario objects.

Each scenario exercises a distinct ICL-poisoning failure mode — what
happens when a prior turn in the conversation history is corrupted:

  empty_prior_assistant_turn  — blank prior assistant content (the
                                empty-prior-turn serialization bug shape).
                                Pass = model continues task, returns real answer.
                                Fail = model emits text-ending-in-colon + stops.

  primitive_fallback_leak     — prior turn rendered as 'tool: {...}' literal text
                                (the fallback-render serialization bug shape).
                                Pass = model produces a real answer (not replicating
                                the literal text format).
                                Fail = model's own content contains 'tool: {...}'.

  bad_prior_call_pattern      — prior assistant used wrong arg names in a tool call.
                                Pass = model retries / calls tool with correct args.
                                Fail = model copies the malformed arg field names.

All scenarios are tagged "dim:icl_poisoning" for the failure taxonomy.
These are REGRESSION GUARDS for two real production serialization bugs —
when run against a build with both fixes in place, all three should PASS.

Unlike the tool-affordance dim, these scenarios do not require a per-dim
mock MCP — they are pure serialization evals that test whether the model's
policy survives a corrupted conversation history. The perturbations operate
directly on the Trace, no live MCP call needed for the perturbation itself.

The runner does use the tool-affordance mock MCP server for the actual live
model calls, reusing its ops-suite-shaped tools. This avoids spinning up a
second identical mock just to get tool calls in the conversation.
"""
from __future__ import annotations

from windtunnel.api.perturbations import (
    BlankAssistantContent,
    FallbackRenderLeak,
    MalformedToolCall,
)
from windtunnel.api.scenario import Policy, Scenario
from windtunnel.api.score import FailureCost
from windtunnel.api.trace import Trace

DIM_TAG = "dim:icl_poisoning"

# ─── Shared failure-cost annotation ──────────────────────────────────────────
# ICL poisoning failures are high-severity because they cause the model to
# break mid-task silently — the user gets a colon-terminated non-answer or
# a literal JSON blob instead of the answer they asked for.
_FAILURE_COST = FailureCost(
    severity="high",
    customer_visible=True,
    reversible=True,
    side_effect_performed=False,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _last_assistant_content(trace: Trace) -> str:
    """Return the content of the last assistant turn, or ''."""
    for turn in reversed(trace.turns):
        if turn.role == "assistant":
            return turn.content
    return ""


# ─── Scenario 1: empty_prior_assistant_turn ───────────────────────────────────
#
# Regression guard for the empty-prior-turn serialization bug.
# The bug: _build_prompt received a Turn with content="" and tool_calls=[].
# The queue Message dataclass silently dropped tool_calls, so the assistant
# turn was serialized as an empty string into the chat template. On the next
# completion, the model saw its own prior empty turn and treated it as an
# in-context demonstration of "empty reply is fine", then produced
# 'text-ending-in-colon' + an immediate stop token (stop right after the colon).
#
# Pass = model's final assistant turn contains at least one of the target facts
#        (i.e. it produced a real answer or made a tool call that yielded results).
# Fail = model's final turn has content that ends in ':' with no further data,
#        OR is empty.
#
# The perturbation (BlankAssistantContent at turn_idx=1) blanks out the first
# assistant turn in a known-good session so the history looks like:
#   user:      "How many items does Bluewing Logistics have on order?"
#   assistant: ""                       ← BLANK (the poisoned prior turn)
#   tool:      <order_query result>
#   assistant: <model's new response>   ← this is what we score

def _colon_stop_policy(trace: Trace) -> bool:
    """Return True (policy satisfied) if the model did NOT produce colon-stop output.

    The empty-prior-turn bug produces a last assistant turn whose content ends
    with ':' (a sentence like 'Here are the order totals for Bluewing Logistics:')
    followed by nothing. We detect this as a policy violation.
    """
    content = _last_assistant_content(trace)
    if not content:
        return False  # Empty last turn is also a failure
    stripped = content.rstrip()
    return not stripped.endswith(":")


empty_prior_assistant_turn = Scenario(
    name="empty_prior_assistant_turn",
    prompt=(
        "How many total items does Bluewing Logistics have on order "
        "across all stages?"
    ),
    # Outcome: the answer must contain the client reference AND a number
    # (any non-zero number — the mock returns 20 items for ACC-BLWG-001)
    target_facts=[
        ["Bluewing Logistics", "ACC-BLWG-001", "Bluewing"],
        ["20", "twenty"],
    ],
    # requires_tool_use=False: ICL poisoning scenarios are serialization evals.
    # The test is whether the model produces a real answer despite a corrupted
    # prior turn — not whether it was forced to use a tool to get there.
    requires_tool_use=False,
    must_call=["order_query"],
    order_matters=False,
    # Constraint: model must not produce the colon-stop failure shape
    policies=[
        Policy(
            name="no_colon_stop",
            predicate=_colon_stop_policy,
            effect_class=None,
        ),
    ],
    # Perturbation: apply blank content to turn_idx=1 (the first assistant turn)
    perturbations=[
        BlankAssistantContent(turn_idx=1),
    ],
    failure_cost=_FAILURE_COST,
    tags=[DIM_TAG],
)


# ─── Scenario 2: primitive_fallback_leak ──────────────────────────────────────
#
# Regression guard for the fallback-render serialization bug.
# The bug: _build_prompt's tool_calls handling was bypassed because the
# worker's queue Message dataclass dropped tool_calls/tool_call_id from the
# message dict. The fallback rendered the assistant turn as literal text:
#   'tool: {"id": "call_0", "type": "function", "function": {...}}'
# On the next turn, the model saw this text as a prior assistant turn and
# replicated the same format in its own response — hallucinating a tool call
# as literal text instead of emitting a real <tool_call> block.
#
# Pass = model's final turn contains a real answer (e.g. "20 items") and does
#        NOT contain 'tool: {' literal text in its content.
# Fail = model's final turn replicates 'tool: {...}' in its content.
#
# The perturbation (FallbackRenderLeak at turn_idx=1) replaces the first
# assistant turn's structured tool_calls with the primitive 'tool: {...}' text.

_FALLBACK_LITERAL_PATTERN = 'tool: {'


def _no_fallback_replication_policy(trace: Trace) -> bool:
    """Return True if the model's last turn does NOT replicate the 'tool: {' pattern."""
    content = _last_assistant_content(trace)
    return _FALLBACK_LITERAL_PATTERN not in content


primitive_fallback_leak = Scenario(
    name="primitive_fallback_leak",
    prompt=(
        "How many total items does Bluewing Logistics have on order "
        "across all stages?"
    ),
    target_facts=[
        ["Bluewing Logistics", "ACC-BLWG-001", "Bluewing"],
        ["20", "twenty"],
    ],
    requires_tool_use=False,
    must_call=["order_query"],
    order_matters=False,
    # Constraint: model must not replicate the fallback 'tool: {' literal text
    policies=[
        Policy(
            name="no_fallback_replication",
            predicate=_no_fallback_replication_policy,
            effect_class=None,
        ),
    ],
    # Perturbation: apply fallback-render corruption to turn_idx=1
    perturbations=[
        FallbackRenderLeak(turn_idx=1),
    ],
    failure_cost=_FAILURE_COST,
    tags=[DIM_TAG],
)


# ─── Scenario 3: bad_prior_call_pattern ───────────────────────────────────────
#
# A prior assistant turn called client_lookup with wrong arg names
# (e.g. {"name": "Bluewing Logistics"} instead of {"query": "Bluewing Logistics"}).
# The wrong-key call got no results (empty matches), so this turn is in the
# history as a failed attempt. The question is: does the model learn from
# the failure and retry correctly, or does it copy the same broken arg names?
#
# Pass = model calls client_lookup with the correct 'query' key, gets results,
#        and returns them (outcome contains target facts).
# Fail = model copies {"name": ...} key, gets empty results again, fails to answer.
#
# The perturbation (MalformedToolCall at turn_idx=1) corrupts the first
# assistant turn's tool call arguments to use 'x_query' (wrong field name).

empty_result_tool = '{"result": "{\\"matches\\": [], \\"note\\": \\"No clients matched.\\"}"}'

bad_prior_call_pattern = Scenario(
    name="bad_prior_call_pattern",
    prompt=(
        "Look up Bluewing Logistics and tell me their account status "
        "and primary contact email."
    ),
    # Outcome: answer must contain client id or name AND email
    target_facts=[
        ["Bluewing Logistics", "ACC-BLWG-001"],
        ["ops@bluewing.example", "bluewing.example"],
    ],
    requires_tool_use=False,
    must_call=["client_lookup"],
    order_matters=False,
    policies=[],
    # Perturbation: corrupt the first client_lookup call's args
    perturbations=[
        MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names"),
    ],
    failure_cost=_FAILURE_COST,
    tags=[DIM_TAG],
)


# ─── Exported scenario set ────────────────────────────────────────────────────

ICL_POISONING_SCENARIOS: list[Scenario] = [
    empty_prior_assistant_turn,
    primitive_fallback_leak,
    bad_prior_call_pattern,
]
