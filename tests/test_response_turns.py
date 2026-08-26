"""Per-step response turns — the runner's optional enrichment channel.

A runtime that reconstructs its agent loop step by step rides Turn-shaped
dicts on ``response["turns"]``; the runner adopts them into Trace.turns
instead of synthesizing the single aggregated assistant turn.

Covers:
  1. Adoption happy path through the REAL pipeline (in_memory scripted
     turns): thoughts, per-step tool_calls, shape-faithful tool_results,
     per-step error threading
  2. Aggregated behavior byte-identical when ``turns`` is absent
  3. Fail-closed validation: every rejection falls back to the aggregated
     turn with a loud response_turns_rejected trace warning, never a crash
  4. The last-assistant-carries-the-final-text invariant (answer-turn
     selection depends on it)
  5. Latency and response-level-error rules
  6. Evidence over multi-step traces: interleaved claimed-call enumeration,
     span-precise final output, scored-turn selection, integrity INVALID on
     a final-step error
"""
from __future__ import annotations

from windtunnel.api._runner.messages import adopt_response_turns
from windtunnel.api.runner import run_scenario
from windtunnel.api.scenario import Scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime

_FINAL = "the order total is 12 units"


def _step(content: str, calls: list | None = None, **extra: object) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": calls or [],
        **extra,
    }


def _scripted_entry() -> dict:
    """One send() response: final reply + three per-step turns."""
    return {
        "content": _FINAL,
        "tool_calls": [
            {"id": "call_1", "name": "client_lookup", "args": {}},
            {"id": "call_2", "name": "order_query", "args": {}},
        ],
        "turns": [
            _step(
                "first, look the client up",
                calls=[{"id": "call_1", "name": "client_lookup", "args": {}}],
                tool_results=[{"id": "call_1", "content": {"name": "Bluewing Logistics"}}],
            ),
            _step(
                "now count the orders",
                calls=[{"id": "call_2", "name": "order_query", "args": {}}],
                tool_results=[{"id": "call_2", "content": {"total": 12}}],
            ),
            _step(_FINAL),
        ],
    }


def _run(entry: object, scenario: Scenario | None = None):
    scenario = scenario or Scenario(
        name="per_step_case", prompt="how many units?", target_facts=[["12 units"]]
    )
    runtime = InMemoryRuntime(scripted_responses=[entry])
    return run_scenario(scenario, runtime)


class TestAdoptionHappyPath:
    def test_per_step_turns_are_adopted_into_the_trace(self) -> None:
        result = _run(_scripted_entry())
        trace = result.runs[0].trace
        roles = [turn.role for turn in trace.turns]
        assert roles == ["user", "assistant", "assistant", "assistant"]
        assert trace.turns[1].content == "first, look the client up"
        assert trace.turns[1].tool_calls[0]["name"] == "client_lookup"
        # tool_results are preserved shape-faithfully (no normalization).
        assert trace.turns[1].tool_results == [
            {"id": "call_1", "content": {"name": "Bluewing Logistics"}}
        ]
        assert trace.turns[3].content == _FINAL
        assert result.aggregate.verdict == "PASS"

    def test_round_trips_through_save_and_load(self, tmp_path) -> None:
        from windtunnel.api.trace import load_trace, save_trace

        trace = _run(_scripted_entry()).runs[0].trace
        path = tmp_path / "run.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert [t.content for t in loaded.turns] == [t.content for t in trace.turns]
        assert loaded.turns[2].tool_results == trace.turns[2].tool_results

    def test_step_error_threads_through_turn_error(self) -> None:
        entry = _scripted_entry()
        entry["turns"][1]["error"] = "tool worker crashed"
        trace = _run(entry).runs[0].trace
        assert trace.turns[2].error == "tool worker crashed"
        assert trace.turns[3].error is None  # only the marked step

    def test_final_step_error_makes_the_run_invalid(self) -> None:
        entry = _scripted_entry()
        entry["turns"][-1]["error"] = "gateway returned an empty completion"
        result = _run(entry)
        assert result.aggregate.verdict == "INVALID"

    def test_tool_role_turns_are_accepted(self) -> None:
        entry = _scripted_entry()
        entry["turns"].insert(1, {"role": "tool", "content": "lookup result payload"})
        trace = _run(entry).runs[0].trace
        assert [turn.role for turn in trace.turns] == [
            "user", "assistant", "tool", "assistant", "assistant",
        ]


class TestAggregatedFallbackUnchanged:
    def test_absent_turns_yields_the_single_aggregated_turn(self) -> None:
        entry = _scripted_entry()
        del entry["turns"]
        trace = _run(entry).runs[0].trace
        assert [turn.role for turn in trace.turns] == ["user", "assistant"]
        assert trace.turns[1].content == _FINAL
        assert len(trace.turns[1].tool_calls) == 2
        assert trace.turns[1].tool_results == []
        assert not any(
            w.startswith("response_turns_rejected") for w in trace.worker_warnings
        )

    def test_rejected_turns_fall_back_to_the_same_aggregated_turn(self) -> None:
        entry = _scripted_entry()
        entry["turns"][0]["surprise_field"] = True  # unknown field → reject
        trace = _run(entry).runs[0].trace
        assert [turn.role for turn in trace.turns] == ["user", "assistant"]
        assert trace.turns[1].content == _FINAL
        rejections = [
            w for w in trace.worker_warnings if w.startswith("response_turns_rejected")
        ]
        assert len(rejections) == 1
        assert "surprise_field" in rejections[0]


class TestValidationRules:
    """Direct adopt_response_turns cases: every bad shape rejects loudly."""

    def _adopt(self, turns: object, reply: str = _FINAL):
        return adopt_response_turns({"content": reply, "turns": turns}, reply, 100.0)

    def _reason(self, turns: object, reply: str = _FINAL) -> str:
        adopted, warnings = self._adopt(turns, reply)
        assert adopted is None
        assert len(warnings) == 1
        assert warnings[0].startswith("response_turns_rejected: ")
        assert warnings[0].endswith("falling back to the aggregated turn")
        return warnings[0]

    def test_absent_key_is_not_a_rejection(self) -> None:
        adopted, warnings = adopt_response_turns({"content": _FINAL}, _FINAL, 100.0)
        assert adopted is None
        assert warnings == []

    def test_non_list_and_empty_list(self) -> None:
        assert "non-empty list" in self._reason("not-a-list")
        assert "non-empty list" in self._reason([])

    def test_non_dict_entry(self) -> None:
        assert "turns[0] is not an object" in self._reason(["just text"])

    def test_unknown_field(self) -> None:
        assert "unknown field" in self._reason([_step(_FINAL, thought="x")])

    def test_missing_required_field(self) -> None:
        assert "missing field" in self._reason([{"role": "assistant"}])

    def test_role_must_be_assistant_or_tool(self) -> None:
        assert "role" in self._reason([{"role": "user", "content": _FINAL}])

    def test_content_must_be_a_string(self) -> None:
        assert "content" in self._reason([{"role": "assistant", "content": None}])

    def test_tool_lists_must_be_lists_of_objects(self) -> None:
        assert "tool_calls" in self._reason(
            [_step(_FINAL, calls=[["not", "an", "object"]])]
        )
        assert "tool_results" in self._reason([_step(_FINAL, tool_results="nope")])

    def test_latency_and_error_types(self) -> None:
        assert "latency_ms" in self._reason([_step(_FINAL, latency_ms="fast")])
        assert "error" in self._reason([_step(_FINAL, error=500)])

    def test_at_least_one_assistant_step(self) -> None:
        assert "assistant step" in self._reason([{"role": "tool", "content": "x"}])

    def test_last_assistant_must_carry_the_final_text(self) -> None:
        """Answer-turn selection scores the last assistant turn — a final
        step diverging from the reply text would change what gets scored."""
        reason = self._reason([_step("first"), _step("something else entirely")])
        assert "last assistant step" in reason

    def test_trailing_tool_turn_after_the_final_assistant_step_is_fine(self) -> None:
        adopted, warnings = self._adopt([_step(_FINAL), {"role": "tool", "content": "x"}])
        assert warnings == []
        assert adopted is not None and len(adopted) == 2


class TestLatencyAndErrorRules:
    def test_measured_latency_lands_on_last_assistant_when_steps_carry_none(self) -> None:
        adopted, _ = adopt_response_turns(
            {"content": _FINAL, "turns": [_step("a"), _step(_FINAL)]}, _FINAL, 250.0
        )
        assert adopted is not None
        assert adopted[0].latency_ms == 0.0
        assert adopted[1].latency_ms == 250.0

    def test_step_latencies_are_used_when_provided(self) -> None:
        adopted, _ = adopt_response_turns(
            {"content": _FINAL,
             "turns": [_step("a", latency_ms=40), _step(_FINAL, latency_ms=60)]},
            _FINAL, 250.0,
        )
        assert adopted is not None
        assert [turn.latency_ms for turn in adopted] == [40.0, 60.0]

    def test_response_level_error_lands_on_the_final_assistant_step(self) -> None:
        adopted, _ = adopt_response_turns(
            {"content": _FINAL, "error": "inference worker timed out",
             "turns": [_step("a"), _step(_FINAL)]},
            _FINAL, 100.0,
        )
        assert adopted is not None
        assert adopted[0].error is None
        assert adopted[1].error == "inference worker timed out"


class TestEvidenceOverPerStepTraces:
    def test_claimed_enumeration_interleaves_and_spans_stay_exact(self) -> None:
        """The claimed-call walk (extract order, transcript_call_map basis,
        page data-claim tagging) naturally interleaves across per-step
        turns; the scored turn is the last assistant step and fact spans
        slice its content exactly."""
        from windtunnel._serve.evidence import compute_evidence

        scenario = Scenario(
            name="per_step_case",
            prompt="how many units?",
            target_facts=[["12 units"]],
            must_call=["client_lookup", "order_query"],
            order_matters=True,
        )
        result = _run(_scripted_entry(), scenario)
        trace = result.runs[0].trace
        evidence = compute_evidence(scenario, trace)

        trajectory = evidence["trajectory"]
        assert trajectory["evidence_source"] == "transcript"
        assert trajectory["observed_calls"] == ["client_lookup", "order_query"]
        assert trajectory["order_satisfied"] is True
        assert [entry["satisfied"] for entry in trajectory["must_call"]] == [True, True]

        outcome = evidence["outcome"]
        assert outcome["answer_turn_index"] == 3  # the last assistant step
        span = outcome["fact_groups"][0]["spans"][0]
        answer = trace.turns[outcome["answer_turn_index"]].content
        assert answer[span["start"]:span["end"]].lower() == "12 units"
        assert result.aggregate.verdict == "PASS"
