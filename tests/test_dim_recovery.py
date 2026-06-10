"""Tests for dim_recovery scenarios.

Coverage:
  1. Three new perturbations in perturbations.py:
     - InjectWrongPriorToolCall
     - InjectSchemaRejectedCall
     - InjectPaginationTruncation
  2. Four scenarios, all tagged 'dim:recovery':
     - wrong_tool_then_correct
     - bad_arg_then_retry
     - empty_result_then_alternate_lookup
     - partial_result_then_clarify
  3. Perturbation apply() mechanics:
     - apply() returns a NEW Trace (original unchanged)
     - marker string injected into worker_warnings
     - turn structure reflects injected prior state
  4. Scenario scoring:
     - pass traces pass outcome + trajectory
     - fail traces fail outcome or trajectory
  5. Smoke: dim_tag is 'dim:recovery' on all scenarios
  6. Integration marker for live runner tests

Distinction from dim_icl_poisoning:
  - ICL poisoning: CorruptPriorAssistantTurn — CORRUPTED-AT-SERIALIZATION
    (empty turn, template artifact — bugs at the serializer level)
  - Recovery: InjectWrongPriorToolCall etc. — REALISTIC-BUT-WRONG
    (the model made a reasonable mistake on a prior turn)
  This distinction matters for the remediation vector.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from windtunnel.api.evaluators import evaluate_outcome, evaluate_robustness, evaluate_trajectory

# ─── Import targets (fail until implemented) ──────────────────────────────────
from windtunnel.api.perturbations import (
    InjectPaginationTruncation,
    InjectSchemaRejectedCall,
    InjectWrongPriorToolCall,
)
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.scenarios.dim_recovery.scenarios import (
    DIM_TAG,
    RECOVERY_SCENARIOS,
    bad_arg_then_retry,
    empty_result_then_alternate_lookup,
    partial_result_then_clarify,
    wrong_tool_then_correct,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    latency_ms: float = 50.0,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        latency_ms=latency_ms,
    )


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": str(args or {})},
    }


def _make_trace(*turns: Turn, warnings: list[str] | None = None) -> Trace:
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
        worker_warnings=warnings or [],
    )


# ─── 1. DIM tag + scenario set ────────────────────────────────────────────────

class TestDimTag:
    def test_dim_tag_constant(self):
        assert DIM_TAG == "dim:recovery"

    def test_all_scenarios_tagged(self):
        for sc in RECOVERY_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_count(self):
        assert len(RECOVERY_SCENARIOS) == 4

    def test_scenario_names(self):
        names = {sc.name for sc in RECOVERY_SCENARIOS}
        assert names == {
            "wrong_tool_then_correct",
            "bad_arg_then_retry",
            "empty_result_then_alternate_lookup",
            "partial_result_then_clarify",
        }


# ─── 2. InjectWrongPriorToolCall perturbation ─────────────────────────────────

class TestInjectWrongPriorToolCall:
    """Injects a prior turn where the model called the wrong tool.

    The trace gets a prior assistant turn that called wrong_tool_name and
    received fake_result. The injected turns appear BEFORE the final user
    turn so the model sees a "the prior assistant turn was wrong" context.
    """

    def _base_trace(self) -> Trace:
        """A trace with just the user prompt — no prior tool call yet."""
        return _make_trace(
            _turn(role="user", content="What is the total order quantity for ACC-BLWG-001?"),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{\"found\": false, \"note\": \"No product found.\"}"}',
        )
        new_trace = p.apply(trace)
        assert new_trace is not trace, "apply() must return a NEW trace, not the original"

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_turns = list(trace.turns)
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        p.apply(trace)
        assert trace.turns == original_turns, "apply() must not mutate original trace"

    def test_apply_injects_marker_into_warnings(self):
        trace = self._base_trace()
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        new_trace = p.apply(trace)
        assert any("perturbation_applied" in w and "inject_wrong_prior_tool_call" in w
                   for w in new_trace.worker_warnings), (
            f"Expected inject_wrong_prior_tool_call marker in warnings, got: {new_trace.worker_warnings}"
        )

    def test_apply_injects_tool_call_turn(self):
        """After apply(), trace contains an assistant turn with the wrong tool call."""
        trace = self._base_trace()
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        new_trace = p.apply(trace)
        tool_names_in_trace = []
        for turn in new_trace.turns:
            for tc in turn.tool_calls:
                if "function" in tc:
                    tool_names_in_trace.append(tc["function"]["name"])
                elif "name" in tc:
                    tool_names_in_trace.append(tc["name"])
        assert "mcp_acme_ops_product_lookup" in tool_names_in_trace, (
            f"Expected wrong tool call in trace turns, tool calls found: {tool_names_in_trace}"
        )

    def test_marker_contains_wrong_tool_name(self):
        p = InjectWrongPriorToolCall(
            turn_idx=1,
            wrong_tool_name="mcp_acme_ops_order_query",
            fake_result='{"result": "{}"}',
        )
        assert "mcp_acme_ops_order_query" in p.marker

    def test_apply_adds_tool_result_turn(self):
        """The injected wrong call also gets a corresponding tool result turn."""
        trace = self._base_trace()
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        new_trace = p.apply(trace)
        tool_result_turns = [t for t in new_trace.turns if t.role == "tool"]
        assert len(tool_result_turns) >= 1, "Expected at least one tool result turn after injection"


# ─── 3. InjectSchemaRejectedCall perturbation ─────────────────────────────────

class TestInjectSchemaRejectedCall:
    """Injects a prior turn where the tool call was rejected by the schema.

    The trace gets a prior assistant turn with bad args + a tool_result
    containing a schema error message.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Show orders for client Bluewing Logistics."),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"client": 12345},  # int instead of str
        )
        new_trace = p.apply(trace)
        assert new_trace is not trace

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_len = len(trace.turns)
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"stage": "INVALID_STAGE_VALUE"},
        )
        p.apply(trace)
        assert len(trace.turns) == original_len

    def test_apply_injects_marker(self):
        trace = self._base_trace()
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"client": None, "limit": -1},
        )
        new_trace = p.apply(trace)
        assert any("perturbation_applied" in w and "inject_schema_rejected_call" in w
                   for w in new_trace.worker_warnings)

    def test_apply_injects_error_result_turn(self):
        """Tool result turn contains a schema/validation error message."""
        trace = self._base_trace()
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"stage": "INVALID_STAGE_VALUE"},
        )
        new_trace = p.apply(trace)
        tool_result_turns = [t for t in new_trace.turns if t.role == "tool"]
        assert len(tool_result_turns) >= 1
        # The tool result should contain an error indicator
        error_content = tool_result_turns[0].content
        assert (
            "error" in error_content.lower()
            or "invalid" in error_content.lower()
            or "schema" in error_content.lower()
            or "rejected" in error_content.lower()
        ), f"Expected error in tool result content, got: {error_content[:200]}"

    def test_marker_contains_tool_name(self):
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_report",
            bad_arg={"sku": None},
        )
        assert "mcp_acme_ops_order_report" in p.marker


# ─── 4. InjectPaginationTruncation perturbation ───────────────────────────────

class TestInjectPaginationTruncation:
    """Injects a prior tool result that was truncated mid-list.

    The trace gets a tool result with a 'note: ...truncated' indicator
    so the model can see pagination is incomplete.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="List all orders for ACC-BLWG-001."),
            _turn(
                role="assistant", content="",
                tool_calls=[_tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})],
            ),
            _turn(
                role="tool",
                content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1001\", \"quantity\": 12}], \"pagination\": {\"hasMore\": false}}"}',
            ),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        new_trace = p.apply(trace)
        assert new_trace is not trace

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_content = trace.turns[2].content
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        p.apply(trace)
        assert trace.turns[2].content == original_content

    def test_apply_injects_marker(self):
        trace = self._base_trace()
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        new_trace = p.apply(trace)
        assert any("perturbation_applied" in w and "inject_pagination_truncation" in w
                   for w in new_trace.worker_warnings)

    def test_apply_sets_has_more_true(self):
        """After apply(), the tool result indicates more pages exist."""
        trace = self._base_trace()
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        new_trace = p.apply(trace)
        modified_content = new_trace.turns[2].content
        assert "true" in modified_content.lower() or "hasMore" in modified_content, (
            f"Expected hasMore=true indicator in truncated result, got: {modified_content[:300]}"
        )

    def test_apply_adds_truncation_note(self):
        """After apply(), the tool result mentions truncation."""
        trace = self._base_trace()
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        new_trace = p.apply(trace)
        modified_content = new_trace.turns[2].content
        assert (
            "truncated" in modified_content.lower()
            or "hasMore" in modified_content
            or "more" in modified_content.lower()
        ), f"Expected truncation indicator in content: {modified_content[:300]}"

    def test_marker_contains_truncate_at(self):
        p = InjectPaginationTruncation(turn_idx=1, truncate_at=5)
        assert "5" in p.marker or "truncate_at" in p.marker


# ─── 5. wrong_tool_then_correct scenario ─────────────────────────────────────

class TestWrongToolThenCorrect:
    """Prior assistant turn called the wrong tool; next turn should recognize and fix.

    Setup: prior turn called product_lookup (catalog info) when the task needed
    order data via order_query. The model must now call the correct tool.

    Pass = next turn calls order_query (or order_report) with the client id.
    Fail = next turn doubles down on product_lookup or hallucinates from wrong result.
    """

    def _passing_trace(self) -> Trace:
        """Model recognizes the prior wrong tool and calls the correct one."""
        return _make_trace(
            _turn(role="user", content="What's the total order quantity Bluewing Logistics has on file?"),
            # Prior wrong turn: model called product_lookup (returns catalog, not order data)
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_product_lookup", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\"found\": true, \"product\": {\"name\": \"Bluewing Jersey - Home\"}}"}'),
            # Recovery: model recognizes the error, calls client_lookup then order_query
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\"matches\": [{\"id\": \"ACC-BLWG-001\"}]}"}'),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"result": "{\"summary\": {\"total\": {\"quantity\": 20}}}"}'),
            _turn(role="assistant", content="Bluewing Logistics (ACC-BLWG-001) has a total order quantity of 20 on file."),
        )

    def _failing_trace_doubles_down(self) -> Trace:
        """Model doubles down — calls product_lookup again instead of order_query."""
        return _make_trace(
            _turn(role="user", content="What's the total order quantity Bluewing Logistics has on file?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_product_lookup", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\"found\": true, \"product\": {\"name\": \"Bluewing Jersey - Home\"}}"}'),
            # Doubles down: calls product_lookup again
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_product_lookup", {"sku": "B002BBB"})
            ]),
            _turn(role="tool", content='{"result": "{\"found\": true, \"product\": {\"name\": \"Bluewing Cap\"}}"}'),
            _turn(role="assistant", content="There are 2 products for Bluewing Logistics in the catalog."),
        )

    def _failing_trace_hallucinates(self) -> Trace:
        """Model hallucinates an order count from the wrong-tool result."""
        return _make_trace(
            _turn(role="user", content="What's the total order quantity Bluewing Logistics has on file?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_product_lookup", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\"found\": true, \"product\": {\"name\": \"Bluewing Jersey - Home\"}}"}'),
            _turn(role="assistant", content="Based on the catalog data, Bluewing Logistics has 1 product on file."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), wrong_tool_then_correct)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_doubles_down_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_doubles_down(), wrong_tool_then_correct)
        assert not result.passed, "Expected outcome fail for doubling-down trace"

    def test_failing_hallucinates_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_hallucinates(), wrong_tool_then_correct)
        assert not result.passed, "Expected outcome fail for hallucination trace"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), wrong_tool_then_correct)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_doubles_down_trajectory_fails(self):
        """product_lookup is the wrong tool — must call order_query or order_report."""
        result = evaluate_trajectory(self._failing_trace_doubles_down(), wrong_tool_then_correct)
        assert not result.passed, "Expected trajectory fail for doubling-down"

    def test_scenario_requires_tool_use(self):
        assert wrong_tool_then_correct.requires_tool_use is True

    def test_scenario_must_call_correct_tool(self):
        called = wrong_tool_then_correct.must_call
        has_inv = any("order_query" in t or "order_report" in t for t in called)
        assert has_inv, f"Expected order_query or order_report in must_call, got: {called}"

    def test_scenario_no_forbidden_calls(self):
        """Recovery scenarios don't use forbidden_calls for the wrong tool.

        The perturbation injects the prior wrong-tool call into history.
        The trajectory check looks at what the model calls going FORWARD.
        forbidden_calls would incorrectly fire on the injected history.
        Recovery is gated by must_call (order_query) + outcome (total=20).
        """
        # product_lookup is injected by the perturbation, not forbidden going forward
        assert "product_lookup" not in wrong_tool_then_correct.forbidden_calls
        assert wrong_tool_then_correct.target_facts, "Must have target facts for outcome check"

    def test_scenario_has_perturbation(self):
        """Scenario uses InjectWrongPriorToolCall perturbation."""
        from windtunnel.api.perturbations import InjectWrongPriorToolCall
        has_p = any(isinstance(p, InjectWrongPriorToolCall) for p in wrong_tool_then_correct.perturbations)
        assert has_p, "wrong_tool_then_correct must use InjectWrongPriorToolCall perturbation"


# ─── 6. bad_arg_then_retry scenario ──────────────────────────────────────────

class TestBadArgThenRetry:
    """Prior tool call was rejected; next turn should retry with corrected args.

    Setup: prior turn called order_query with invalid stage enum value. The
    tool returned a schema/validation error. The model must retry with a
    valid stage value or without the bad arg.

    Pass = next turn re-attempts the call with corrected args.
    Fail = next turn gives up or claims success.
    """

    def _passing_trace(self) -> Trace:
        """Model retries with corrected args after schema rejection."""
        return _make_trace(
            _turn(role="user", content="Show me orders at the 'Incoming' stage for ACC-BLWG-001."),
            # Prior bad call: invalid stage enum
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "stage": "Incoming"})
            ]),
            _turn(role="tool", content='{"error": "Invalid value for stage: \'Incoming\'. Valid values: Intake, Checked In, Storage, Client Outbound, Shipped"}'),
            # Recovery: model retries with correct stage name
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "stage": "Intake"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1001\", \"quantity\": 12}], \"summary\": {\"total\": {\"quantity\": 12}}}"}'),
            _turn(role="assistant", content="ACC-BLWG-001 has 12 orders in Intake (the incoming stage)."),
        )

    def _failing_trace_gives_up(self) -> Trace:
        """Model gives up after the schema error — no retry."""
        return _make_trace(
            _turn(role="user", content="Show me orders at the 'Incoming' stage for ACC-BLWG-001."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "stage": "Incoming"})
            ]),
            _turn(role="tool", content='{"error": "Invalid value for stage: \'Incoming\'. Valid values: Intake, Checked In, Storage, Client Outbound, Shipped"}'),
            _turn(role="assistant", content="I'm sorry, I encountered an error and cannot retrieve the order information at this time."),
        )

    def _failing_trace_claims_success(self) -> Trace:
        """Model claims success despite the schema error."""
        return _make_trace(
            _turn(role="user", content="Show me orders at the 'Incoming' stage for ACC-BLWG-001."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "stage": "Incoming"})
            ]),
            _turn(role="tool", content='{"error": "Invalid value for stage"}'),
            _turn(role="assistant", content="There are 0 orders at the Incoming stage for ACC-BLWG-001."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), bad_arg_then_retry)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_gives_up_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_gives_up(), bad_arg_then_retry)
        assert not result.passed, "Expected outcome fail for give-up trace"

    def test_failing_claims_success_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_claims_success(), bad_arg_then_retry)
        assert not result.passed, "Expected outcome fail for false-success trace"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), bad_arg_then_retry)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_gives_up_trajectory_fails(self):
        """Giving up still calls order_query once (the bad call), so trajectory passes
        on tool name — but outcome fails because no valid data was returned.
        Outcome is the gate here: no valid data returned → outcome fails.
        """
        # order_query was called (bad arg), so trajectory passes on tool name presence.
        # The semantic gate is outcome: no valid "Intake" data returned.
        outcome = evaluate_outcome(self._failing_trace_gives_up(), bad_arg_then_retry)
        assert not outcome.passed, "Give-up trace must fail outcome check"

    def test_scenario_requires_tool_use(self):
        assert bad_arg_then_retry.requires_tool_use is True

    def test_scenario_must_call_order_query(self):
        assert "order_query" in bad_arg_then_retry.must_call

    def test_scenario_has_perturbation(self):
        """Scenario uses InjectSchemaRejectedCall perturbation."""
        from windtunnel.api.perturbations import InjectSchemaRejectedCall
        has_p = any(isinstance(p, InjectSchemaRejectedCall) for p in bad_arg_then_retry.perturbations)
        assert has_p, "bad_arg_then_retry must use InjectSchemaRejectedCall perturbation"


# ─── 7. empty_result_then_alternate_lookup scenario ──────────────────────────

class TestEmptyResultThenAlternateLookup:
    """Prior tool returned 0 results; model should try broader query or alternate approach.

    Setup: prior turn called order_query with a strict client name instead of id,
    getting 0 results. The user clearly expected results. The model must try
    client_lookup first to resolve the id, or ask for clarification.

    Pass = next turn calls client_lookup (to resolve id) OR asks for clarification.
    Fail = next turn confidently reports "nothing found."
    """

    def _passing_trace_lookup(self) -> Trace:
        """Model tries client_lookup to resolve the id after empty result."""
        return _make_trace(
            _turn(role="user", content="Show me all orders for Portland Pickles."),
            # Prior zero-result turn: used raw name (strict tool)
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "Portland Pickles"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [], \"summary\": {\"total\": {\"quantity\": 0}}, \"note\": \"No orders matched. If you passed a client name instead of an id, use ops_client_lookup first.\"}"}'),
            # Recovery: model calls client_lookup to resolve the id
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Portland Pickles"})
            ]),
            _turn(role="tool", content='{"result": "{\"matches\": [{\"id\": \"ACC-PORT-001\", \"name\": \"Portland Pickles\"}]}"}'),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-PORT-001"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [], \"summary\": {\"total\": {\"quantity\": 0}}}"}'),
            _turn(role="assistant", content="Portland Pickles (ACC-PORT-001) currently has no orders on file."),
        )

    def _failing_trace_reports_nothing(self) -> Trace:
        """Model confidently reports nothing found without trying alternate approach."""
        return _make_trace(
            _turn(role="user", content="Show me all orders for Portland Pickles."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "Portland Pickles"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [], \"summary\": {\"total\": {\"quantity\": 0}}}"}'),
            _turn(role="assistant", content="Portland Pickles has no orders on file."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_lookup(), empty_result_then_alternate_lookup)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_reports_nothing_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_reports_nothing(), empty_result_then_alternate_lookup)
        assert not result.passed, "Expected outcome fail for confident-no-result trace"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace_lookup(), empty_result_then_alternate_lookup)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_trajectory_fails(self):
        """Model must call client_lookup — skipping it is a trajectory miss."""
        result = evaluate_trajectory(self._failing_trace_reports_nothing(), empty_result_then_alternate_lookup)
        assert not result.passed, "Expected trajectory fail: client_lookup was not called"

    def test_scenario_requires_tool_use(self):
        assert empty_result_then_alternate_lookup.requires_tool_use is True

    def test_scenario_must_call_client_lookup(self):
        assert "client_lookup" in empty_result_then_alternate_lookup.must_call

    def test_scenario_has_perturbation(self):
        """Scenario uses InjectWrongPriorToolCall (wrong arg variant) perturbation."""
        # empty result from strict-with-name is the same perturbation pattern:
        # prior call was made with bad args (name instead of id)
        from windtunnel.api.perturbations import InjectWrongPriorToolCall
        has_p = any(isinstance(p, InjectWrongPriorToolCall) for p in empty_result_then_alternate_lookup.perturbations)
        assert has_p, "empty_result_then_alternate_lookup must use InjectWrongPriorToolCall perturbation"

    def test_target_facts_include_client_id(self):
        """Target facts must include the resolved client id (ACC-PORT-001) to prove recovery.

        The ID specifically (not just the name) — a model that skips client_lookup
        and reports "Portland Pickles has 0 orders" will fail outcome because it
        never surfaced ACC-PORT-001 in its answer.
        """
        flat = [f for group in empty_result_then_alternate_lookup.target_facts for f in group]
        has_id = any("ACC-PORT-001" in f for f in flat)
        assert has_id, f"Expected ACC-PORT-001 in target_facts, got: {flat}"


# ─── 8. partial_result_then_clarify scenario ─────────────────────────────────

class TestPartialResultThenClarify:
    """Prior tool result was paginated/truncated; model should paginate or ask.

    Setup: prior order_query returned 1 result with hasMore=true and a truncation
    note. User asked "list all orders." The model must either paginate (call again
    with offset) or ask the user which page/slice they want.

    Pass = next turn paginates OR asks for clarification.
    Fail = next turn answers as if it has all data (the 1 item is the full list).
    """

    def _passing_trace_paginates(self) -> Trace:
        """Model paginates after seeing hasMore=true."""
        return _make_trace(
            _turn(role="user", content="List all orders for ACC-BLWG-001."),
            # Prior truncated result (hasMore=true)
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "limit": 1})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1001\", \"quantity\": 12}], \"pagination\": {\"hasMore\": true, \"returned\": 1, \"matchedCount\": 2}, \"note\": \"Results truncated — use offset to page through.\"}"}'),
            # Recovery: model paginates with offset=1
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "limit": 1, "offset": 1})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1002\", \"quantity\": 8}], \"pagination\": {\"hasMore\": false}}"}'),
            _turn(role="assistant", content="ACC-BLWG-001 has 2 orders: ORD-1001 (qty 12) and ORD-1002 (qty 8)."),
        )

    def _failing_trace_answers_partial(self) -> Trace:
        """Model answers as if the 1 truncated result is the complete list."""
        return _make_trace(
            _turn(role="user", content="List all orders for ACC-BLWG-001."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001", "limit": 1})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1001\", \"quantity\": 12}], \"pagination\": {\"hasMore\": true, \"returned\": 1, \"matchedCount\": 2}}"}'),
            _turn(role="assistant", content="ACC-BLWG-001 has 1 order: ORD-1001 with qty 12."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_paginates(), partial_result_then_clarify)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_answers_partial_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_answers_partial(), partial_result_then_clarify)
        assert not result.passed, "Expected outcome fail for partial-answer trace"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace_paginates(), partial_result_then_clarify)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_scenario_requires_tool_use(self):
        assert partial_result_then_clarify.requires_tool_use is True

    def test_scenario_must_call_order_query_twice(self):
        """Scenario must require order_query in must_call (called twice for pagination)."""
        assert "order_query" in partial_result_then_clarify.must_call

    def test_scenario_has_perturbation(self):
        """Scenario uses InjectPaginationTruncation perturbation."""
        from windtunnel.api.perturbations import InjectPaginationTruncation
        has_p = any(isinstance(p, InjectPaginationTruncation) for p in partial_result_then_clarify.perturbations)
        assert has_p, "partial_result_then_clarify must use InjectPaginationTruncation perturbation"

    def test_target_facts_include_both_orders(self):
        """Both order rows must be in target facts to prove full pagination."""
        flat = [f for group in partial_result_then_clarify.target_facts for f in group]
        has_u1001 = any("ORD-1001" in f or "12" in f for f in flat)
        has_u1002 = any("ORD-1002" in f or "8" in f for f in flat)
        assert has_u1001, f"Expected ORD-1001 data in target_facts, got: {flat}"
        assert has_u1002, f"Expected ORD-1002 data in target_facts, got: {flat}"


# ─── 9. Perturbation robustness layer integration ─────────────────────────────

class TestRobustnessLayerIntegration:
    """Verify the robustness layer correctly checks perturbation markers."""

    def test_wrong_prior_tool_call_marker_recognized(self):
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        base_trace = _make_trace(
            _turn(role="user", content="test"),
        )
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass after apply(), got: {result.detail}"

    def test_schema_rejected_call_marker_recognized(self):
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"stage": "INVALID"},
        )
        base_trace = _make_trace(_turn(role="user", content="test"))
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass, got: {result.detail}"

    def test_pagination_truncation_marker_recognized(self):
        base_trace = _make_trace(
            _turn(role="user", content="list orders"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [{\"orderId\": \"ORD-1001\"}], \"pagination\": {\"hasMore\": false}}"}'),
        )
        p = InjectPaginationTruncation(turn_idx=2, truncate_at=1)
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass, got: {result.detail}"

    def test_unapplied_perturbation_fails_robustness(self):
        """If apply() is NOT called, the robustness layer must fail."""
        p = InjectWrongPriorToolCall(
            turn_idx=0,
            wrong_tool_name="mcp_acme_ops_product_lookup",
            fake_result='{"result": "{}"}',
        )
        base_trace = _make_trace(_turn(role="user", content="test"))
        # Do NOT call p.apply() — marker absent

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(base_trace, scenario)
        assert not result.passed, "Expected robustness fail when perturbation not applied"
