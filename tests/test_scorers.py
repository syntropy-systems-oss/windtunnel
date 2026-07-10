"""Tests for windtunnel.api.scorers outcome-function helpers.

These tests stay close to the public contracts in docs/design/0001:
scorers return LayerResult, compose into Scenario.outcome_fn, reuse the
existing fact and NumberFact matchers, and keep provenance tied to frozen
trace evidence rather than model self-report.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from windtunnel.api import (
    LayerResult,
    NumberFact,
    Scenario,
    Trace,
    Turn,
    all_of,
    any_of,
    compute_hash,
    evaluate_constraint,
    evaluate_outcome,
    llm_judge,
    no_divergence,
    observation,
    substantiated_by_tools,
)


def _turn(
    role: str,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        latency_ms=0.0,
    )


def _trace(
    final_answer: str = "done",
    *,
    mcp_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    observations: dict[str, Any] | None = None,
    worker_warnings: list[str] | None = None,
) -> Trace:
    now = datetime.now(UTC)
    turns = [_turn("user", "question")]
    if tool_results:
        turns.append(_turn("tool", tool_results=tool_results))
    turns.append(_turn("assistant", final_answer))
    return Trace(
        scenario_id="s01",
        agent_id="agent",
        variant_id="variant",
        model="model",
        quant="q",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=turns,
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=worker_warnings or [],
        mcp_calls=mcp_calls or [],
        observations=observations or {},
    )


def _mcp_result(result: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    call = {
        "tool_name": "client_lookup",
        "args": {},
        "result": result,
        "timestamp_ms": 1.0,
    }
    if extra is not None:
        call["extra"] = extra
    return call


class TestCombinators:
    def test_all_of_joins_failure_details(self) -> None:
        scorer = all_of(
            lambda _t: LayerResult(True, "ok"),
            lambda _t: LayerResult(False, "missing account"),
            lambda _t: LayerResult(False, "missing email"),
        )

        result = scorer(_trace())

        assert result.passed is False
        assert result.detail == "missing account; missing email"

    def test_any_of_joins_failure_details_when_none_pass(self) -> None:
        scorer = any_of(
            lambda _t: LayerResult(False, "not csv"),
            lambda _t: LayerResult(False, "not json"),
        )

        result = scorer(_trace())

        assert result.passed is False
        assert result.detail == "not csv; not json"

    def test_any_of_passes_on_first_success(self) -> None:
        scorer = any_of(
            lambda _t: LayerResult(False, "not csv"),
            lambda _t: LayerResult(True, "json matched"),
        )

        result = scorer(_trace())

        assert result.passed is True
        assert "json matched" in result.detail

    def test_child_scorer_exception_becomes_failure_detail(self) -> None:
        def _boom(_trace: Trace) -> LayerResult:
            raise RuntimeError("scorer exploded")

        scenario = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            outcome_fn=all_of(lambda _t: LayerResult(True, "ok"), _boom),
        )

        result = evaluate_outcome(_trace(), scenario)

        assert result.passed is False
        assert "scorer exploded" in result.detail
        assert "outcome_fn error" not in result.detail


class TestObservation:
    def test_nested_dict_path_passes(self) -> None:
        trace = _trace(observations={"db": {"client": {"email": "ops@example.test"}}})

        result = observation("db", "client.email", lambda v: v == "ops@example.test", "email")(trace)

        assert result.passed is True

    def test_list_index_path_passes(self) -> None:
        trace = _trace(observations={"github": {"prs": [{"base": "main"}]}})

        result = observation("github", "prs[0].base", lambda v: v == "main", "base")(trace)

        assert result.passed is True

    def test_dotted_list_index_path_passes(self) -> None:
        trace = _trace(observations={"github": {"prs": [{"merged": True}]}})

        result = observation("github", "prs.0.merged", lambda v: v is True, "merged")(trace)

        assert result.passed is True

    def test_missing_source_fails_without_raise(self) -> None:
        result = observation("missing", "x", lambda _v: True, "source")(_trace())

        assert result.passed is False
        assert "missing observation source" in result.detail

    def test_missing_path_fails_without_raise(self) -> None:
        trace = _trace(observations={"db": {"client": {}}})

        result = observation("db", "client.email", lambda _v: True, "email")(trace)

        assert result.passed is False
        assert "missing path" in result.detail
        assert "email" in result.detail


class TestLLMJudge:
    def test_pass_response_passes_and_prompt_contains_inputs(self) -> None:
        prompts: list[str] = []

        def _generate(turns: list[Turn]) -> list[Turn]:
            prompts.append(turns[0].content)
            return [_turn("assistant", "PASS")]

        trace = _trace(
            "The answer is 12.",
            mcp_calls=[_mcp_result({"orders": 12})],
        )

        result = llm_judge("Pass when the answer uses tool evidence.", _generate)(trace)

        assert result.passed is True
        assert "Pass when the answer uses tool evidence." in prompts[0]
        assert "The answer is 12." in prompts[0]
        assert "orders" in prompts[0]

    def test_fail_response_fails(self) -> None:
        result = llm_judge(
            "Pass only if complete.",
            lambda _turns: [_turn("assistant", "FAIL")],
        )(_trace())

        assert result.passed is False
        assert result.detail == "llm_judge: FAIL"

    def test_garbage_response_fails_with_raw_response(self) -> None:
        result = llm_judge(
            "Return a strict verdict.",
            lambda _turns: [_turn("assistant", "PASS because it looks fine")],
        )(_trace())

        assert result.passed is False
        assert "parse failure" in result.detail
        assert "PASS because it looks fine" in result.detail


class TestSubstantiatedByTools:
    def test_mcp_call_results_are_server_witnessed_evidence(self) -> None:
        trace = _trace(
            "Portland Pickles has 12 orders.",
            mcp_calls=[_mcp_result({"client": "Portland Pickles", "orders": 12})],
        )

        result = substantiated_by_tools([["Portland Pickles"], NumberFact(12)])(trace)

        assert result.passed is True
        assert "server-witnessed" in result.detail

    def test_transcript_tool_results_are_fallback_evidence(self) -> None:
        trace = _trace(
            "Bluewing has 7 orders.",
            tool_results=[{"tool_call_id": "call_1", "content": '{"client": "Bluewing", "orders": 7}'}],
        )

        result = substantiated_by_tools([["Bluewing"], NumberFact(7)])(trace)

        assert result.passed is True
        assert "transcript" in result.detail

    def test_known_empty_server_evidence_does_not_fall_back_to_transcript_results(self) -> None:
        trace = _trace(
            "Bluewing has 7 orders.",
            tool_results=[{"content": '{"client": "Bluewing", "orders": 7}'}],
            worker_warnings=["mcp_evidence: available"],
        )

        result = substantiated_by_tools([NumberFact(7)])(trace)

        assert result.passed is False
        assert "server-witnessed" in result.detail

    def test_single_string_fact_is_one_fact_not_characters(self) -> None:
        trace = _trace(
            "Bluewing has orders.",
            mcp_calls=[_mcp_result({"client": "Bluewing"})],
        )

        result = substantiated_by_tools("Bluewing")(trace)

        assert result.passed is True

    def test_numbers_from_nowhere_fail(self) -> None:
        trace = _trace(
            "Bluewing has 12 orders.",
            mcp_calls=[_mcp_result({"client": "Bluewing", "orders": 9})],
        )

        result = substantiated_by_tools()(trace)

        assert result.passed is False
        assert "unsubstantiated numeric facts" in result.detail
        assert "12" in result.detail


class TestNoDivergence:
    def test_passes_without_divergence_evidence(self) -> None:
        scenario = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            policies=[no_divergence()],
        )

        result = evaluate_constraint(_trace(), scenario)

        assert result.passed is True

    def test_fails_on_worker_warning_divergence(self) -> None:
        scenario = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            policies=[no_divergence()],
        )
        trace = _trace(worker_warnings=["universe_divergence: tool=client_lookup policy=empty"])

        result = evaluate_constraint(trace, scenario)

        assert result.passed is False
        assert "no_divergence" in result.detail

    def test_fails_on_mcp_extra_divergence(self) -> None:
        scenario = Scenario(
            name="s",
            prompt="p",
            target_facts=[],
            policies=[no_divergence()],
        )
        trace = _trace(
            mcp_calls=[
                _mcp_result(
                    {"error": "no_recorded_result"},
                    extra={"divergence": {"policy": "fail_call", "matched": None}},
                )
            ]
        )

        result = evaluate_constraint(trace, scenario)

        assert result.passed is False
        assert "no_divergence" in result.detail


class TestOutcomeFnRaiseSemantics:
    def test_raw_outcome_fn_exception_is_still_caught_by_evaluator(self) -> None:
        def _boom(_trace: Trace) -> LayerResult:
            raise RuntimeError("raw scorer exploded")

        scenario = Scenario(name="s", prompt="p", target_facts=[], outcome_fn=_boom)

        result = evaluate_outcome(_trace(), scenario)

        assert result.passed is False
        assert "outcome_fn error" in result.detail
        assert "raw scorer exploded" in result.detail
