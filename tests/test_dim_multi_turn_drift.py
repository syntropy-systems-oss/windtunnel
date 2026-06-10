"""Tests for dim_multi_turn_drift scenarios.

Coverage:
  1. Scenario tags field populated on all 3 scenarios — 'dim:multi_turn_drift'
  2. Scenario structure: each scenario carries a `turns` list (multi-turn shape),
     not just a single `prompt`. The runner sends each turn sequentially,
     maintaining the same session id.
  3. Per-scenario scoring: outcome evaluator checks the FINAL assistant turn only.
  4. Unit tests for all 3 scenarios — passing and failing traces:
       - constraint_change_mid_flow: turn 4 must respect the > 50 order constraint
       - pronoun_resolution: 'their' must resolve to Bluewing Logistics (strict match)
       - topic_switch_and_return: turn 5 must return to Portland Pickles B001AAA
  5. Synthetic DB contract: order totals, client data correct
  6. MultiTurnScenario dataclass shape: turns field is a list of user messages
  7. Helper: build_turn_messages() converts a MultiTurnScenario's turns
     into the accumulated messages list for each successive api_server call

Design note:
  - Pronoun-resolution scoring uses strict gold-answer match (email must appear),
    not an LLM judge. Simpler, deterministic, sufficient for prototype phase.
  - Container choice: each dim ships its own eval container for isolation
    (this one on port 8644). Tool set reuses the ops-suite tools from
    tool_affordance (same mock MCP shape) since multi-turn drift is about
    context tracking, not exotic tools.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from windtunnel.api.evaluators import evaluate_constraint, evaluate_outcome, evaluate_trajectory
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.scenarios.dim_multi_turn_drift.multi_turn import (
    build_turn_messages,
)

# ─── Import targets (fail until implemented) ──────────────────────────────────
from windtunnel.scenarios.dim_multi_turn_drift.scenarios import (
    DIM_TAG,
    MULTI_TURN_DRIFT_SCENARIOS,
    constraint_change_mid_flow,
    pronoun_resolution,
    topic_switch_and_return,
)
from windtunnel.scenarios.dim_multi_turn_drift.synthetic_db import (
    CLIENTS,
    CLIENTS_ABOVE_50,
    CLIENTS_BELOW_50,
    ORDER_TOTALS,
    find_clients,
    order_total,
    query_orders,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    latency_ms: float = 50.0,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=[],
        latency_ms=latency_ms,
    )


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": str(args or {})},
    }


def _make_trace(*turns: Turn) -> Trace:
    return Trace(
        scenario_id="test",
        agent_id="agent-test",
        variant_id="baseline",
        model="test-model",
        quant="q4",
        sampler={},
        started_at=_ts(),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=list(turns),
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )


# ─── 1. DIM tag + scenario set ────────────────────────────────────────────────

class TestDimTag:
    def test_dim_tag_constant(self):
        assert DIM_TAG == "dim:multi_turn_drift"

    def test_all_scenarios_tagged(self):
        for sc in MULTI_TURN_DRIFT_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_count(self):
        assert len(MULTI_TURN_DRIFT_SCENARIOS) == 3

    def test_scenario_names(self):
        names = {sc.name for sc in MULTI_TURN_DRIFT_SCENARIOS}
        assert names == {
            "constraint_change_mid_flow",
            "pronoun_resolution",
            "topic_switch_and_return",
        }


# ─── 2. MultiTurnScenario dataclass ──────────────────────────────────────────

class TestMultiTurnScenarioShape:
    """The MultiTurnScenario wraps a Scenario and carries a list of user turns."""

    def test_has_scenario_field(self):
        """MultiTurnScenario must have a scenario attribute pointing to the Scenario."""
        mts = constraint_change_mid_flow
        assert hasattr(mts, "scenario"), "MultiTurnScenario must have a .scenario field"

    def test_has_user_turns_field(self):
        """MultiTurnScenario must carry user_turns: list of str."""
        mts = constraint_change_mid_flow
        assert hasattr(mts, "user_turns"), "MultiTurnScenario must have .user_turns"
        assert isinstance(mts.user_turns, list)
        assert all(isinstance(t, str) for t in mts.user_turns)

    def test_constraint_change_has_at_least_3_turns(self):
        """constraint_change_mid_flow needs turns 1..4: at least 3 user messages."""
        assert len(constraint_change_mid_flow.user_turns) >= 3

    def test_pronoun_resolution_has_at_least_2_turns(self):
        """pronoun_resolution: turn 1 (lookup) + turn 3 (their email) = 2+ user turns."""
        assert len(pronoun_resolution.user_turns) >= 2

    def test_topic_switch_has_at_least_4_turns(self):
        """topic_switch_and_return: client A (1-2) + weather (3-4) + return (5) = 5 turns,
        but the model handles turns 3-4 so we send at least 4 user messages."""
        assert len(topic_switch_and_return.user_turns) >= 4

    def test_scenario_field_is_scenario_object(self):
        from windtunnel.api.scenario import Scenario
        assert isinstance(constraint_change_mid_flow.scenario, Scenario)


# ─── 3. build_turn_messages helper ────────────────────────────────────────────

class TestBuildTurnMessages:
    """build_turn_messages(user_turns, responses) accumulates the full message list.

    It takes the list of user_turns and the list of assistant responses so far,
    interleaves them, and returns the accumulated messages list for the next call.

    When sending turn N (0-indexed), we pass:
      - user_turns[0..N]  interleaved with  responses[0..N-1]
    """

    def test_first_turn_is_just_user_message(self):
        msgs = build_turn_messages(["Hello?"], responses=[])
        assert msgs == [{"role": "user", "content": "Hello?"}]

    def test_second_turn_interleaves_response(self):
        msgs = build_turn_messages(
            ["Hello?", "What's the email?"],
            responses=["Hi there!"],
        )
        assert msgs == [
            {"role": "user", "content": "Hello?"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "What's the email?"},
        ]

    def test_three_turn_accumulation(self):
        msgs = build_turn_messages(
            ["Turn 1", "Turn 2", "Turn 3"],
            responses=["Reply 1", "Reply 2"],
        )
        assert msgs == [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Reply 2"},
            {"role": "user", "content": "Turn 3"},
        ]

    def test_empty_turns_returns_empty(self):
        msgs = build_turn_messages([], responses=[])
        assert msgs == []

    def test_mismatched_lengths_raises(self):
        """Caller error: more responses than preceding turns."""
        with pytest.raises((ValueError, AssertionError, IndexError)):
            build_turn_messages(["Turn 1"], responses=["Reply 1", "Reply 2"])


# ─── 4. constraint_change_mid_flow scenario ───────────────────────────────────

class TestConstraintChangeMidFlow:
    """Turn 1: 'show active clients.' Turn 3: 'only those with > 50 orders.'
    Pass = turn 4 answer mentions clients with > 50 orders (Portland Pickles,
           Chicago Cubs) AND does NOT include clients with <= 50 orders
           (Bluewing Logistics, Bluewing Concessions).
    """

    def _passing_trace(self) -> Trace:
        """Final assistant turn correctly applies the > 50 constraint."""
        return _make_trace(
            _turn(role="user", content="Show me all active clients."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"clientStatus": "active"})
            ]),
            _turn(role="tool", content='{"result": "[all 4 active clients]"}'),
            _turn(role="assistant", content=(
                "Here are the active clients: Bluewing Logistics (20 orders), "
                "Bluewing Concessions (3 orders), Portland Pickles (75 orders), "
                "Chicago Cubs (100 orders)."
            )),
            _turn(role="user", content="Actually, just the ones with more than 50 open orders."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"clientStatus": "active"})
            ]),
            _turn(role="tool", content='{"result": "[order totals]"}'),
            # Final turn: only high-order-count clients
            _turn(role="assistant", content=(
                "Clients with more than 50 open orders: "
                "Portland Pickles (ACC-PORT-001, 75 orders) and "
                "Chicago Cubs (ACC-CHIC-001, 100 orders)."
            )),
        )

    def _failing_trace_ignores_constraint(self) -> Trace:
        """Final turn lists all clients again, ignoring the > 50 constraint."""
        return _make_trace(
            _turn(role="user", content="Show me all active clients."),
            _turn(role="assistant", content=(
                "Active clients: Bluewing Logistics, Bluewing Concessions, "
                "Portland Pickles, Chicago Cubs."
            )),
            _turn(role="user", content="Actually, just the ones with more than 50 open orders."),
            # Fails: still includes low-order-count clients
            _turn(role="assistant", content=(
                "Here are all active clients: Bluewing Logistics, "
                "Bluewing Concessions, Portland Pickles, Chicago Cubs."
            )),
        )

    def _failing_trace_includes_low_order_client(self) -> Trace:
        """Final turn includes Bluewing Logistics (below threshold) — drift failure."""
        return _make_trace(
            _turn(role="user", content="Show me all active clients."),
            _turn(role="assistant", content="Active clients listed above."),
            _turn(role="user", content="Actually, just the ones with more than 50 open orders."),
            _turn(role="assistant", content=(
                "Portland Pickles (75 orders), Bluewing Logistics (20 orders), "
                "Chicago Cubs (100 orders)."
            )),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), constraint_change_mid_flow.scenario)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_ignores_constraint_outcome_fails(self):
        result = evaluate_outcome(
            self._failing_trace_ignores_constraint(),
            constraint_change_mid_flow.scenario,
        )
        assert not result.passed, "Expected outcome fail: ignored > 50 constraint"

    def test_failing_includes_low_order_client_outcome_fails(self):
        """Including a below-threshold client means the constraint was not applied."""
        result = evaluate_outcome(
            self._failing_trace_includes_low_order_client(),
            constraint_change_mid_flow.scenario,
        )
        assert not result.passed, "Expected outcome fail: low-order-count client included"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), constraint_change_mid_flow.scenario)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_scenario_requires_tool_use(self):
        assert constraint_change_mid_flow.scenario.requires_tool_use is True

    def test_scenario_has_at_least_3_user_turns(self):
        """Need at least: Turn 1 (list clients), Turn 2 or 3 (apply constraint),
        Turn N (response after constraint)."""
        assert len(constraint_change_mid_flow.user_turns) >= 3

    def test_target_facts_include_above_threshold_clients(self):
        """Must mention Portland Pickles and Chicago Cubs in the final answer."""
        flat = [f for group in constraint_change_mid_flow.scenario.target_facts for f in group]
        has_portland = any("Portland Pickles" in f or "ACC-PORT-001" in f for f in flat)
        has_chicago = any("Chicago Cubs" in f or "ACC-CHIC-001" in f for f in flat)
        assert has_portland, f"Expected Portland Pickles in target_facts, got: {flat}"
        assert has_chicago, f"Expected Chicago Cubs in target_facts, got: {flat}"

    def test_constraint_no_low_order_count_clients_in_final_answer(self):
        """The scenario's forbidden_facts or constraint should exclude low-order-count clients.
        Test via the failing trace: a trace that includes Bluewing Logistics must fail outcome."""
        result = evaluate_outcome(
            self._failing_trace_includes_low_order_client(),
            constraint_change_mid_flow.scenario,
        )
        assert not result.passed


# ─── 5. pronoun_resolution scenario ──────────────────────────────────────────

class TestPronounResolution:
    """Turn 1: look up Bluewing Logistics. Turn 3: 'what's their contact email?'
    Pass = final turn returns ops@bluewing.example (strict match).
    Fail = model returns wrong email, asks 'whose?', or returns nothing.

    Scoring: strict gold-answer match — the exact email string must appear.
    (Not an LLM judge — simpler for prototype phase.)
    """

    def _passing_trace(self) -> Trace:
        """'Their' correctly resolves to Bluewing Logistics."""
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup",
                           {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content=(
                '{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\", '
                '\\"name\\": \\"Bluewing Logistics\\", '
                '\\"clientEmail\\": \\"ops@bluewing.example\\"}]}"}'
            )),
            _turn(role="assistant", content=(
                "Found Bluewing Logistics (ACC-BLWG-001). "
                "Contact: ops@bluewing.example."
            )),
            _turn(role="user", content="What's their contact email?"),
            # Correct: model resolves 'their' to ACC-BLWG-001
            _turn(role="assistant", content=(
                "The contact email for Bluewing Logistics is "
                "ops@bluewing.example."
            )),
        )

    def _failing_trace_wrong_email(self) -> Trace:
        """Model picks the wrong Bluewing Logistics (Concessions) email — drift."""
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics."),
            _turn(role="assistant", content=(
                "Found Bluewing Logistics (ACC-BLWG-001)."
            )),
            _turn(role="user", content="What's their contact email?"),
            # Wrong: returns Concessions email (jane@) instead of Bluewing Logistics (joe@)
            _turn(role="assistant", content=(
                "The contact email is accounts@bluewing.example."
            )),
        )

    def _failing_trace_asks_whose(self) -> Trace:
        """Model fails to resolve pronoun — asks 'whose?' instead of using context."""
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics."),
            _turn(role="assistant", content=(
                "Found Bluewing Logistics (ACC-BLWG-001)."
            )),
            _turn(role="user", content="What's their contact email?"),
            # Fail: model asks for clarification instead of resolving from context
            _turn(role="assistant", content=(
                "Could you clarify whose contact email you're looking for?"
            )),
        )

    def _failing_trace_no_email(self) -> Trace:
        """Model doesn't include the email at all."""
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics."),
            _turn(role="assistant", content="Found the client."),
            _turn(role="user", content="What's their contact email?"),
            _turn(role="assistant", content="I don't have that information available."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), pronoun_resolution.scenario)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_wrong_email_outcome_fails(self):
        """Wrong email (jane@) fails strict gold-answer match."""
        result = evaluate_outcome(self._failing_trace_wrong_email(), pronoun_resolution.scenario)
        assert not result.passed, "Expected outcome fail: wrong email returned"

    def test_failing_asks_whose_outcome_fails(self):
        """Asking 'whose?' fails — gold email not in answer."""
        result = evaluate_outcome(self._failing_trace_asks_whose(), pronoun_resolution.scenario)
        assert not result.passed, "Expected outcome fail: model asked for clarification"

    def test_failing_no_email_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_no_email(), pronoun_resolution.scenario)
        assert not result.passed, "Expected outcome fail: no email in answer"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), pronoun_resolution.scenario)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_scenario_requires_tool_use(self):
        assert pronoun_resolution.scenario.requires_tool_use is True

    def test_target_fact_is_specific_email(self):
        """The gold email for Bluewing Logistics must be a target fact."""
        flat = [f for group in pronoun_resolution.scenario.target_facts for f in group]
        assert "ops@bluewing.example" in flat, (
            f"Expected ops@bluewing.example in target_facts, got: {flat}"
        )

    def test_wrong_email_not_in_target_facts(self):
        """Jane's email (Concessions) must NOT be a passing answer."""
        flat = [f for group in pronoun_resolution.scenario.target_facts for f in group]
        # The wrong email (jane@) must not be in target_facts — if it were,
        # the scenario would accept the drift answer as a pass.
        assert "accounts@bluewing.example" not in flat, (
            "accounts@bluewing.example must not be in target_facts"
        )

    def test_has_at_least_2_user_turns(self):
        assert len(pronoun_resolution.user_turns) >= 2


# ─── 6. topic_switch_and_return scenario ─────────────────────────────────────

class TestTopicSwitchAndReturn:
    """Turns 1-2: Portland Pickles / B001AAA orders.
    Turns 3-4: weather question (model should decline, out-of-scope).
    Turn 5: 'what about their B001 orders?' — 'their' = Portland Pickles.
    Pass = final turn reports Portland Pickles' B001AAA count (5 orders),
           NOT Bluewing Logistics' B001AAA (12 orders) or any other conflation.
    """

    def _passing_trace(self) -> Trace:
        """Final turn correctly returns to Portland Pickles, not contaminated by Bluewing."""
        return _make_trace(
            _turn(role="user", content="What's the order count for Portland Pickles?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Portland Pickles"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-PORT-001\\"}]}"}'),
            _turn(role="assistant", content=(
                "Portland Pickles (ACC-PORT-001) has 75 orders total in the system."
            )),
            _turn(role="user", content="What's the weather like in Portland?"),
            _turn(role="assistant", content=(
                "I'm a back-office ops assistant and can't help with weather. "
                "Is there something about Portland Pickles' orders I can help with?"
            )),
            _turn(role="user", content="What about their B001 orders?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query",
                           {"client": "ACC-PORT-001", "sku": "B001AAA"})
            ]),
            _turn(role="tool", content=(
                '{"result": "{\\"orders\\": [{\\"orderId\\": \\"ORD-3001\\", '
                '\\"sku\\": \\"B001AAA\\", \\"quantity\\": 5, '
                '\\"clientName\\": \\"Portland Pickles\\"}]}"}'
            )),
            # Final turn: correct client (Portland), correct quantity (5)
            _turn(role="assistant", content=(
                "Portland Pickles (ACC-PORT-001) has 5 orders for B001AAA "
                "(Bluewing Jersey - Home) in Storage."
            )),
        )

    def _failing_trace_conflates_clients(self) -> Trace:
        """Drift: model conflates Portland Pickles with Bluewing Logistics — returns 12 orders."""
        return _make_trace(
            _turn(role="user", content="What's the order count for Portland Pickles?"),
            _turn(role="assistant", content="Portland Pickles has orders in the system."),
            _turn(role="user", content="What's the weather like in Portland?"),
            _turn(role="assistant", content="I can't help with weather."),
            _turn(role="user", content="What about their B001 orders?"),
            # Fails: returns Bluewing Logistics' B001AAA count (12) not Portland's (5)
            _turn(role="assistant", content=(
                "For B001AAA (Bluewing Jersey - Home), there are 12 orders in "
                "Intake for Bluewing Logistics."
            )),
        )

    def _failing_trace_wrong_quantity(self) -> Trace:
        """Returns B001AAA orders but wrong quantity (uses wrong client data)."""
        return _make_trace(
            _turn(role="user", content="What's the order count for Portland Pickles?"),
            _turn(role="assistant", content="Portland Pickles (ACC-PORT-001) has open orders."),
            _turn(role="user", content="What's the weather like in Portland?"),
            _turn(role="assistant", content="I can't answer weather questions."),
            _turn(role="user", content="What about their B001 orders?"),
            # Wrong: 12 orders (Bluewing Logistics' count) instead of 5 (Portland Pickles)
            _turn(role="assistant", content=(
                "Portland Pickles has 12 orders for B001AAA in the system."
            )),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), topic_switch_and_return.scenario)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_conflates_clients_outcome_fails(self):
        result = evaluate_outcome(
            self._failing_trace_conflates_clients(),
            topic_switch_and_return.scenario,
        )
        assert not result.passed, "Expected outcome fail: client conflation"

    def test_failing_wrong_quantity_outcome_fails(self):
        result = evaluate_outcome(
            self._failing_trace_wrong_quantity(),
            topic_switch_and_return.scenario,
        )
        assert not result.passed, "Expected outcome fail: wrong B001AAA quantity"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), topic_switch_and_return.scenario)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_scenario_requires_tool_use(self):
        assert topic_switch_and_return.scenario.requires_tool_use is True

    def test_target_facts_include_portland_pickles(self):
        flat = [f for group in topic_switch_and_return.scenario.target_facts for f in group]
        has_portland = any(
            "Portland Pickles" in f or "ACC-PORT-001" in f for f in flat
        )
        assert has_portland, f"Expected Portland Pickles in target_facts, got: {flat}"

    def test_target_facts_include_b001_count(self):
        """Must verify the correct quantity (5) for Portland Pickles' B001AAA."""
        flat = [f for group in topic_switch_and_return.scenario.target_facts for f in group]
        has_5 = any("5" in f for f in flat)
        assert has_5, f"Expected '5' (Portland Pickles B001AAA count) in target_facts, got: {flat}"

    def test_has_at_least_4_user_turns(self):
        """Need: client A setup, weather (off-topic), return-to-client-A."""
        assert len(topic_switch_and_return.user_turns) >= 4


# ─── 7. Synthetic DB contracts ────────────────────────────────────────────────

class TestSyntheticDbContracts:
    """Verify the synthetic DB enforces correct order data for drift scenarios."""

    def test_blwg_001_below_threshold(self):
        """Bluewing Logistics: 20 orders < 50."""
        assert ORDER_TOTALS["ACC-BLWG-001"] == 20
        assert "ACC-BLWG-001" in CLIENTS_BELOW_50
        assert "ACC-BLWG-001" not in CLIENTS_ABOVE_50

    def test_blwg_002_below_threshold(self):
        """Bluewing Concessions: 3 orders < 50."""
        assert ORDER_TOTALS["ACC-BLWG-002"] == 3
        assert "ACC-BLWG-002" in CLIENTS_BELOW_50

    def test_port_001_above_threshold(self):
        """Portland Pickles: 75 orders > 50."""
        assert ORDER_TOTALS["ACC-PORT-001"] == 75
        assert "ACC-PORT-001" in CLIENTS_ABOVE_50
        assert "ACC-PORT-001" not in CLIENTS_BELOW_50

    def test_chic_001_above_threshold(self):
        """Chicago Cubs: 100 orders > 50."""
        assert ORDER_TOTALS["ACC-CHIC-001"] == 100
        assert "ACC-CHIC-001" in CLIENTS_ABOVE_50

    def test_order_total_helper(self):
        assert order_total("ACC-PORT-001") == 75
        assert order_total("ACC-BLWG-001") == 20
        assert order_total("UNKNOWN") == 0

    def test_find_clients_returns_all_active(self):
        results = find_clients(client_status="active")
        ids = {c["id"] for c in results}
        assert "ACC-BLWG-001" in ids
        assert "ACC-PORT-001" in ids
        assert "ACC-CHIC-001" in ids

    def test_blwg_001_email(self):
        """Joe's email (Bluewing Logistics) is the gold answer for pronoun_resolution."""
        results = find_clients(query="Bluewing Logistics")
        assert any(c["clientEmail"] == "ops@bluewing.example" for c in results)

    def test_blwg_002_email_different(self):
        """Jane's email (Concessions) must differ from Joe's — tests that the
        wrong-email failure mode is actually wrong."""
        results = find_clients(query="Bluewing Concessions")
        assert any(c["clientEmail"] == "accounts@bluewing.example" for c in results)

    def test_portland_b001_quantity(self):
        """Portland Pickles has 5 orders for B001AAA (topic_switch gold answer)."""
        orders = query_orders(client="ACC-PORT-001", sku="B001AAA")
        total = sum(o["quantity"] for o in orders)
        assert total == 5

    def test_bluewing_b001_quantity_differs(self):
        """Bluewing Logistics has 12 orders for B001AAA.
        This must NOT match Portland's 5 — verifies conflation is detectable."""
        orders = query_orders(client="ACC-BLWG-001", sku="B001AAA")
        total = sum(o["quantity"] for o in orders)
        assert total == 12
        assert total != 5  # Must differ from Portland's count

    def test_four_clients_exist(self):
        assert len(CLIENTS) == 4

    def test_above_50_set_has_exactly_two(self):
        assert len(CLIENTS_ABOVE_50) == 2
        assert CLIENTS_ABOVE_50 == {"ACC-PORT-001", "ACC-CHIC-001"}

    def test_below_50_set_has_exactly_two(self):
        assert len(CLIENTS_BELOW_50) == 2
        assert CLIENTS_BELOW_50 == {"ACC-BLWG-001", "ACC-BLWG-002"}


# ─── 8. Constraint layer (policy) ─────────────────────────────────────────────

class TestConstraintLayer:
    """constraint_change_mid_flow uses a policy to detect forbidden clients.

    The constraint layer checks that low-order-count clients are NOT mentioned
    in the final answer after the > 50 filter was applied.
    """

    def test_constraint_passes_when_final_answer_clean(self):
        """Final answer mentions only high-order-count clients → constraint passes."""
        trace = _make_trace(
            _turn(role="user", content="List active clients."),
            _turn(role="assistant", content="All 4 clients listed."),
            _turn(role="user", content="Only those with more than 50 open orders."),
            # Clean final turn: only Portland + Chicago
            _turn(role="assistant", content=(
                "Portland Pickles (75 orders) and Chicago Cubs (100 orders)."
            )),
        )
        result = evaluate_constraint(trace, constraint_change_mid_flow.scenario)
        assert result.passed, f"Expected constraint pass but got: {result.detail}"

    def test_constraint_fails_when_low_order_count_client_present(self):
        """Final answer mentions Bluewing Logistics → constraint fails."""
        trace = _make_trace(
            _turn(role="user", content="List active clients."),
            _turn(role="assistant", content="All 4 clients listed."),
            _turn(role="user", content="Only those with more than 50 open orders."),
            # Failing: includes Bluewing Logistics (below threshold)
            _turn(role="assistant", content=(
                "Portland Pickles (75 orders), Bluewing Logistics (20 orders), "
                "Chicago Cubs (100 orders)."
            )),
        )
        result = evaluate_constraint(trace, constraint_change_mid_flow.scenario)
        assert not result.passed, "Expected constraint fail: low-order-count client present"
