"""Tests for dim_tool_affordance scenarios — TDD red phase.

Coverage:
  1. Scenario tags field exists on Scenario and is populated on all 3 scenarios
  2. All 3 scenarios tagged 'dim:tool_affordance'
  3. Unit: outcome evaluator — right-path (pass) and wrong-path (fail) for each scenario
  4. Unit: trajectory evaluator catches wrong-tool-call case
     (e.g. order_report called with raw name when client_lookup should come first)
  5. Mock MCP synthetic_db contract consistency:
     - client_lookup is lenient (substring by name)
     - order_report/order_query are strict (exact client id required)
     - product_lookup is by SKU
  6. Scenario fields: must_call, forbidden_calls, order_matters, requires_tool_use all set
  7. Integration marker: pytest.mark.integration for live end-to-end tests
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from windtunnel.api.evaluators import evaluate_outcome, evaluate_trajectory
from windtunnel.api.trace import Trace, Turn, compute_hash

# ─── Import targets (fail until implemented) ──────────────────────────────────
from windtunnel.scenarios.dim_tool_affordance.scenarios import (
    CSV_DELIVERY_MARKERS,
    DIM_TAG,
    TOOL_AFFORDANCE_SCENARIOS,
    bulk_table_to_csv,
    export_customer_products,
    field_scope_inference,
    investigate_before_export,
    lookup_before_action,
    wrong_tool_correction,
)
from windtunnel.scenarios.dim_tool_affordance.synthetic_db import (
    CLIENTS,
    PRODUCTS,
    find_clients,
    order_report,
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


# ─── 1. Scenario tags field ───────────────────────────────────────────────────

class TestScenarioTags:
    """The Scenario dataclass must have a tags field."""

    def test_scenario_has_tags_field(self):
        from windtunnel.api.scenario import Scenario
        s = Scenario(name="test", prompt="hello?", target_facts=[["yes"]], tags=["dim:test"])
        assert s.tags == ["dim:test"]

    def test_scenario_tags_default_empty(self):
        from windtunnel.api.scenario import Scenario
        s = Scenario(name="test", prompt="hello?", target_facts=[["yes"]])
        assert s.tags == []

    def test_dim_tag_constant(self):
        assert DIM_TAG == "dim:tool_affordance"

    def test_all_three_scenarios_tagged(self):
        for sc in TOOL_AFFORDANCE_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_set_has_exactly_six(self):
        # 6: 3 base + investigate_before_export + export_customer_products +
        # bulk_table_to_csv (all CSV scenarios now use deliver_as, not csv_export).
        assert len(TOOL_AFFORDANCE_SCENARIOS) == 6

    def test_scenario_names(self):
        names = {sc.name for sc in TOOL_AFFORDANCE_SCENARIOS}
        assert names == {
            "lookup_before_action",
            "field_scope_inference",
            "wrong_tool_correction",
            "investigate_before_export",
            "export_customer_products",
            "bulk_table_to_csv",
        }


# ─── 2. lookup_before_action scenario ────────────────────────────────────────

class TestLookupBeforeAction:
    """User gives a client name; model must call client_lookup BEFORE the order query.

    Pass = order query called with resolved client id (ACC-BLWG-001), not raw name.
    Fail = order query/report called with raw name (returns 0), model reports 0.
    """

    def _passing_trace(self) -> Trace:
        """Model correctly calls client_lookup first, then uses resolved id."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\"}]}"}'),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"result": "{\\"summary\\": {\\"total\\": {\\"quantity\\": 20}}}"}'),
            _turn(role="assistant", content="Bluewing Logistics (ACC-BLWG-001) has 20 items across their open orders."),
        )

    def _failing_trace_raw_name(self) -> Trace:
        """Model skips client_lookup, calls order_query with raw name — gets 0."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"summary\\": {\\"total\\": {\\"quantity\\": 0}}}"}'),
            _turn(role="assistant", content="Bluewing Logistics has 0 items on order."),
        )

    def _failing_trace_no_tools(self) -> Trace:
        """Model refuses/hedges without calling any tools."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content="I don't have access to order data for that client."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), lookup_before_action)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_raw_name_outcome_fails(self):
        """Raw-name shortcut returns 0 → answer missing '20' → outcome FAIL."""
        result = evaluate_outcome(self._failing_trace_raw_name(), lookup_before_action)
        assert not result.passed, "Expected outcome fail for raw-name shortcut"

    def test_no_tools_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_no_tools(), lookup_before_action)
        assert not result.passed, "Expected outcome fail for no-tools trace"

    def test_passing_trace_trajectory_passes(self):
        """client_lookup must be called before order_query."""
        result = evaluate_trajectory(self._passing_trace(), lookup_before_action)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_raw_name_trajectory_fails(self):
        """order_query called without client_lookup first violates must_call."""
        result = evaluate_trajectory(self._failing_trace_raw_name(), lookup_before_action)
        assert not result.passed, "Expected trajectory fail: client_lookup was not called"

    def test_scenario_requires_tool_use(self):
        assert lookup_before_action.requires_tool_use is True

    def test_scenario_must_call_client_lookup(self):
        assert "client_lookup" in lookup_before_action.must_call

    def test_scenario_must_call_order_query_or_report(self):
        called = lookup_before_action.must_call
        has_query = any("order_query" in t or "order_report" in t for t in called)
        assert has_query, f"Expected order_query or order_report in must_call, got: {called}"

    def test_scenario_order_matters(self):
        """client_lookup must come BEFORE the query tool."""
        assert lookup_before_action.order_matters is True

    def test_target_facts_present(self):
        assert len(lookup_before_action.target_facts) >= 1


# ─── 3. field_scope_inference scenario ───────────────────────────────────────

class TestFieldScopeInference:
    """Ask for a field available via client_lookup but framed as 'external'.

    This reproduces the specific_field_lookup regression: the model should
    call ops_client_lookup and return the email. It must NOT refuse with
    'I don't have access to external contact information'.

    Pass = model calls client_lookup, returns ops@bluewing.example.
    Fail = model refuses/hedges, calls 0 tools.
    """

    def _passing_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="What's the contact email for Bluewing Logistics?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"clientEmail\\": \\"ops@bluewing.example\\"}]}"}'),
            _turn(role="assistant", content="The contact email for Bluewing Logistics is ops@bluewing.example."),
        )

    def _failing_trace_hedge(self) -> Trace:
        """Model hedges — says it doesn't have access to external info."""
        return _make_trace(
            _turn(role="user", content="What's the contact email for Bluewing Logistics?"),
            _turn(role="assistant", content="I don't have access to external contact information for Bluewing Logistics through the back-office tools available to me."),
        )

    def _failing_trace_wrong_answer(self) -> Trace:
        """Model calls tools but returns wrong email."""
        return _make_trace(
            _turn(role="user", content="What's the contact email for Bluewing Logistics?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"clientEmail\\": \\"ops@bluewing.example\\"}]}"}'),
            _turn(role="assistant", content="The contact email is jane@example.com."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), field_scope_inference)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_hedge_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_hedge(), field_scope_inference)
        assert not result.passed, "Expected outcome fail for hedge (no tools used)"

    def test_failing_wrong_answer_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_wrong_answer(), field_scope_inference)
        assert not result.passed, "Expected outcome fail for wrong email"

    def test_passing_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), field_scope_inference)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_hedge_trajectory_fails(self):
        """client_lookup was not called at all."""
        result = evaluate_trajectory(self._failing_trace_hedge(), field_scope_inference)
        assert not result.passed, "Expected trajectory fail for hedge (no tool calls)"

    def test_scenario_requires_tool_use(self):
        assert field_scope_inference.requires_tool_use is True

    def test_scenario_must_call_client_lookup(self):
        assert "client_lookup" in field_scope_inference.must_call

    def test_target_fact_is_email(self):
        flat = [f for group in field_scope_inference.target_facts for f in group]
        assert "ops@bluewing.example" in flat


# ─── 4. wrong_tool_correction scenario ───────────────────────────────────────

class TestWrongToolCorrection:
    """User implies a wrong tool fits the question; model should re-pick.

    'Show me catalog info for SKU B001AAA' — catalog info = product_lookup,
    NOT order_query or order_report. The model must recognize 'catalog info'
    means product description/metadata, not order records.

    Pass = product_lookup called with B001AAA, returns product description.
    Fail = model calls order_query or order_report instead.
    """

    def _passing_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Show me the catalog info for SKU B001AAA."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_product_lookup", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\\"product\\": {\\"name\\": \\"Bluewing Jersey - Home\\", \\"description\\": \\"Official home jersey, machine washable.\\", \\"brand\\": \\"BluewingGear\\"}}"}'),
            _turn(role="assistant", content="Catalog info for B001AAA — Bluewing Jersey - Home: Official home jersey, machine washable. Brand: BluewingGear."),
        )

    def _failing_trace_wrong_tool(self) -> Trace:
        """Model calls order_report instead of product_lookup for catalog info."""
        return _make_trace(
            _turn(role="user", content="Show me the catalog info for SKU B001AAA."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_report", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\\"byStage\\": {\\"Intake\\": {\\"quantity\\": 12}}}"}'),
            _turn(role="assistant", content="For SKU B001AAA, there are 12 items in the Intake stage."),
        )

    def _failing_trace_order_query(self) -> Trace:
        """Model calls order_query for catalog info."""
        return _make_trace(
            _turn(role="user", content="Show me the catalog info for SKU B001AAA."),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"sku": "B001AAA"})
            ]),
            _turn(role="tool", content='{"result": "{\\"orders\\": [{\\"orderId\\": \\"ORD-1001\\"}]}"}'),
            _turn(role="assistant", content="SKU B001AAA appears on order ORD-1001."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), wrong_tool_correction)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_wrong_tool_outcome_fails(self):
        """Order data does not contain the product description — outcome fails."""
        result = evaluate_outcome(self._failing_trace_wrong_tool(), wrong_tool_correction)
        assert not result.passed, "Expected outcome fail for wrong tool (order report instead of catalog)"

    def test_failing_order_query_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_order_query(), wrong_tool_correction)
        assert not result.passed, "Expected outcome fail for order_query used for catalog info"

    def test_passing_trace_trajectory_passes(self):
        result = evaluate_trajectory(self._passing_trace(), wrong_tool_correction)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_wrong_tool_trajectory_fails(self):
        """order_report is forbidden for a catalog-info request."""
        result = evaluate_trajectory(self._failing_trace_wrong_tool(), wrong_tool_correction)
        assert not result.passed, "Expected trajectory fail: order_report is forbidden for catalog"

    def test_order_query_trajectory_fails(self):
        """order_query is also forbidden for catalog info."""
        result = evaluate_trajectory(self._failing_trace_order_query(), wrong_tool_correction)
        assert not result.passed, "Expected trajectory fail: order_query is forbidden for catalog"

    def test_scenario_must_call_product_lookup(self):
        assert "product_lookup" in wrong_tool_correction.must_call

    def test_scenario_forbids_order_report(self):
        assert "order_report" in wrong_tool_correction.forbidden_calls

    def test_scenario_forbids_order_query(self):
        assert "order_query" in wrong_tool_correction.forbidden_calls

    def test_target_facts_include_product_description(self):
        flat = [f for group in wrong_tool_correction.target_facts for f in group]
        # At minimum the product name or description must be a target
        has_product_info = any(
            "Bluewing Jersey" in f or "machine washable" in f or "BluewingGear" in f
            for f in flat
        )
        assert has_product_info, f"Expected product description in target_facts, got: {flat}"


# ─── 5. Mock MCP synthetic_db contract ───────────────────────────────────────

class TestSyntheticDbContracts:
    """Verify that the mock DB enforces the correct strict/lenient split.

    The tool description contract must match the implementation:
    - client_lookup: lenient (substring by name or id)
    - order_query / order_report: strict (exact client id required)
    - product_lookup: by SKU
    """

    def test_find_clients_by_name_substring(self):
        """client_lookup lenient: partial name match works."""
        results = find_clients(query="Bluewing")
        names = [c["name"] for c in results]
        assert "Bluewing Logistics" in names
        assert "Bluewing Concessions" in names

    def test_find_clients_by_exact_id(self):
        """client_lookup lenient: exact id match also works."""
        results = find_clients(query="ACC-BLWG-001")
        assert len(results) == 1
        assert results[0]["id"] == "ACC-BLWG-001"

    def test_find_clients_returns_email(self):
        """client_lookup result contains clientEmail field."""
        results = find_clients(query="Bluewing Logistics")
        assert any(c.get("clientEmail") == "ops@bluewing.example" for c in results)

    def test_query_orders_strict_name_returns_empty(self):
        """order_query strict: raw client name returns 0 results (forces chaining)."""
        results = query_orders(client="Bluewing Logistics")
        assert results == [], (
            "order_query must return empty for raw name — model must call client_lookup first"
        )

    def test_query_orders_strict_id_returns_results(self):
        """order_query strict: exact client id returns results."""
        results = query_orders(client="ACC-BLWG-001")
        assert len(results) > 0

    def test_order_report_strict_name_returns_zeros(self):
        """order_report strict: raw name → all zeros."""
        summary = order_report(client="Bluewing Logistics")
        total_qty = sum(b["quantity"] for b in summary.values())
        assert total_qty == 0, (
            "order_report must return zeros for raw name — forces chaining"
        )

    def test_order_report_strict_id_returns_results(self):
        """order_report strict: exact id → real quantities."""
        summary = order_report(client="ACC-BLWG-001")
        total_qty = sum(b["quantity"] for b in summary.values())
        assert total_qty == 20  # 12 + 8 for ACC-BLWG-001

    def test_product_lookup_by_sku(self):
        """product_lookup returns catalog entry for known SKU."""
        assert "B001AAA" in PRODUCTS
        p = PRODUCTS["B001AAA"]
        assert "Bluewing Jersey" in p["name"]
        assert "BluewingGear" in p.get("brand", "")

    def test_clients_have_expected_ids(self):
        ids = {c["id"] for c in CLIENTS}
        assert "ACC-BLWG-001" in ids
        assert "ACC-BLWG-002" in ids
        assert "ACC-CHIC-001" in ids


# ─── 5b. CSV-delivery scenarios (deliver_as="csv", not a csv_export tool) ─────

class TestCSVDeliveryScenarios:
    """investigate_before_export / export_customer_products / bulk_table_to_csv.

    FIDELITY: production has no csv_export tool. CSV delivery is deliver_as="csv"
    on a read-only tool → the server returns a DEFERRED async ack and posts the
    link as a follow-up. So a correct reply RELAYS that async/follow-up framing
    (CSV_DELIVERY_MARKERS); the observed production failure (a markdown table)
    and a fabricated inline link both hit none of those markers and must FAIL.
    """

    CSV_SCENARIOS = [
        investigate_before_export,
        export_customer_products,
        bulk_table_to_csv,
    ]

    def test_csv_scenarios_use_delivery_markers_and_require_lookup(self):
        for sc in self.CSV_SCENARIOS:
            assert sc.requires_tool_use is True, sc.name
            assert "client_lookup" in sc.must_call, sc.name
            # No phantom csv_export tool may be required anywhere.
            assert "csv_export" not in sc.must_call, sc.name
            # The CSV-delivery marker group must be a target group.
            assert CSV_DELIVERY_MARKERS in sc.target_facts, sc.name

    def test_deferred_ack_relay_passes(self):
        # The model resolved the bulk client AND relayed the deferred CSV ack.
        good = _make_trace(
            _turn(tool_calls=[_tool_call("mcp_acme_ops_client_lookup",
                                         {"query": "Seattle Steelheads"})]),
            _turn(tool_calls=[_tool_call("mcp_acme_ops_order_query",
                                         {"client": "ACC-STLH-001", "stage": "Storage",
                                          "deliver_as": "csv"})]),
            _turn(content=(
                "I've started the export of the Seattle Steelheads (ACC-STLH-001) "
                "Storage orders. The CSV file will arrive as a follow-up "
                "message when it's ready."
            )),
        )
        assert evaluate_outcome(good, bulk_table_to_csv).passed

    def test_markdown_table_fails(self):
        # The OBSERVED production failure: resolved the client (trajectory fine)
        # but dumped a markdown pipe-table that the chat surface won't render.
        bad = _make_trace(
            _turn(tool_calls=[_tool_call("mcp_acme_ops_client_lookup",
                                         {"query": "Seattle Steelheads"})]),
            _turn(content=(
                "Here is the Storage order list for Seattle Steelheads:\n\n"
                "| SKU | Quantity in Storage |\n|------|------|\n"
                "| B200STL | 5 |\n| B201STL | 12 |\n| B202STL | 19 |\n"
            )),
        )
        assert not evaluate_outcome(bad, bulk_table_to_csv).passed
        # Trajectory is fine (client_lookup was called) — this is purely a
        # delivery-FORMAT failure, which the outcome gate catches.
        assert evaluate_trajectory(bad, bulk_table_to_csv).passed

    def test_fabricated_inline_link_fails(self):
        # Making up an inline URL is also wrong (production posts the link
        # async). No async/follow-up framing → fails.
        bad = _make_trace(
            _turn(tool_calls=[_tool_call("mcp_acme_ops_client_lookup",
                                         {"query": "Seattle Steelheads"})]),
            _turn(content=(
                "Here's your download link for Seattle Steelheads: "
                "https://files.acme.example/exports/ACC-STLH-001.csv"
            )),
        )
        assert not evaluate_outcome(bad, bulk_table_to_csv).passed

    def test_bulk_client_data_exists(self):
        # The scenario needs a genuinely-bulk Storage table to be a real CSV case.
        rows = query_orders(client="ACC-STLH-001", stage="Storage")
        assert len(rows) >= 15, f"bulk client should have many Storage rows, got {len(rows)}"
        assert len({r["sku"] for r in rows}) >= 15, "bulk client should have many distinct SKUs"
        assert any(c["id"] == "ACC-STLH-001" for c in CLIENTS)
