"""Tests for dim_icl_poisoning scenarios — TDD red phase.

Coverage:

  1. Perturbation library extensions — 3 classes in perturbations.py:
       BlankAssistantContent(turn_idx)
       FallbackRenderLeak(turn_idx)
       MalformedToolCall(turn_idx, arg_corruption_mode)

  2. Scenario set — 3 scenarios with correct field values:
       empty_prior_assistant_turn
       primitive_fallback_leak
       bad_prior_call_pattern

  3. All 3 scenarios tagged 'dim:icl_poisoning'

  4. Evaluator unit tests — pass shape vs. fail shape for each scenario:
       empty_prior_assistant_turn:
         pass  = model continues task (non-empty assistant content, tool call or answer)
         fail  = model emits text-ending-in-colon pattern (the empty-prior-turn
                 serialization bug's collapse shape)
       primitive_fallback_leak:
         pass  = last assistant turn contains a real <tool_call> block
         fail  = last assistant turn replicates the 'tool: {...}' literal text pattern
       bad_prior_call_pattern:
         pass  = model retries with correct args
         fail  = model copies malformed arg names in its own tool call

  5. Perturbation apply() contracts:
       - returns NEW trace (original not mutated)
       - injects 'perturbation_applied:' marker into worker_warnings
       - BlankAssistantContent: blanks the target turn's content
       - FallbackRenderLeak: replaces structured turns with 'role: content' primitive format
       - MalformedToolCall: replaces tool_calls args with broken shape

  6. Evaluate_robustness integration: scenario with perturbation declared → passes
     iff marker is present in worker_warnings

  7. Integration marker: pytest.mark.integration for live end-to-end tests
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from windtunnel.api.evaluators import evaluate_outcome, evaluate_robustness

# ─── Import targets (fail until implemented) ──────────────────────────────────
from windtunnel.api.perturbations import (
    BlankAssistantContent,
    FallbackRenderLeak,
    MalformedToolCall,
)
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace, Turn, compute_hash
from windtunnel.scenarios.dim_icl_poisoning.scenarios import (
    DIM_TAG,
    ICL_POISONING_SCENARIOS,
    bad_prior_call_pattern,
    empty_prior_assistant_turn,
    primitive_fallback_leak,
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


# ─── 1. Scenario tags ─────────────────────────────────────────────────────────

class TestScenarioTags:
    def test_dim_tag_constant(self):
        assert DIM_TAG == "dim:icl_poisoning"

    def test_all_three_scenarios_tagged(self):
        for sc in ICL_POISONING_SCENARIOS:
            assert DIM_TAG in sc.tags, f"{sc.name} missing tag {DIM_TAG}"

    def test_scenario_set_has_exactly_three(self):
        assert len(ICL_POISONING_SCENARIOS) == 3

    def test_scenario_names(self):
        names = {sc.name for sc in ICL_POISONING_SCENARIOS}
        assert names == {
            "empty_prior_assistant_turn",
            "primitive_fallback_leak",
            "bad_prior_call_pattern",
        }


# ─── 2. BlankAssistantContent perturbation ────────────────────────────────────

class TestBlankAssistantContent:
    """BlankAssistantContent(turn_idx) — sets turn content to empty string.

    Mimics the empty-prior-turn serialization bug: an assistant turn with
    content="" + tool_calls was re-serialized as an empty prior turn, causing
    the model to emit 'text-ending-in-colon' then stop.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Look up the orders for client ACC-BLWG-001"),
            _turn(
                role="assistant",
                content="I'll look that up for you.",
                tool_calls=[_tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})],
            ),
            _turn(role="tool", content='{"result": "20 items"}'),
            _turn(role="assistant", content="ACC-BLWG-001 has 20 items across their orders."),
        )

    def test_blank_returns_new_trace(self):
        """apply() must return a new Trace, not mutate the original."""
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=1)
        result = p.apply(original)
        assert result is not original

    def test_blank_does_not_mutate_original(self):
        original = self._base_trace()
        original_content = original.turns[1].content
        p = BlankAssistantContent(turn_idx=1)
        p.apply(original)
        assert original.turns[1].content == original_content

    def test_blank_sets_content_empty(self):
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=1)
        result = p.apply(original)
        assert result.turns[1].content == ""

    def test_blank_preserves_tool_calls(self):
        """The tool_calls on the blanked turn must be preserved."""
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=1)
        result = p.apply(original)
        # Tool calls should still be there — we only blank the text content
        assert result.turns[1].tool_calls == original.turns[1].tool_calls

    def test_blank_injects_marker_in_warnings(self):
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=1)
        result = p.apply(original)
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert any("blank_assistant_content" in w for w in result.worker_warnings)
        assert any("turn_idx=1" in w for w in result.worker_warnings)

    def test_blank_marker_matches_property(self):
        p = BlankAssistantContent(turn_idx=1)
        trace = self._base_trace()
        result = p.apply(trace)
        assert any(p.marker in w for w in result.worker_warnings)

    def test_blank_out_of_bounds_is_safe(self):
        """Applying to an out-of-bounds index should not crash — no-op on turns."""
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=99)
        result = p.apply(original)
        # Worker warnings still has marker; turns unchanged
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert len(result.turns) == len(original.turns)

    def test_blank_leaves_other_turns_unchanged(self):
        original = self._base_trace()
        p = BlankAssistantContent(turn_idx=1)
        result = p.apply(original)
        assert result.turns[0].content == original.turns[0].content
        assert result.turns[2].content == original.turns[2].content
        assert result.turns[3].content == original.turns[3].content


# ─── 3. FallbackRenderLeak perturbation ───────────────────────────────────────

class TestFallbackRenderLeak:
    """FallbackRenderLeak(turn_idx) — replaces structured turns with the
    primitive 'role: content' format that the worker's fallback produced.

    The fallback-render bug: when tool_calls were present but the queue
    message didn't carry them, _build_prompt rendered the assistant turn as
    literal text:
      'tool: {"name": "mcp_acme_ops_order_query", "arguments": ...}'
    A model that sees this in its own prior history learns to replicate it.
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Look up the orders for Bluewing Logistics"),
            _turn(
                role="assistant",
                content="",
                tool_calls=[_tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})],
            ),
            _turn(role="tool", content='{"result": "20 items"}'),
            _turn(role="assistant", content="Bluewing Logistics has 20 items on order."),
        )

    def test_fallback_returns_new_trace(self):
        original = self._base_trace()
        p = FallbackRenderLeak(turn_idx=1)
        result = p.apply(original)
        assert result is not original

    def test_fallback_does_not_mutate_original(self):
        original = self._base_trace()
        orig_content = original.turns[1].content
        p = FallbackRenderLeak(turn_idx=1)
        p.apply(original)
        assert original.turns[1].content == orig_content

    def test_fallback_replaces_content_with_primitive_text(self):
        """The target turn's content should contain the 'tool: {...}' pattern."""
        original = self._base_trace()
        p = FallbackRenderLeak(turn_idx=1)
        result = p.apply(original)
        # The leaked format is the primitive role: content string
        # For a turn that had tool_calls, it becomes "tool: {serialized call}"
        content = result.turns[1].content
        assert "tool:" in content or "tool_call" in content.lower() or "{" in content

    def test_fallback_injects_marker(self):
        original = self._base_trace()
        p = FallbackRenderLeak(turn_idx=1)
        result = p.apply(original)
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert any("fallback_render_leak" in w for w in result.worker_warnings)
        assert any("turn_idx=1" in w for w in result.worker_warnings)

    def test_fallback_marker_matches_property(self):
        p = FallbackRenderLeak(turn_idx=1)
        trace = self._base_trace()
        result = p.apply(trace)
        assert any(p.marker in w for w in result.worker_warnings)

    def test_fallback_out_of_bounds_is_safe(self):
        original = self._base_trace()
        p = FallbackRenderLeak(turn_idx=99)
        result = p.apply(original)
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert len(result.turns) == len(original.turns)

    def test_fallback_leaves_other_turns_unchanged(self):
        original = self._base_trace()
        p = FallbackRenderLeak(turn_idx=1)
        result = p.apply(original)
        assert result.turns[0].content == original.turns[0].content
        assert result.turns[3].content == original.turns[3].content


# ─── 4. MalformedToolCall perturbation ───────────────────────────────────────

class TestMalformedToolCall:
    """MalformedToolCall(turn_idx, arg_corruption_mode) — replaces a prior
    tool call's arguments with a broken shape.

    arg_corruption_mode:
      'wrong_field_names' — renames canonical fields to garbage keys
      'broken_json'       — replaces the args string with invalid JSON
    """

    def _base_trace(self) -> Trace:
        return _make_trace(
            _turn(role="user", content="Look up client Bluewing Logistics"),
            _turn(
                role="assistant",
                content="",
                tool_calls=[
                    _tool_call(
                        "mcp_acme_ops_client_lookup",
                        {"query": "Bluewing Logistics"},
                    )
                ],
            ),
            _turn(role="tool", content='{"result": "{}"}'),
            _turn(role="assistant", content="Found Bluewing Logistics."),
        )

    def test_malformed_wrong_field_names_returns_new_trace(self):
        original = self._base_trace()
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names")
        result = p.apply(original)
        assert result is not original

    def test_malformed_does_not_mutate_original(self):
        original = self._base_trace()
        orig_args = original.turns[1].tool_calls[0]["function"]["arguments"]
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names")
        p.apply(original)
        assert original.turns[1].tool_calls[0]["function"]["arguments"] == orig_args

    def test_wrong_field_names_changes_args(self):
        """Wrong-field-names mode must produce different argument keys."""
        original = self._base_trace()
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names")
        result = p.apply(original)
        orig_args_str = original.turns[1].tool_calls[0]["function"]["arguments"]
        new_args_str = result.turns[1].tool_calls[0]["function"]["arguments"]
        assert orig_args_str != new_args_str

    def test_broken_json_mode_produces_invalid_json(self):
        """Broken-JSON mode must produce a string that is not valid JSON."""
        import json
        original = self._base_trace()
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="broken_json")
        result = p.apply(original)
        new_args_str = result.turns[1].tool_calls[0]["function"]["arguments"]
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(new_args_str)

    def test_malformed_injects_marker(self):
        original = self._base_trace()
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names")
        result = p.apply(original)
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert any("malformed_tool_call" in w for w in result.worker_warnings)
        assert any("turn_idx=1" in w for w in result.worker_warnings)

    def test_malformed_marker_matches_property(self):
        p = MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names")
        trace = self._base_trace()
        result = p.apply(trace)
        assert any(p.marker in w for w in result.worker_warnings)

    def test_malformed_out_of_bounds_is_safe(self):
        original = self._base_trace()
        p = MalformedToolCall(turn_idx=99, arg_corruption_mode="wrong_field_names")
        result = p.apply(original)
        assert any("perturbation_applied:" in w for w in result.worker_warnings)
        assert len(result.turns) == len(original.turns)

    def test_malformed_default_mode_is_wrong_field_names(self):
        """Default arg_corruption_mode should be 'wrong_field_names'."""
        p = MalformedToolCall(turn_idx=1)
        assert p.arg_corruption_mode == "wrong_field_names"


# ─── 5. evaluate_robustness integration ──────────────────────────────────────

class TestRobustnessWithIclPerturbations:
    """Confirm that evaluate_robustness honours ICL perturbation markers."""

    def _scenario_with_perturbation(self, p: Any) -> Scenario:
        return Scenario(
            name="test_icl",
            prompt="test",
            target_facts=[["result"]],
            perturbations=[p],
            tags=[DIM_TAG],
        )

    def test_robustness_passes_when_blank_marker_present(self):
        p = BlankAssistantContent(turn_idx=0)
        scenario = self._scenario_with_perturbation(p)
        trace = _make_trace(
            _turn(role="assistant", content="result"),
            warnings=[p.marker],
        )
        result = evaluate_robustness(trace, scenario)
        assert result.passed, result.detail

    def test_robustness_fails_when_blank_marker_absent(self):
        p = BlankAssistantContent(turn_idx=0)
        scenario = self._scenario_with_perturbation(p)
        trace = _make_trace(_turn(role="assistant", content="result"))
        result = evaluate_robustness(trace, scenario)
        assert not result.passed

    def test_robustness_passes_when_fallback_marker_present(self):
        p = FallbackRenderLeak(turn_idx=0)
        scenario = self._scenario_with_perturbation(p)
        trace = _make_trace(
            _turn(role="assistant", content="result"),
            warnings=[p.marker],
        )
        result = evaluate_robustness(trace, scenario)
        assert result.passed, result.detail

    def test_robustness_passes_when_malformed_marker_present(self):
        p = MalformedToolCall(turn_idx=0, arg_corruption_mode="broken_json")
        scenario = self._scenario_with_perturbation(p)
        trace = _make_trace(
            _turn(role="assistant", content="result"),
            warnings=[p.marker],
        )
        result = evaluate_robustness(trace, scenario)
        assert result.passed, result.detail


# ─── 6. Scenario: empty_prior_assistant_turn ──────────────────────────────────

class TestEmptyPriorAssistantTurnScenario:
    """Scenario 1: blank a prior assistant turn; model must continue task normally.

    Pass = model produces a non-empty final answer (or makes a tool call)
           and does NOT emit 'text-ending-in-colon' + stop.
    Fail = model emits a sentence ending in ':' with no further content
           (the empty-prior-turn bug's collapse shape).
    """

    def _passing_trace(self) -> Trace:
        """Model continues normally despite a blank prior assistant turn."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            # This turn was blanked by the perturbation (simulates the bug shape)
            _turn(role="assistant", content=""),
            _turn(role="tool", content='{"result": "20 items"}'),
            # Model produces a real answer on the next turn
            _turn(role="assistant", content="Bluewing Logistics has 20 items across their orders."),
        )

    def _failing_trace_colon_stop(self) -> Trace:
        """Model emits text ending in ':' and stops — the collapse failure shape."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content=""),
            _turn(role="tool", content='{"result": "20 items"}'),
            # Model copies the empty-turn shape: emits colon-terminated text, stops
            _turn(role="assistant", content="Here are the order totals for Bluewing Logistics:"),
        )

    def _failing_trace_also_empty(self) -> Trace:
        """Model also produces empty content — copies the poisoned prior turn."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content=""),
            _turn(role="tool", content='{"result": "20 items"}'),
            _turn(role="assistant", content=""),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), empty_prior_assistant_turn)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_colon_stop_outcome_fails(self):
        """Colon-terminated content without facts → outcome fails."""
        result = evaluate_outcome(self._failing_trace_colon_stop(), empty_prior_assistant_turn)
        assert not result.passed, "Expected outcome fail for colon-stop shape"

    def test_failing_empty_content_outcome_fails(self):
        """Empty last assistant turn → outcome fails."""
        result = evaluate_outcome(self._failing_trace_also_empty(), empty_prior_assistant_turn)
        assert not result.passed, "Expected outcome fail for empty content"

    def test_scenario_has_perturbation(self):
        """Scenario must declare a BlankAssistantContent perturbation."""
        assert any(
            isinstance(p, BlankAssistantContent)
            for p in empty_prior_assistant_turn.perturbations
        ), "empty_prior_assistant_turn must have BlankAssistantContent perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in empty_prior_assistant_turn.tags

    def test_scenario_target_facts_non_empty(self):
        assert len(empty_prior_assistant_turn.target_facts) > 0


# ─── 7. Scenario: primitive_fallback_leak ────────────────────────────────────

class TestPrimitiveFallbackLeakScenario:
    """Scenario 2: prior turn has 'tool: {...}' literal text (the
    fallback-render bug shape).

    Pass = model's final turn uses a real <tool_call> block or makes a
           proper structured tool call (not replicated literal text).
    Fail = model replicates 'tool: {...}' in its own assistant content.
    """

    # The exact string the fallback-render bug leaked into the conversation
    _FALLBACK_TEXT = 'tool: {"name": "mcp_acme_ops_order_query", "arguments": {"client": "ACC-BLWG-001"}}'

    def _passing_trace(self) -> Trace:
        """Prior turn has leaked fallback text; model still uses structured tool call."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            # Poisoned prior: the fallback literal text leaked into assistant content
            _turn(role="assistant", content=self._FALLBACK_TEXT),
            _turn(role="tool", content='{"result": "20 items"}'),
            # Model uses a real tool call (not replicating the text)
            _turn(
                role="assistant",
                content="Bluewing Logistics has 20 items across their orders.",
                tool_calls=[_tool_call("mcp_acme_ops_order_query", {"client": "ACC-BLWG-001"})],
            ),
        )

    def _passing_trace_clean_answer(self) -> Trace:
        """Model produces a clean text answer, not replicating fallback format."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content=self._FALLBACK_TEXT),
            _turn(role="tool", content='{"result": "20 items"}'),
            _turn(role="assistant", content="Bluewing Logistics has 20 items across their orders."),
        )

    def _failing_trace_replicates_fallback(self) -> Trace:
        """Model sees the fallback text and replicates it in its own response."""
        return _make_trace(
            _turn(role="user", content="How many items does Bluewing Logistics have on order?"),
            _turn(role="assistant", content=self._FALLBACK_TEXT),
            _turn(role="tool", content='{"result": "20 items"}'),
            # Model copies the fallback format instead of answering
            _turn(
                role="assistant",
                content='tool: {"name": "mcp_acme_ops_order_query", "arguments": {"client": "Bluewing Logistics"}}',
            ),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace_clean_answer(), primitive_fallback_leak)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_trace_replicates_fallback_outcome_fails(self):
        """Model replicates 'tool: {...}' literal → lacks real answer → outcome fails."""
        result = evaluate_outcome(self._failing_trace_replicates_fallback(), primitive_fallback_leak)
        assert not result.passed, "Expected outcome fail for fallback-replication shape"

    def test_scenario_has_perturbation(self):
        """Scenario must declare a FallbackRenderLeak perturbation."""
        assert any(
            isinstance(p, FallbackRenderLeak)
            for p in primitive_fallback_leak.perturbations
        ), "primitive_fallback_leak must have FallbackRenderLeak perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in primitive_fallback_leak.tags

    def test_scenario_has_policy_against_fallback_replication(self):
        """Scenario should have a constraint policy that fails if model replicates fallback."""
        from windtunnel.api.evaluators import evaluate_constraint
        # A trace where the model replicates the fallback format should fail the constraint
        failing_trace = self._failing_trace_replicates_fallback()
        result = evaluate_constraint(failing_trace, primitive_fallback_leak)
        assert not result.passed, (
            "Constraint layer should detect fallback-replication in the last assistant turn"
        )

    def test_scenario_constraint_passes_clean_answer(self):
        """Clean answer passes the constraint."""
        from windtunnel.api.evaluators import evaluate_constraint
        passing_trace = self._passing_trace_clean_answer()
        result = evaluate_constraint(passing_trace, primitive_fallback_leak)
        assert result.passed, f"Expected constraint pass for clean answer, got: {result.detail}"


# ─── 8. Scenario: bad_prior_call_pattern ─────────────────────────────────────

class TestBadPriorCallPatternScenario:
    """Scenario 3: prior assistant turn has a malformed tool call (wrong arg names).
    Model must recover / retry with correct args instead of copying the bad pattern.

    Pass = model's own tool call uses correct argument names.
    Fail = model copies the wrong argument names from the prior turn.
    """

    # The malformed prior tool call: uses 'name' instead of 'query' for client_lookup
    _BAD_TOOL_CALL = {
        "id": "call_bad",
        "type": "function",
        "function": {
            "name": "mcp_acme_ops_client_lookup",
            "arguments": '{"name": "Bluewing Logistics"}',  # WRONG: should be 'query'
        },
    }

    def _passing_trace(self) -> Trace:
        """Prior turn had wrong args; model produces a correct tool call."""
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics and get their orders."),
            # Prior turn: bad args (wrong field name)
            _turn(role="assistant", content="", tool_calls=[self._BAD_TOOL_CALL]),
            _turn(role="tool", content='{"result": "{\\"matches\\": []}"}'),  # Got empty
            # Model recovers: retries with correct args
            _turn(
                role="assistant",
                content="",
                tool_calls=[
                    _tool_call("mcp_acme_ops_client_lookup", {"query": "Bluewing Logistics"})
                ],
            ),
            _turn(role="tool", content='{"result": "{\\"matches\\": [{\\"id\\": \\"ACC-BLWG-001\\", \\"clientEmail\\": \\"ops@bluewing.example\\"}]}"}'),
            _turn(role="assistant", content="Bluewing Logistics (ACC-BLWG-001) contact: ops@bluewing.example"),
        )

    def _failing_trace_copies_bad_args(self) -> Trace:
        """Model copies the wrong arg names from the prior poisoned turn."""
        bad_copied = {
            "id": "call_copied",
            "type": "function",
            "function": {
                "name": "mcp_acme_ops_client_lookup",
                "arguments": '{"name": "Bluewing Logistics"}',  # COPIED the wrong key
            },
        }
        return _make_trace(
            _turn(role="user", content="Look up Bluewing Logistics and get their orders."),
            _turn(role="assistant", content="", tool_calls=[self._BAD_TOOL_CALL]),
            _turn(role="tool", content='{"result": "{\\"matches\\": []}"}'),
            # Model copies the bad arg key
            _turn(role="assistant", content="", tool_calls=[bad_copied]),
            _turn(role="tool", content='{"result": "{\\"matches\\": []}"}'),
            _turn(role="assistant", content="No results found for Bluewing Logistics."),
        )

    def test_passing_trace_outcome_passes(self):
        result = evaluate_outcome(self._passing_trace(), bad_prior_call_pattern)
        assert result.passed, f"Expected pass but got: {result.detail}"

    def test_failing_copies_bad_args_outcome_fails(self):
        """Model copies bad args → gets empty result → answer missing target facts."""
        result = evaluate_outcome(self._failing_trace_copies_bad_args(), bad_prior_call_pattern)
        assert not result.passed, "Expected outcome fail when model copies bad arg pattern"

    def test_scenario_has_perturbation(self):
        """Scenario must declare a MalformedToolCall perturbation."""
        assert any(
            isinstance(p, MalformedToolCall)
            for p in bad_prior_call_pattern.perturbations
        ), "bad_prior_call_pattern must have MalformedToolCall perturbation"

    def test_scenario_tagged(self):
        assert DIM_TAG in bad_prior_call_pattern.tags

    def test_scenario_target_facts_non_empty(self):
        assert len(bad_prior_call_pattern.target_facts) > 0


# ─── 9. Production-failure-shape regression guard ────────────────────────────

class TestFailureShapeRegressionGuard:
    """Verify that the real production failure shapes are recognized as
    failure patterns by the evaluators.

    These mirror two real production serialization bugs:
      - the empty-prior-turn bug: the model's final assistant turn was an
        empty string (tool_calls present, content dropped)
      - the fallback-render bug: the model's final turn contained
        'tool: {...}' literal text (a hallucinated tool call as text)

    We rebuild those failure shapes inline and verify the scenario evaluators
    score them FAIL. This is the regression guard: if either fix is reverted,
    this test catches it.
    """

    def test_empty_turn_is_a_failure_shape(self):
        """The empty-prior-turn bug's last assistant turn is the colon-stop/empty
        failure shape.

        In the captured production session, the last assistant message is an
        empty string (the model produced tool_calls but empty content), which
        our evaluators correctly treat as FAIL because target facts are absent.
        """
        # Build a trace that represents the failure shape:
        # the model's final turn has empty content (it stopped after tool call)
        failure_trace = _make_trace(
            _turn(role="user", content="Please provide a complete list of all SKUs"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-1322"})
            ]),
            _turn(role="tool", content='{"result": "{}"}'),
            _turn(role="assistant", content=""),  # model stopped here, empty content
        )
        result = evaluate_outcome(failure_trace, empty_prior_assistant_turn)
        assert not result.passed, (
            "Empty-prior-turn failure shape (empty last assistant turn) must score FAIL"
        )

    def test_hallucinated_toolcall_is_a_failure_shape(self):
        """The fallback-render bug's last assistant turn contains 'tool: {...}'
        literal text.

        This is the hallucinated tool call: model replicates the fallback format
        in its own content instead of making a real structured tool call.
        """
        # The shape of the captured production failure (last assistant message)
        leaked_content = (
            'tool: {"result": "{\\"filter\\":{\\"sku\\":null,\\"client\\":\\"ACC-132 queried\\"'
        )
        failure_trace = _make_trace(
            _turn(role="user", content="Please provide a complete list"),
            _turn(role="assistant", content="", tool_calls=[
                _tool_call("mcp_acme_ops_order_query", {"client": "ACC-1322"})
            ]),
            _turn(role="tool", content='{"result": "{}"}'),
            # Model replicated the 'tool: {...}' text in its content
            _turn(role="assistant", content=leaked_content),
        )
        result = evaluate_outcome(failure_trace, primitive_fallback_leak)
        assert not result.passed, (
            "Fallback-render failure shape ('tool: {...}' in content) must score FAIL on primitive_fallback_leak"
        )
