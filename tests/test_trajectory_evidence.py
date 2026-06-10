"""Trajectory evidence source — server-witnessed vs transcript.

The founding bet test bed: evaluate_trajectory must not take the
transcript's word for what the agent did. When trace.mcp_calls (the tool
server's OWN call log, drained by the runner) is non-empty, it is the sole
evidence; the transcript's tool_calls are ignored. When mcp_calls is empty
(in_memory runtime, no logging mock), the legacy transcript path applies
unchanged.

Why no clipping is needed on the server path: perturbation-INJECTED history
(fake prior tool calls shaped into the messages) never reaches the tool
server, so the server log is naturally free of calls the live model never
made. The transcript fallback keeps its existing semantics exactly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from windtunnel.api.evaluators import evaluate_trajectory, tool_name_matches
from windtunnel.api.scenario import Scenario, TrajectoryCheck
from windtunnel.api.trace import Trace, Turn, compute_hash

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _tool_call(name: str) -> dict[str, Any]:
    """Transcript-shape (OpenAI wire) tool call — the agent's own claim."""
    return {
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _mcp_call(name: str, ts: float) -> dict[str, Any]:
    """Server-witnessed call dict, the shape runner._collect_mcp_calls emits."""
    return {"tool_name": name, "args": {}, "result": "ok", "timestamp_ms": ts}


def _trace(
    transcript_tools: list[str] | None = None,
    mcp_calls: list[dict[str, Any]] | None = None,
) -> Trace:
    now = datetime.now(UTC)
    turns: list[Turn] = [
        Turn(role="user", content="q", tool_calls=[], tool_results=[], latency_ms=0.0),
    ]
    for name in (transcript_tools or []):
        turns.append(Turn(
            role="assistant",
            content="",
            tool_calls=[_tool_call(name)],
            tool_results=[],
            latency_ms=0.0,
        ))
    turns.append(Turn(
        role="assistant", content="done", tool_calls=[], tool_results=[], latency_ms=0.0,
    ))
    return Trace(
        scenario_id="s01",
        agent_id="a",
        variant_id="v",
        model="m",
        quant="q",
        sampler={},
        started_at=now,
        finished_at=now,
        turns=turns,
        tool_schema_hash=compute_hash("[]"),
        mcp_calls=mcp_calls or [],
    )


def _scenario(**kwargs: Any) -> Scenario:
    return Scenario(name="t", prompt="q", target_facts=[["done"]], **kwargs)


# ─── Server-witnessed path ────────────────────────────────────────────────────

class TestServerWitnessed:
    def test_transcript_claim_without_server_witness_fails(self) -> None:
        """THE founding-bet case: the transcript CLAIMS the must_call tool but
        the server never saw it → FAIL. Don't take the transcript's word."""
        trace = _trace(
            transcript_tools=["mcp_acme_ops_client_lookup"],  # claimed…
            mcp_calls=[_mcp_call("ops_order_report", 1.0)],   # …but not witnessed
        )
        result = evaluate_trajectory(trace, _scenario(must_call=["client_lookup"]))
        assert result.passed is False
        assert "server-witnessed" in result.detail

    def test_server_witness_satisfies_must_call(self) -> None:
        """Server log alone satisfies must_call — even with an empty transcript,
        and through the same prefix-chain normalizer (canonical 'client_lookup'
        matches witnessed 'ops_client_lookup')."""
        trace = _trace(
            transcript_tools=[],
            mcp_calls=[_mcp_call("ops_client_lookup", 1.0)],
        )
        result = evaluate_trajectory(trace, _scenario(must_call=["client_lookup"]))
        assert result.passed is True
        assert "server-witnessed" in result.detail

    def test_forbidden_call_omitted_from_transcript_still_caught(self) -> None:
        """The dual failure mode: agent silently calls a forbidden tool and
        doesn't report it. The server saw it → FAIL."""
        trace = _trace(
            transcript_tools=["ops_client_lookup"],  # transcript looks clean
            mcp_calls=[
                _mcp_call("ops_client_lookup", 1.0),
                _mcp_call("ops_order_report", 2.0),  # the unreported call
            ],
        )
        result = evaluate_trajectory(trace, _scenario(forbidden_calls=["order_report"]))
        assert result.passed is False
        assert "server-witnessed" in result.detail

    def test_injected_transcript_forbidden_call_ignored(self) -> None:
        """Perturbation-injected fake calls live only in the transcript — they
        never hit the server. Server evidence must not be polluted by them
        (this is why the server path needs no clipping)."""
        trace = _trace(
            transcript_tools=["example_wrong_tool"],  # injected, never executed
            mcp_calls=[_mcp_call("ops_client_lookup", 1.0)],
        )
        result = evaluate_trajectory(
            trace,
            _scenario(must_call=["client_lookup"], forbidden_calls=["example_wrong_tool"]),
        )
        assert result.passed is True

    def test_order_check_follows_timestamps_not_list_order(self) -> None:
        """Server calls are ordered by timestamp_ms, not list position —
        merged multi-server logs may arrive interleaved."""
        scenario = _scenario(must_call=["a_tool", "b_tool"], order_matters=True)
        # Listed b-then-a, but timestamps say a happened first → PASS
        trace = _trace(mcp_calls=[
            _mcp_call("b_tool", 2.0),
            _mcp_call("a_tool", 1.0),
        ])
        assert evaluate_trajectory(trace, scenario).passed is True
        # Timestamps say b happened first → order violated → FAIL
        trace = _trace(mcp_calls=[
            _mcp_call("a_tool", 2.0),
            _mcp_call("b_tool", 1.0),
        ])
        result = evaluate_trajectory(trace, scenario)
        assert result.passed is False
        assert "order violated" in result.detail


# ─── Transcript fallback path ─────────────────────────────────────────────────

class TestTranscriptFallback:
    def test_fallback_when_mcp_calls_empty(self) -> None:
        """No logging mock in play (mcp_calls=[]) → legacy transcript scoring."""
        trace = _trace(transcript_tools=["mcp_acme_ops_client_lookup"], mcp_calls=[])
        result = evaluate_trajectory(trace, _scenario(must_call=["client_lookup"]))
        assert result.passed is True
        assert "transcript" in result.detail

    def test_fallback_forbidden_semantics_unchanged(self) -> None:
        trace = _trace(transcript_tools=["ops_order_report"], mcp_calls=[])
        result = evaluate_trajectory(trace, _scenario(forbidden_calls=["order_report"]))
        assert result.passed is False
        assert "transcript" in result.detail

    def test_fallback_missing_must_call_fails(self) -> None:
        trace = _trace(transcript_tools=[], mcp_calls=[])
        result = evaluate_trajectory(trace, _scenario(must_call=["client_lookup"]))
        assert result.passed is False
        assert "transcript" in result.detail


# ─── Custom TrajectoryCheck (scenario.trajectory_checks) ─────────────────────

class _MaxCalls(TrajectoryCheck):
    """A realistic custom check: cap the number of calls to one tool."""

    def __init__(self, canonical: str, budget: int) -> None:
        self.canonical = canonical
        self.budget = budget

    def check(self, calls: list[str]) -> tuple[bool, str]:
        n = sum(1 for c in calls if tool_name_matches(self.canonical, c))
        if n > self.budget:
            return False, f"called {self.canonical} {n}x, budget {self.budget}"
        return True, f"{self.canonical} within budget"


class _Recorder(TrajectoryCheck):
    """Records the calls list it was given (to assert the evidence source)."""

    def __init__(self) -> None:
        self.seen: list[str] | None = None

    def check(self, calls: list[str]) -> tuple[bool, str]:
        self.seen = list(calls)
        return True, "recorded"


class _Boom(TrajectoryCheck):
    def check(self, calls: list[str]) -> tuple[bool, str]:
        raise RuntimeError("kaboom")


class TestCustomTrajectoryChecks:
    def test_failing_custom_check_flips_trajectory_to_fail(self) -> None:
        trace = _trace(transcript_tools=["client_lookup", "client_lookup", "client_lookup"])
        result = evaluate_trajectory(
            trace, _scenario(trajectory_checks=[_MaxCalls("client_lookup", 2)])
        )
        assert result.passed is False
        assert "called client_lookup 3x, budget 2" in result.detail

    def test_passing_custom_check_leaves_trajectory_green(self) -> None:
        trace = _trace(transcript_tools=["client_lookup"])
        result = evaluate_trajectory(
            trace, _scenario(trajectory_checks=[_MaxCalls("client_lookup", 2)])
        )
        assert result.passed is True
        assert "trajectory requirements satisfied" in result.detail

    def test_sugar_fields_and_custom_checks_compose(self) -> None:
        """Built-in must_call passes but the custom check fails → layer fails;
        custom check passes but forbidden_calls trips → layer fails too."""
        trace = _trace(transcript_tools=["client_lookup", "client_lookup"])
        result = evaluate_trajectory(
            trace,
            _scenario(
                must_call=["client_lookup"],
                trajectory_checks=[_MaxCalls("client_lookup", 1)],
            ),
        )
        assert result.passed is False
        assert "budget 1" in result.detail

        trace = _trace(transcript_tools=["client_lookup", "order_report"])
        result = evaluate_trajectory(
            trace,
            _scenario(
                forbidden_calls=["order_report"],
                trajectory_checks=[_MaxCalls("client_lookup", 5)],
            ),
        )
        assert result.passed is False
        assert "forbidden tools called" in result.detail

    def test_custom_check_sees_server_witnessed_calls(self) -> None:
        """When mcp_calls is non-empty, custom checks receive the SERVER's
        chronological call list — not the transcript's claims."""
        recorder = _Recorder()
        trace = _trace(
            transcript_tools=["fake_claimed_tool"],
            mcp_calls=[
                _mcp_call("ops_client_lookup", 2.0),
                _mcp_call("ops_order_report", 1.0),
            ],
        )
        result = evaluate_trajectory(trace, _scenario(trajectory_checks=[recorder]))
        assert result.passed is True
        assert recorder.seen == ["ops_order_report", "ops_client_lookup"]  # ts order

    def test_custom_check_sees_transcript_calls_on_fallback(self) -> None:
        recorder = _Recorder()
        trace = _trace(transcript_tools=["mcp_acme_ops_client_lookup"], mcp_calls=[])
        evaluate_trajectory(trace, _scenario(trajectory_checks=[recorder]))
        assert recorder.seen == ["mcp_acme_ops_client_lookup"]

    def test_raising_custom_check_is_recorded_as_failure(self) -> None:
        """Same forgiveness as Policy predicates — never crashes the evaluator."""
        trace = _trace(transcript_tools=["client_lookup"])
        result = evaluate_trajectory(trace, _scenario(trajectory_checks=[_Boom()]))
        assert result.passed is False
        assert "_Boom(error: kaboom)" in result.detail
