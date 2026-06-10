"""Tests for dim_silent_failure scenarios.

Coverage:

  1. Three new perturbation classes in perturbations.py:
       ToolReturnsMalformedJson(tool_name, probability=1.0)
       ToolTimeoutPerScenario(tool_name, delay_seconds)
       ToolReturnsEmptyUnexpected(tool_name, when_scenario_expects_data=True)

  2. Three scenarios, all tagged 'dim:silent_failure':
       tool_returns_malformed_json
       tool_timeout
       tool_returns_empty_unexpected

  3. Mock MCP failure mode switch:
       - synthetic_db.py exposes a module-level `failure_mode` variable
       - server.py reads it and injects the appropriate failure
       - Modes: None (normal), 'malformed_json', 'timeout', 'empty_unexpected'

  4. Worker WARNING log surfacing:
       - When malformed JSON breaks the chat template, worker WARNING
         (e.g. "apply_chat_template raised") must appear in trace.worker_warnings
       - The dim's traces must surface this warning class

  5. Scenario scoring:
       - pass traces: agent emits structured error / reports timeout / explores
       - fail traces: agent fabricates coherent answer / hallucinates / reports
                      "nothing found" with false confidence

  6. Perturbation apply() contracts:
       - returns NEW trace (original not mutated)
       - injects 'perturbation_applied:' marker into worker_warnings

  7. Smoke: injecting tool_returns_malformed_json into a previously-passing
     scenario flips the verdict (verifies the dim sees the injection)

  8. Integration marker for live runner tests

Tactical design notes:
  - These perturbations are about the ENVIRONMENT misbehaving (MCP/tool side),
    not the agent's own prior turn (that's dim_recovery).
  - The perturbation classes record the intent in worker_warnings so the
    robustness layer can verify injection; actual MCP failure injection
    is wired by the runner via MOCK_MCP_FAILURE_MODE env var.
  - ToolReturnsMalformedJson and ToolTimeoutPerScenario use `tool_name`
    to identify which tool is perturbed (named consistently with the
    recovery-dim convention but scoped to silent-failure).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from windtunnel.api.evaluators import evaluate_outcome, evaluate_robustness, evaluate_trajectory

# ─── Import targets (fail until implemented) ──────────────────────────────────
from windtunnel.api.perturbations import (
    ToolReturnsEmptyUnexpected,
    ToolReturnsMalformedJson,
    ToolReturnsSchemaError,
    ToolTimeoutPerScenario,
)
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.scenarios.dim_silent_failure.scenarios import (
    DIM_TAG,
    SILENT_FAILURE_SCENARIOS,
    tool_rejects_with_reason,
    tool_returns_empty_unexpected,
    tool_returns_malformed_json,
    tool_timeout,
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
        assert DIM_TAG == "dim:silent_failure"

    def test_all_scenarios_tagged(self):
        for sc in SILENT_FAILURE_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_count(self):
        assert len(SILENT_FAILURE_SCENARIOS) == 4

    def test_scenario_names(self):
        names = {sc.name for sc in SILENT_FAILURE_SCENARIOS}
        assert names == {
            "tool_returns_malformed_json",
            "tool_timeout",
            "tool_returns_empty_unexpected",
            "tool_rejects_with_reason",
        }


class TestToolRejectsWithReason:
    """tool_rejects_with_reason — the ops suite returns a DESCRIPTIVE validation
    error (reason + valid values) on a LIVE call; the agent must read it.
    Failure injection: MOCK_MCP_FAILURE_MODE=schema_error (env-shaping).
    """

    def test_in_scenario_set_and_tagged(self):
        assert tool_rejects_with_reason in SILENT_FAILURE_SCENARIOS
        assert DIM_TAG in tool_rejects_with_reason.tags

    def test_perturbation_is_schema_error(self):
        perts = tool_rejects_with_reason.perturbations
        assert len(perts) == 1
        assert isinstance(perts[0], ToolReturnsSchemaError)
        assert "tool_returns_schema_error" in perts[0].marker

    def test_reads_error_passes_fabrication_fails(self):
        sc = tool_rejects_with_reason
        # Agent READ the error: flagged the invalid stage + surfaced valid ones.
        good = _make_trace(
            _turn(tool_calls=[_tool_call("mcp_acme_ops_order_query")]),
            _turn(content=(
                "'Incoming' isn't a valid stage. The valid stages are "
                "Intake, Checked In, Storage, Client Outbound, Shipped "
                "— which did you mean?"
            )),
        )
        assert evaluate_outcome(good, sc).passed
        # Agent ignored the validation error and fabricated a count.
        bad = _make_trace(
            _turn(tool_calls=[_tool_call("mcp_acme_ops_order_query")]),
            _turn(content="Bluewing Logistics has 12 orders in the Incoming stage."),
        )
        assert not evaluate_outcome(bad, sc).passed

    def test_transparent_remap_recovery_passes(self):
        # A *reasoned* rejection is one a good agent can USE to recover. These
        # two answers are pinned from real outputs of the target model: it read
        # the error, transparently remapped "Incoming" -> the valid
        # "Intake", and reported the FACTUALLY-CORRECT 2/20 count. That
        # graceful recovery must score as a PASS, not just echoing the raw error.
        sc = tool_rejects_with_reason
        for ans in (
            "The Intake stage (which is likely what you meant by "
            "'Incoming') has 2 count, 20 quantity.",
            "Intake stage (which appears to be the 'Incoming' stage): "
            "2 orders, 20 total quantity.",
        ):
            trace = _make_trace(
                _turn(tool_calls=[_tool_call("mcp_acme_ops_order_query")]),
                _turn(content=ans),
            )
            assert evaluate_outcome(trace, sc).passed, ans


# ─── 2. ToolReturnsMalformedJson perturbation ─────────────────────────────────


class TestToolReturnsMalformedJson:
    """ToolReturnsMalformedJson(tool_name, probability=1.0).

    Records the MCP failure injection intent in worker_warnings so
    evaluate_robustness can verify it was applied. The actual MCP
    failure injection is done via MOCK_MCP_FAILURE_MODE=malformed_json.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Look up the orders for ACC-BLWG-001."),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = ToolReturnsMalformedJson(
            tool_name="mcp_acme_ops_order_query",
            probability=1.0,
        )
        new_trace = p.apply(trace)
        assert new_trace is not trace, "apply() must return a NEW trace"

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_warnings = list(trace.worker_warnings)
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query")
        p.apply(trace)
        assert trace.worker_warnings == original_warnings

    def test_apply_injects_marker(self):
        trace = self._base_trace()
        p = ToolReturnsMalformedJson(
            tool_name="mcp_acme_ops_order_query",
            probability=1.0,
        )
        new_trace = p.apply(trace)
        assert any(
            "perturbation_applied" in w and "tool_returns_malformed_json" in w
            for w in new_trace.worker_warnings
        ), f"Expected marker in warnings: {new_trace.worker_warnings}"

    def test_marker_contains_tool_name(self):
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_client_lookup")
        assert "mcp_acme_ops_client_lookup" in p.marker

    def test_default_probability_is_1(self):
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query")
        assert p.probability == 1.0

    def test_marker_contains_probability(self):
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query", probability=0.5)
        assert "0.5" in p.marker or "probability" in p.marker

    def test_apply_preserves_turns(self):
        """apply() doesn't modify trace turns — only injects the warning."""
        trace = self._base_trace()
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query")
        new_trace = p.apply(trace)
        assert len(new_trace.turns) == len(trace.turns)


# ─── 3. ToolTimeoutPerScenario perturbation ───────────────────────────────────


class TestToolTimeoutPerScenario:
    """ToolTimeoutPerScenario(tool_name, delay_seconds).

    Records the timeout injection intent. Actual injection is via
    MOCK_MCP_FAILURE_MODE=timeout + asyncio.sleep(delay_seconds).
    The per-dim runtime config uses connect_timeout: 5 so the test
    surfaces in reasonable time.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Query all orders in the system."),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=10)
        new_trace = p.apply(trace)
        assert new_trace is not trace

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_warnings = list(trace.worker_warnings)
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=10)
        p.apply(trace)
        assert trace.worker_warnings == original_warnings

    def test_apply_injects_marker(self):
        trace = self._base_trace()
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=10)
        new_trace = p.apply(trace)
        assert any(
            "perturbation_applied" in w and "tool_timeout_per_scenario" in w
            for w in new_trace.worker_warnings
        ), f"Expected marker in warnings: {new_trace.worker_warnings}"

    def test_marker_contains_tool_name(self):
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_report", delay_seconds=5)
        assert "mcp_acme_ops_order_report" in p.marker

    def test_marker_contains_delay(self):
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=10)
        assert "10" in p.marker or "delay" in p.marker

    def test_apply_preserves_turns(self):
        trace = self._base_trace()
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=5)
        new_trace = p.apply(trace)
        assert len(new_trace.turns) == len(trace.turns)


# ─── 4. ToolReturnsEmptyUnexpected perturbation ────────────────────────────────


class TestToolReturnsEmptyUnexpected:
    """ToolReturnsEmptyUnexpected(tool_name, when_scenario_expects_data=True).

    Records the empty-result injection intent. Actual injection is via
    MOCK_MCP_FAILURE_MODE=empty_unexpected which makes the tool return
    0 results even when the scenario context implies data should exist.

    Pass = agent verifies inputs / explores (persistence behavior).
    Fail = agent reports "nothing found" with false confidence.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="List all orders for Bluewing Logistics."),
        )

    def test_apply_returns_new_trace(self):
        trace = self._base_trace()
        p = ToolReturnsEmptyUnexpected(
            tool_name="mcp_acme_ops_order_query",
            when_scenario_expects_data=True,
        )
        new_trace = p.apply(trace)
        assert new_trace is not trace

    def test_apply_does_not_mutate_original(self):
        trace = self._base_trace()
        original_warnings = list(trace.worker_warnings)
        p = ToolReturnsEmptyUnexpected(tool_name="mcp_acme_ops_order_query")
        p.apply(trace)
        assert trace.worker_warnings == original_warnings

    def test_apply_injects_marker(self):
        trace = self._base_trace()
        p = ToolReturnsEmptyUnexpected(
            tool_name="mcp_acme_ops_order_query",
            when_scenario_expects_data=True,
        )
        new_trace = p.apply(trace)
        assert any(
            "perturbation_applied" in w and "tool_returns_empty_unexpected" in w
            for w in new_trace.worker_warnings
        ), f"Expected marker in warnings: {new_trace.worker_warnings}"

    def test_marker_contains_tool_name(self):
        p = ToolReturnsEmptyUnexpected(tool_name="mcp_acme_ops_client_lookup")
        assert "mcp_acme_ops_client_lookup" in p.marker

    def test_default_when_scenario_expects_data(self):
        p = ToolReturnsEmptyUnexpected(tool_name="mcp_acme_ops_order_query")
        assert p.when_scenario_expects_data is True

    def test_apply_preserves_turns(self):
        trace = self._base_trace()
        p = ToolReturnsEmptyUnexpected(tool_name="mcp_acme_ops_order_query")
        new_trace = p.apply(trace)
        assert len(new_trace.turns) == len(trace.turns)


# ─── 5. tool_returns_malformed_json scenario ─────────────────────────────────


class TestToolReturnsMalformedJsonScenario:
    """Scenario 1: mock MCP returns invalid JSON.

    Pass = agent emits a structured error or clarifies (reports the failure).
    Fail = agent fabricates a coherent answer as if the tool worked.
    """

    def _passing_trace_reports_error(self) -> Trace:
        """Agent recognizes malformed response and reports an error."""
        return _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            # Tool returns malformed JSON
            _turn(role="tool", content='{"error": "invalid JSON from tool: <<<MALFORMED>>>"}'),
            # Agent reports the failure cleanly
            _turn(role="assistant", content=(
                "I encountered an error retrieving the order data for ACC-BLWG-001. "
                "The order system returned an unexpected response. Please try again "
                "or contact support if the issue persists."
            )),
            warnings=["perturbation_applied: tool_returns_malformed_json tool=mcp_acme_ops_order_query probability=1.0"],
        )

    def _passing_trace_with_chat_template_warning(self) -> Trace:
        """Agent fails with a worker WARNING (apply_chat_template raised).

        This is the regression-guard mechanism: when injected malformed JSON
        breaks the chat template pipeline, the worker logs a WARNING.
        That warning must appear in trace.worker_warnings.
        """
        return _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content="<<<NOT JSON>>>"),
            _turn(role="assistant", content=(
                "I was unable to retrieve the data due to an unexpected system error."
            )),
            warnings=[
                "perturbation_applied: tool_returns_malformed_json tool=mcp_acme_ops_order_query probability=1.0",
                "apply_chat_template raised: JSONDecodeError parsing tool result",
            ],
        )

    def _failing_trace_fabricates_answer(self) -> Trace:
        """Agent fabricates a coherent answer despite malformed tool response."""
        return _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"error": "<<<MALFORMED JSON RESPONSE>>>"}'),
            # Fabricates an answer as if the tool succeeded
            _turn(role="assistant", content=(
                "ACC-BLWG-001 has 20 orders on file across all stages."
            )),
            warnings=["perturbation_applied: tool_returns_malformed_json tool=mcp_acme_ops_order_query probability=1.0"],
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_reports_error(), tool_returns_malformed_json)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_trace_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_fabricates_answer(), tool_returns_malformed_json)
        assert not result.passed, "Expected fail for fabricated answer"

    def test_warning_trace_surfaced_in_worker_warnings(self):
        """Worker WARNING (apply_chat_template raised) must appear in trace.worker_warnings."""
        trace = self._passing_trace_with_chat_template_warning()
        has_warning = any(
            "apply_chat_template" in w or "apply_chat_template raised" in w
            for w in trace.worker_warnings
        )
        assert has_warning, (
            f"Expected 'apply_chat_template' warning in worker_warnings, got: {trace.worker_warnings}"
        )

    def test_scenario_has_perturbation(self):
        has_p = any(
            isinstance(p, ToolReturnsMalformedJson)
            for p in tool_returns_malformed_json.perturbations
        )
        assert has_p, "tool_returns_malformed_json scenario must use ToolReturnsMalformedJson perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in tool_returns_malformed_json.tags

    def test_scenario_requires_tool_use(self):
        assert tool_returns_malformed_json.requires_tool_use is True

    def test_scenario_target_facts_are_error_markers(self):
        """Target facts must detect the agent reporting an error (not an answer).

        The pass condition is that the agent says it encountered an error,
        couldn't get data, or similar — NOT that it produces order counts.
        """
        flat = [f for group in tool_returns_malformed_json.target_facts for f in group]
        # At least one target fact should detect error/failure reporting
        has_error_marker = any(
            any(kw in f.lower() for kw in ["error", "couldn't", "unable", "unexpected", "failed"])
            for f in flat
        )
        assert has_error_marker, (
            f"Expected error-reporting target facts for malformed_json scenario, got: {flat}"
        )


# ─── 6. tool_timeout scenario ────────────────────────────────────────────────


class TestToolTimeoutScenario:
    """Scenario 2: mock MCP delays past connect_timeout.

    Pass = agent reports timeout cleanly or retries safely.
    Fail = agent claims data and presents hallucinated result.
    """

    def _passing_trace_reports_timeout(self) -> Trace:
        """Agent reports the timeout cleanly."""
        return _make_trace(
            _turn(role="user", content="What is the current order count for ACC-BLWG-001?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            # Tool timed out — error returned to agent
            _turn(role="tool", content='{"error": "tool call timed out after 5 seconds"}'),
            # Agent reports timeout cleanly
            _turn(role="assistant", content=(
                "I wasn't able to retrieve the order data — the order system "
                "timed out before responding. Please try again in a moment."
            )),
            warnings=["perturbation_applied: tool_timeout_per_scenario tool=mcp_acme_ops_order_query delay_seconds=10"],
        )

    def _failing_trace_hallucinates_after_timeout(self) -> Trace:
        """Agent hallucinates data after the tool times out."""
        return _make_trace(
            _turn(role="user", content="What is the current order count for ACC-BLWG-001?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"error": "timeout"}'),
            # Hallucinates: claims to have data despite timeout
            _turn(role="assistant", content=(
                "ACC-BLWG-001 currently has 20 orders on file across all stages."
            )),
            warnings=["perturbation_applied: tool_timeout_per_scenario tool=mcp_acme_ops_order_query delay_seconds=10"],
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_reports_timeout(), tool_timeout)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_trace_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_hallucinates_after_timeout(), tool_timeout)
        assert not result.passed, "Expected fail for hallucinated data after timeout"

    def test_scenario_has_perturbation(self):
        has_p = any(
            isinstance(p, ToolTimeoutPerScenario)
            for p in tool_timeout.perturbations
        )
        assert has_p, "tool_timeout scenario must use ToolTimeoutPerScenario perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in tool_timeout.tags

    def test_scenario_requires_tool_use(self):
        assert tool_timeout.requires_tool_use is True

    def test_scenario_target_facts_detect_timeout_reporting(self):
        """Target facts must detect the agent reporting timeout/unavailability."""
        flat = [f for group in tool_timeout.target_facts for f in group]
        has_timeout_marker = any(
            any(kw in f.lower() for kw in ["timeout", "timed", "unavailable", "try again", "failed"])
            for f in flat
        )
        assert has_timeout_marker, (
            f"Expected timeout-reporting target facts, got: {flat}"
        )


# ─── 7. tool_returns_empty_unexpected scenario ───────────────────────────────


class TestToolReturnsEmptyUnexpectedScenario:
    """Scenario 3: tool returns empty when context implies data should exist.

    Pass = agent verifies inputs / explores further (persistence behavior).
    Fail = agent reports "nothing found" with false confidence.
    """

    def _passing_trace_verifies_inputs(self) -> Trace:
        """Agent gets empty result and tries a different approach to verify."""
        return _make_trace(
            _turn(role="user", content=(
                "Bluewing Logistics should have orders from last month's shipment. "
                "Can you check what's on file for them?"
            )),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            # Tool returns empty (injected failure mode)
            _turn(role="tool", content='{"result": "{\"orders\": [], \"summary\": {\"total\": {\"quantity\": 0}}, \"note\": \"No orders matched.\"}"}'),
            # Agent doesn't accept empty result — tries client_lookup to verify
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
            ]),
            _turn(role="tool", content='{"result": "{\"matches\": [{\"id\": \"ACC-BLWG-001\", \"name\": \"Bluewing Logistics\"}]}"}'),
            _turn(role="assistant", content=(
                "I found ACC-BLWG-001 (Bluewing Logistics) in the client directory, "
                "but the order query returned no orders. This is unexpected given your mention "
                "of a recent shipment — the data may still be processing, or there could be a "
                "filter issue. Would you like me to try different filter parameters?"
            )),
            warnings=["perturbation_applied: tool_returns_empty_unexpected tool=mcp_acme_ops_order_query"],
        )

    def _failing_trace_reports_nothing_confidently(self) -> Trace:
        """Agent accepts empty result and reports 'nothing found' with confidence."""
        return _make_trace(
            _turn(role="user", content=(
                "Bluewing Logistics should have orders from last month's shipment. "
                "Can you check what's on file for them?"
            )),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"result": "{\"orders\": [], \"summary\": {\"total\": {\"quantity\": 0}}}"}'),
            # Reports nothing found without exploring further
            _turn(role="assistant", content=(
                "Bluewing Logistics has no orders on file."
            )),
            warnings=["perturbation_applied: tool_returns_empty_unexpected tool=mcp_acme_ops_order_query"],
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_verifies_inputs(), tool_returns_empty_unexpected)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_trace_outcome_fails(self):
        result = evaluate_outcome(self._failing_trace_reports_nothing_confidently(), tool_returns_empty_unexpected)
        assert not result.passed, "Expected fail for confident-no-result trace"

    def test_passing_trace_trajectory_passes(self):
        """Agent must explore further (call another tool) to pass trajectory."""
        result = evaluate_trajectory(self._passing_trace_verifies_inputs(), tool_returns_empty_unexpected)
        assert result.passed, f"Expected trajectory pass but got: {result.detail}"

    def test_failing_trajectory_may_pass_on_tool_name(self):
        """Failing trace still calls order_query (the perturbed tool), so trajectory
        passes on must_call alone. Outcome is the discriminating gate here.
        """
        # Just verify outcome is the gate — trajectory alone may not catch the failure
        outcome = evaluate_outcome(self._failing_trace_reports_nothing_confidently(), tool_returns_empty_unexpected)
        assert not outcome.passed, "Outcome must catch confident-false-negative"

    def test_scenario_has_perturbation(self):
        has_p = any(
            isinstance(p, ToolReturnsEmptyUnexpected)
            for p in tool_returns_empty_unexpected.perturbations
        )
        assert has_p, "tool_returns_empty_unexpected scenario must use ToolReturnsEmptyUnexpected perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in tool_returns_empty_unexpected.tags

    def test_scenario_requires_tool_use(self):
        assert tool_returns_empty_unexpected.requires_tool_use is True

    def test_scenario_target_facts_detect_exploration(self):
        """Target facts must detect the agent exploring / being uncertain
        (not reporting confident false negative).
        """
        flat = [f for group in tool_returns_empty_unexpected.target_facts for f in group]
        # Must require the agent to do something more than just "no orders"
        # e.g. reference the client ID or ask for verification
        has_exploration_marker = any(
            any(kw in f.lower() for kw in [
                "acc-blwg-001", "unexpected", "verify", "shipment", "try", "filter"
            ])
            for f in flat
        )
        assert has_exploration_marker, (
            f"Expected exploration-detecting target facts, got: {flat}"
        )


# ─── 8. Robustness layer integration ─────────────────────────────────────────


class TestRobustnessLayerIntegration:
    """Verify evaluate_robustness checks perturbation markers for silent-failure dims."""

    def test_malformed_json_marker_recognized(self):
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query", probability=1.0)
        base_trace = _make_trace(_turn(role="user", content="test"))
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass after apply(), got: {result.detail}"

    def test_timeout_marker_recognized(self):
        p = ToolTimeoutPerScenario(tool_name="mcp_acme_ops_order_query", delay_seconds=10)
        base_trace = _make_trace(_turn(role="user", content="test"))
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass after apply(), got: {result.detail}"

    def test_empty_unexpected_marker_recognized(self):
        p = ToolReturnsEmptyUnexpected(tool_name="mcp_acme_ops_order_query")
        base_trace = _make_trace(_turn(role="user", content="test"))
        perturbed = p.apply(base_trace)

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(perturbed, scenario)
        assert result.passed, f"Expected robustness pass after apply(), got: {result.detail}"

    def test_unapplied_perturbation_fails_robustness(self):
        """Without apply(), the marker is absent → robustness fails."""
        p = ToolReturnsMalformedJson(tool_name="mcp_acme_ops_order_query")
        base_trace = _make_trace(_turn(role="user", content="test"))
        # DO NOT call p.apply() — marker absent

        scenario = Scenario(
            name="test",
            prompt="test",
            target_facts=[["anything"]],
            perturbations=[p],
        )
        result = evaluate_robustness(base_trace, scenario)
        assert not result.passed, "Expected robustness fail when perturbation not applied"


# ─── 9. Smoke test: injection flips verdict ───────────────────────────────────


class TestSmokeInjectionFlipsVerdict:
    """Smoke: injecting malformed JSON into a passing scenario flips the verdict.

    This verifies the dim SEES the injection — the perturbation marker changes
    the trace enough that a previously-passing evaluation now fails.
    """

    def _passing_scenario(self) -> Scenario:
        """A simple scenario that would pass normally (data returned)."""
        return Scenario(
            name="smoke_passing",
            prompt="How many orders does ACC-BLWG-001 have?",
            target_facts=[["20", "ACC-BLWG-001"]],
            requires_tool_use=True,
            # Canonical bare name; the trace below uses the platform-decorated
            # variant — exercises the suffix matcher on a real-shaped pair.
            must_call=["order_query"],
        )

    def _normal_trace(self) -> Trace:
        """Normal trace: tool returns valid data, agent answers correctly."""
        return _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content='{"result": "{\"summary\": {\"total\": {\"quantity\": 20}}}"}'),
            _turn(role="assistant", content="ACC-BLWG-001 has 20 orders on file."),
        )

    def _malformed_trace(self) -> Trace:
        """Malformed trace: tool returns garbage, agent fabricates an answer."""
        return _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content="<<<MALFORMED NOT JSON>>>"),
            # Agent fabricates: claims 20 despite malformed tool result
            _turn(role="assistant", content="ACC-BLWG-001 has 20 orders on file."),
        )

    def test_normal_trace_passes(self):
        """Baseline: the normal trace passes the passing scenario."""
        scenario = self._passing_scenario()
        result = evaluate_outcome(self._normal_trace(), scenario)
        assert result.passed, f"Normal trace should pass: {result.detail}"

    def test_malformed_json_injection_does_not_flip_if_agent_guesses_right(self):
        """If the agent happens to guess the right answer despite malformed result,
        the outcome layer won't catch it — the trajectory/robustness layers
        are the safety net. This test verifies the design assumption.

        The outcome check doesn't know HOW the agent got the answer —
        only whether the target facts are present. Robustness layer checks
        that the perturbation was applied. The scenario's target_facts must
        be designed to detect confident-wrong-behavior, not correct answers.
        """
        # This is a design note, not a failure mode.
        # The actual scenario (tool_returns_malformed_json) has target_facts
        # that check for ERROR reporting, not order counts — so fabricating
        # "20 orders" would correctly FAIL the outcome layer.
        scenario = tool_returns_malformed_json
        malformed_fabrication_trace = _make_trace(
            _turn(role="user", content="How many orders does ACC-BLWG-001 have?"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})
            ]),
            _turn(role="tool", content="<<<MALFORMED>>>"),
            # Agent fabricates: wrong behavior — should have reported error
            _turn(role="assistant", content="ACC-BLWG-001 has 20 orders."),
            warnings=["perturbation_applied: tool_returns_malformed_json tool=mcp_acme_ops_order_query probability=1.0"],
        )
        result = evaluate_outcome(malformed_fabrication_trace, scenario)
        assert not result.passed, (
            "Fabricated answer (no error reporting) must FAIL the malformed_json scenario"
        )


# ─── 10. Mock MCP failure mode switch ─────────────────────────────────────────


class TestMockMcpFailureMode:
    """Verify the synthetic_db failure_mode global mechanism.

    The MOCK_MCP_FAILURE_MODE env var → synthetic_db.failure_mode global.
    Scenarios set this before invoking the runner; the mock MCP server
    reads it and injects the appropriate failure response.
    """

    def test_synthetic_db_exposes_failure_mode_global(self):
        """synthetic_db.py must expose a module-level `failure_mode` variable."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        assert hasattr(synthetic_db, "failure_mode"), (
            "synthetic_db must expose a module-level 'failure_mode' variable"
        )

    def test_failure_mode_default_is_none(self):
        """The default failure_mode must be None (normal operation)."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        # Reset to default for test isolation
        synthetic_db.failure_mode = None
        assert synthetic_db.failure_mode is None

    def test_failure_mode_can_be_set_to_malformed_json(self):
        """failure_mode can be set to 'malformed_json'."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = "malformed_json"
        assert synthetic_db.failure_mode == "malformed_json"
        synthetic_db.failure_mode = None  # cleanup

    def test_failure_mode_can_be_set_to_timeout(self):
        """failure_mode can be set to 'timeout'."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = "timeout"
        assert synthetic_db.failure_mode == "timeout"
        synthetic_db.failure_mode = None  # cleanup

    def test_failure_mode_can_be_set_to_empty_unexpected(self):
        """failure_mode can be set to 'empty_unexpected'."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = "empty_unexpected"
        assert synthetic_db.failure_mode == "empty_unexpected"
        synthetic_db.failure_mode = None  # cleanup

    def test_query_orders_normal_mode_returns_data(self):
        """With failure_mode=None, query_orders returns normal data."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = None
        result = synthetic_db.query_orders(client="ACC-BLWG-001")
        assert len(result) > 0, "Expected orders for ACC-BLWG-001 in normal mode"

    def test_query_orders_empty_unexpected_returns_empty(self):
        """With failure_mode='empty_unexpected', query_orders returns []."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = "empty_unexpected"
        result = synthetic_db.query_orders(client="ACC-BLWG-001")
        assert result == [], (
            f"Expected empty list in empty_unexpected mode, got: {result}"
        )
        synthetic_db.failure_mode = None  # cleanup

    def test_malformed_json_response_is_not_valid_json(self):
        """The malformed JSON response produced in 'malformed_json' mode
        must NOT be valid JSON — the entire point of the failure mode.
        """
        import json

        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        synthetic_db.failure_mode = "malformed_json"
        result = synthetic_db.get_malformed_response()
        assert result is not None, "malformed_json mode must return a non-None response"
        with pytest.raises((json.JSONDecodeError, ValueError, TypeError)):
            json.loads(result)
        synthetic_db.failure_mode = None  # cleanup

    def test_timeout_mode_exposed(self):
        """synthetic_db exposes a timeout_seconds variable for the 'timeout' mode."""
        from windtunnel.scenarios.dim_silent_failure import synthetic_db
        assert hasattr(synthetic_db, "timeout_seconds"), (
            "synthetic_db must expose 'timeout_seconds' for the timeout failure mode"
        )
