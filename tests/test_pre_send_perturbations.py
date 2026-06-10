"""Tests for pre-send (history-shaping) perturbations.

Every history/context-shaping perturbation subclasses ``PreSendPerturbation``
(abstract ``shape_messages(messages, scenario)``; the runner dispatches on
isinstance) so the runner injects the corrupted prior turns into the live
``messages`` BEFORE ``handle.send()`` — the model genuinely runs its scored
turn on top of the poison instead of scoring a post-hoc counterfactual it
never saw.

These tests assert two layers:
  1. Unit: each shape_messages() injects the expected prior turns and preserves
     the final (scored) user turn as the last message.
  2. Integration: run_scenario() through the InMemoryRuntime actually delivers
     the injected messages to send(), records the perturbation marker, and does
     NOT double-apply (post-hoc apply() is skipped for pre_send perturbations).
"""
from __future__ import annotations

from windtunnel.api.perturbations import (
    BlankAssistantContent,
    FallbackRenderLeak,
    InjectPaginationTruncation,
    InjectSchemaRejectedCall,
    InjectStaleMemory,
    InjectWrongPriorToolCall,
    MalformedToolCall,
)
from windtunnel.api.runner import run_scenario
from windtunnel.api.scenario import PreSendPerturbation, Scenario
from windtunnel.runtimes.in_memory import InMemoryRuntime

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _scenario(perturbations: list, **kw) -> Scenario:
    return Scenario(
        name=kw.pop("name", "ps_test"),
        prompt=kw.pop("prompt", "What is the contact email for Bluewing Logistics?"),
        target_facts=kw.pop("target_facts", [["Bluewing Logistics", "ACC-BLWG-001"], ["joe@x.example"]]),
        must_call=kw.pop("must_call", ["mcp_acme_ops_client_lookup"]),
        perturbations=perturbations,
        **kw,
    )


def _base_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a back-office ops suite agent."},
        {"role": "user", "content": "What is the contact email for Bluewing Logistics?"},
    ]


def _roles(messages: list[dict]) -> list[str]:
    return [m["role"] for m in messages]


ALL_PRE_SEND = [
    BlankAssistantContent(turn_idx=1),
    FallbackRenderLeak(turn_idx=1),
    MalformedToolCall(turn_idx=1, arg_corruption_mode="wrong_field_names"),
    InjectWrongPriorToolCall(
        turn_idx=0, wrong_tool_name="mcp_acme_ops_product_lookup", fake_result="{}"
    ),
    InjectSchemaRejectedCall(
        turn_idx=0,
        tool_name="mcp_acme_ops_order_query",
        bad_arg={"client": "ACC-BLWG-001", "stage": "Intake"},
    ),
    InjectPaginationTruncation(turn_idx=2, truncate_at=1),
    InjectStaleMemory(key="bluewing_email_provider", value="Bluewing uses Gmail."),
]


# ─── Unit: every pre_send perturbation declares the contract ─────────────────

class TestPreSendContract:
    def test_all_are_pre_send_perturbations(self) -> None:
        """The runner dispatches on class identity, not duck-typing."""
        for p in ALL_PRE_SEND:
            assert isinstance(p, PreSendPerturbation), type(p).__name__

    def test_pre_send_classvar_stays_truthful(self) -> None:
        """pre_send is a ClassVar on the base — legacy getattr() call sites
        (pack predicates, external tooling) still read True."""
        for p in ALL_PRE_SEND:
            assert getattr(p, "pre_send", False) is True, type(p).__name__

    def test_exported_from_public_api(self) -> None:
        import windtunnel.api as api

        assert api.PreSendPerturbation is PreSendPerturbation
        assert "PreSendPerturbation" in api.__all__

    def test_final_user_turn_preserved_as_last(self) -> None:
        # All except InjectStaleMemory (which prepends a system message) must keep
        # the scored user turn as the final message.
        for p in ALL_PRE_SEND:
            out = p.shape_messages(_base_messages(), _scenario([p]))
            assert out[-1]["role"] == "user", type(p).__name__
            assert "Bluewing Logistics" in out[-1]["content"], type(p).__name__

    def test_injection_grows_history(self) -> None:
        import json
        for p in ALL_PRE_SEND:
            out = p.shape_messages(_base_messages(), _scenario([p]))
            # Either more messages (turn injection) OR more content (stale-memory
            # merges into the existing system message rather than adding one).
            grew = json.dumps(out, default=str)
            base = json.dumps(_base_messages(), default=str)
            assert len(grew) > len(base), type(p).__name__


# ─── Unit: per-perturbation injected shape ───────────────────────────────────

class TestInjectedShapes:
    def test_blank_assistant_injects_blank_synthesis(self) -> None:
        out = BlankAssistantContent(turn_idx=1).shape_messages(_base_messages(), _scenario([]))
        # ... system, assistant(tool_call, blank), tool, assistant(blank), user
        assert _roles(out) == ["system", "assistant", "tool", "assistant", "user"]
        # the poison: a blank assistant synthesis right before the scored user turn
        assert out[-2]["role"] == "assistant" and out[-2]["content"] == ""

    def test_fallback_leak_injects_literal_tool_text(self) -> None:
        out = FallbackRenderLeak(turn_idx=1).shape_messages(_base_messages(), _scenario([]))
        assert _roles(out) == ["system", "assistant", "user"]
        assert out[1]["content"].startswith("tool: {")

    def test_malformed_tool_call_injects_wrong_field_names(self) -> None:
        out = MalformedToolCall(turn_idx=1).shape_messages(_base_messages(), _scenario([]))
        assert _roles(out) == ["system", "assistant", "tool", "user"]
        args = out[1]["tool_calls"][0]["function"]["arguments"]
        assert "x_query" in args  # canonical 'query' mangled to 'x_query'
        assert '"matches": []' in out[2]["content"]

    def test_schema_rejected_injects_error_result(self) -> None:
        p = InjectSchemaRejectedCall(
            turn_idx=0,
            tool_name="mcp_acme_ops_order_query",
            bad_arg={"stage": "Intake"},
        )
        out = p.shape_messages(_base_messages(), _scenario([]))
        assert _roles(out) == ["system", "assistant", "tool", "user"]
        assert out[1]["tool_calls"][0]["function"]["name"] == "mcp_acme_ops_order_query"
        assert "Schema validation error" in out[2]["content"]
        assert "stage" in out[2]["content"]

    def test_pagination_truncation_injects_hasmore(self) -> None:
        out = InjectPaginationTruncation(turn_idx=2, truncate_at=1).shape_messages(
            _base_messages(), _scenario([], must_call=["mcp_acme_ops_order_query"])
        )
        assert _roles(out) == ["system", "assistant", "tool", "user"]
        import json
        outer = json.loads(out[2]["content"])
        inner = json.loads(outer["result"])
        assert inner["pagination"]["hasMore"] is True

    def test_stale_memory_injects_into_system(self) -> None:
        out = InjectStaleMemory(key="k", value="Bluewing uses Gmail.").shape_messages(
            _base_messages(), _scenario([])
        )
        sys = next(m for m in out if m["role"] == "system")
        assert "Relevant saved memory:" in sys["content"]
        assert "Bluewing uses Gmail." in sys["content"]

    def test_two_stale_memories_accumulate(self) -> None:
        msgs = _base_messages()
        msgs = InjectStaleMemory(key="k", value="older: Gmail").shape_messages(msgs, _scenario([]))
        msgs = InjectStaleMemory(key="k", value="newer: Outlook").shape_messages(msgs, _scenario([]))
        sys = next(m for m in msgs if m["role"] == "system")
        assert "older: Gmail" in sys["content"]
        assert "newer: Outlook" in sys["content"]
        # one header, not two
        assert sys["content"].count("Relevant saved memory:") == 1


# ─── Integration: runner delivers injection + skips post-hoc apply ───────────

class TestRunnerWiring:
    def test_injected_messages_reach_send(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["The email is joe@x.example"])
        scenario = _scenario([
            InjectWrongPriorToolCall(
                turn_idx=0,
                wrong_tool_name="mcp_acme_ops_product_lookup",
                fake_result='{"result": "wrong"}',
            )
        ])
        run_scenario(scenario, runtime)
        _, handle = runtime.provisions[0]
        sent_messages, _sid = handle.calls[0]
        names = [
            tc["function"]["name"]
            for m in sent_messages
            for tc in (m.get("tool_calls") or [])
        ]
        # The injected wrong prior tool call is present in what the model received.
        assert "mcp_acme_ops_product_lookup" in names
        # The scored user turn is still the final message.
        assert sent_messages[-1]["role"] == "user"

    def test_marker_recorded_and_no_double_apply(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["ok"])
        pert = BlankAssistantContent(turn_idx=1)
        scenario = _scenario([pert], must_call=["mcp_acme_ops_order_query"])
        result = run_scenario(scenario, runtime)
        trace = result.runs[0].trace
        # Marker present so evaluate_robustness still sees it applied...
        assert any(pert.marker in str(w) for w in trace.worker_warnings)
        # ...but apply() was SKIPPED: the recorded trace is just the live
        # user+assistant turns, NOT re-mutated with injected prior turns.
        assert [t.role for t in trace.turns] == ["user", "assistant"]

    def test_stale_memory_reaches_send_as_system_context(self) -> None:
        runtime = InMemoryRuntime(scripted_responses=["outlook"])
        pert = InjectStaleMemory(
            key="bluewing_email_provider", value="Bluewing uses Gmail."
        )
        scenario = _scenario([pert], must_call=["mcp_acme_ops_client_lookup"])
        run_scenario(scenario, runtime)
        _, handle = runtime.provisions[0]
        sent_messages, _sid = handle.calls[0]
        sys_msgs = [m for m in sent_messages if m["role"] == "system"]
        assert sys_msgs, "expected an injected system memory context"
        assert any("Bluewing uses Gmail." in m["content"] for m in sys_msgs)
