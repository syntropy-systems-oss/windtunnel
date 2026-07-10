"""Perturbation library — adversarial conditions with integrity evidence.

Each perturbation is a dataclass that:
1. Inherits from Perturbation — or PreSendPerturbation for the
   history-shaping subfamily (both abcs defined in scenario.py)
2. Implements apply(trace) -> Trace — returns a NEW trace, never mutates
3. Injects a 'perturbation_applied: <marker>' entry into worker_warnings
   so evaluate_integrity can verify it was applied

Integration notes:
- CorruptPriorAssistantTurn and InjectStaleMemory operate purely on the
  Trace object — they can run without any live agent container.
- ToolTimeout and ToolReturnsMalformed are interface definitions for
  mock-MCP failure injection. They record the knob config in
  worker_warnings so the integrity evaluator sees the marker, but the
  actual injection into the MCP call path is wired by the runner/driver.
  Currently they apply to trace metadata only.

Literal tool text used by corrupt mode='literal_tool_text':
  "<tool_call>\n{\"name\": \"example\"}\n</tool_call>"
  This is the leaked template artifact shape that the ICL-poisoning dim
  stresses. The exact string is intentionally the kind of
  content that confuses a chat-template parser.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from windtunnel.api.scenario import Perturbation, PreSendPerturbation
from windtunnel.api.trace import Trace, Turn

# The canonical "leaked template artifact" content for mode='literal_tool_text'
_LITERAL_TOOL_TEXT = '<tool_call>\n{"name": "example_tool", "arguments": {}}\n</tool_call>'


def _copy_trace_with_turns(trace: Trace, new_turns: list[Turn], extra_warning: str) -> Trace:
    """Return a deep-copied Trace with replaced turns and an appended warning."""
    new_trace = copy.deepcopy(trace)
    object.__setattr__(new_trace, "turns", new_turns)
    new_warnings = list(new_trace.worker_warnings) + [extra_warning]
    object.__setattr__(new_trace, "worker_warnings", new_warnings)
    return new_trace


# ─── Pre-send history-shaping helpers ─────────────────────────────────────────
# These support the PreSendPerturbation.shape_messages(messages, scenario)
# contract (base class in scenario.py): instead of mutating a Trace AFTER the
# model already ran (a counterfactual the model never saw), pre-send
# perturbations inject corrupted prior turns into the MESSAGES the live model
# is about to run on — so the eval is REAL (the model either succumbs to or
# resists the poison). The runner calls shape_messages before handle.send()
# and SKIPS apply() for PreSendPerturbation instances (no double application).

# A neutral, plausible tool-result blob for synthetic prior turns whose exact
# payload doesn't matter (the poison is the turn's SHAPE, not its data).
_GENERIC_TOOL_RESULT = '{"result": "{\\"note\\": \\"lookup completed\\"}"}'


def _msg_role(m: object) -> object:
    """Role of a message, handling both dict and object message shapes."""
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)


def _insert_before_final_user(
    messages: list[dict[str, Any]], injected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Insert `injected` messages just before the final user turn.

    If the last message is the (scored) user turn, the injected prior turns go
    right before it — becoming conversation history the live model runs on top
    of. If there's no trailing user turn, append at the end.
    """
    out = list(messages)
    insert_at = len(out) - 1 if out and _msg_role(out[-1]) == "user" else len(out)
    out[insert_at:insert_at] = injected
    return out


def _first_must_call(scenario: object) -> str | None:
    """First tool the scenario expects (for synthesizing a plausible prior call)."""
    mc = getattr(scenario, "must_call", None) or []
    return mc[0] if mc else None


def _entity(scenario: object) -> str:
    """A human-readable entity name for synthetic prior-call args.

    Prefer a multi-word target fact (e.g. 'Bluewing Logistics'); fall
    back to the first fact, then a neutral placeholder. Keeps synthetic prior
    calls grounded in the scenario without hardcoding per-scenario strings.
    """
    facts = getattr(scenario, "target_facts", None) or []
    for group in facts:
        for f in group:
            if isinstance(f, str) and " " in f:
                return f
    for group in facts:
        if group and isinstance(group[0], str):
            return group[0]
    return "the requested record"


@dataclass
class CorruptPriorAssistantTurn(Perturbation):
    """Corrupt the content of an assistant turn at a given index.

    Simulates ICL poisoning: a prior turn is either blanked (mode='empty')
    or replaced with literal tool-template text (mode='literal_tool_text').
    The corrupted turn stays in the conversation history so the model sees
    a malformed prior context.

    idx: 0-based index into trace.turns (any role; the apply() targets
         the turn at that index regardless of role).
    mode:
      'empty'            — sets content="" (the canonical empty-turn ICL bug)
      'literal_tool_text'— sets content to leaked template artifact
    """
    idx: int
    mode: str = "empty"  # 'empty' | 'literal_tool_text'

    @property
    def marker(self) -> str:
        return f"perturbation_applied: corrupt_prior_assistant_turn idx={self.idx} mode={self.mode}"

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        if self.idx < len(new_turns):
            target = new_turns[self.idx]
            if self.mode == "empty":
                new_content = ""
            else:
                new_content = _LITERAL_TOOL_TEXT
            # Turn is a dataclass without frozen=True but we use object.__setattr__
            # defensively in case it becomes frozen later.
            new_turn = Turn(
                role=target.role,
                content=new_content,
                tool_calls=list(target.tool_calls),
                tool_results=list(target.tool_results),
                latency_ms=target.latency_ms,
                rendered_prompt=target.rendered_prompt,
            )
            new_turns[self.idx] = new_turn
        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class InjectStaleMemory(PreSendPerturbation):
    """Seed a stale memory key/value before the scenario runs.

    Simulates the memory-conflict dim: when durable memory
    says X but the current tool says Y. The actual memory injection into
    agent state is handled by the runner; this perturbation
    records the intent and marks the trace so evaluate_integrity can
    verify it was applied.

    key:   the memory key to inject (e.g. "user_pref", "client_id")
    value: the stale value to seed (e.g. "imperial units", "ACC-OLD-001")
    """
    key: str
    value: str

    @property
    def marker(self) -> str:
        return f"perturbation_applied: inject_stale_memory key={self.key}"

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)

    # ── Pre-send history shaping ─────────────────────────────────────────────
    # Surfaces the stale memory into the system context — which is how agent
    # memory recall typically works (recalled memories are injected into context
    # before the model runs). The live model then sees the stale memory AND must
    # prefer the current tool result over it. Two InjectStaleMemory perturbations
    # on one scenario accumulate (each appends its own line to the same block).
    #
    # Note: dim_memory_conflict has a dedicated runner that can seed REAL agent
    # memory files; this message-level injection is the faithful equivalent for
    # the main `wt run` path, which drives the model via a messages list.
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Inject the stale memory value as a recalled-memory system context."""
        memory_line = f"- {self.value}"
        out = list(messages)
        header = "Relevant saved memory:"
        for i, m in enumerate(out):
            if _msg_role(m) != "system":
                continue
            existing = (
                m.get("content", "") if isinstance(m, dict)
                else getattr(m, "content", "") or ""
            )
            if header in existing:
                merged = existing.rstrip() + "\n" + memory_line
            elif existing:
                merged = existing.rstrip() + "\n\n" + header + "\n" + memory_line
            else:
                merged = header + "\n" + memory_line
            if isinstance(m, dict):
                nm = dict(m)
                nm["content"] = merged
                out[i] = nm
            else:
                out[i] = {"role": "system", "content": merged}
            return out
        out.insert(0, {"role": "system", "content": header + "\n" + memory_line})
        return out


@dataclass
class ToolTimeout(Perturbation):
    """Configure a probability-based tool timeout knob.

    Interface definition for mock-MCP failure injection (the mock-MCP
    layer wires the actual call intercept). This perturbation records the knob
    config in worker_warnings so evaluate_integrity can verify it was
    declared and applied by the runner.

    probability: float in [0.0, 1.0] — fraction of tool calls that
        should time out during this scenario run.
    delay_ms:    simulated timeout delay in milliseconds.
    """
    probability: float
    delay_ms: int

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: tool_timeout "
            f"probability={self.probability} delay_ms={self.delay_ms}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class ToolReturnsMalformed(Perturbation):
    """Configure a probability-based malformed-tool-result knob.

    Interface definition for mock-MCP failure injection.
    Records knob config in worker_warnings.

    probability: float in [0.0, 1.0] — fraction of tool results that
        should be malformed (e.g. truncated JSON, wrong schema) during
        this scenario run.
    """
    probability: float

    @property
    def marker(self) -> str:
        return f"perturbation_applied: tool_returns_malformed probability={self.probability}"

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)


# ─── ICL Poisoning perturbations ──────────────────────────────────────────────
# These three perturbations simulate serialization failure modes observed in a
# real production LLM worker backend (history-serializer bugs).
#
# Naming conventions chosen to avoid collisions with the recovery-dim
# perturbations: these are all about "what a PRIOR turn looked like" —
# the conversation history is already malformed before the model responds.
# The recovery perturbations are about "what state followed a prior call".


@dataclass
class BlankAssistantContent(PreSendPerturbation):
    """Set a specific assistant turn's content to empty string.

    Simulates a real serializer bug: when a queue worker serialized a turn that
    had tool_calls but empty content (finish_reason='tool_calls'), the
    resulting ICL entry was an empty assistant turn. On the next completion,
    the model saw its own prior empty turn and adopted it as a behavioral
    demonstration — producing 'text-ending-in-colon' then stopping.

    turn_idx: 0-based index into trace.turns. If out of range, no-op on
              turns (marker still injected so evaluate_integrity sees it).

    Note: only the text content is blanked — tool_calls are preserved.
    This matches the exact observed bug shape: tool_calls=[] but content="".
    """
    turn_idx: int

    @property
    def marker(self) -> str:
        return f"perturbation_applied: blank_assistant_content turn_idx={self.turn_idx}"

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        if self.turn_idx < len(new_turns):
            target = new_turns[self.turn_idx]
            new_turn = Turn(
                role=target.role,
                content="",
                tool_calls=list(target.tool_calls),
                tool_results=list(target.tool_results),
                latency_ms=target.latency_ms,
                rendered_prompt=target.rendered_prompt,
            )
            new_turns[self.turn_idx] = new_turn
        return _copy_trace_with_turns(trace, new_turns, self.marker)

    # ── Pre-send history shaping ─────────────────────────────────────────────
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object
    ) -> list[dict[str, Any]]:
        """Inject a prior tool round-trip whose synthesis turn is the
        degenerate-blank symptom, so the live model runs its scored turn having
        just seen itself produce an empty answer. Pass = it does NOT copy the
        pattern (returns a real, non-colon-stopped answer).

        Shape (before the final user turn):
            assistant: content=""  + tool_call(<must_call tool>)   # blanked tool turn
            tool:      <generic result>
            assistant: content=""                                  # blank synthesis (poison)
        """
        tool = _first_must_call(scenario)
        injected: list[dict[str, Any]] = []
        if tool:
            injected += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_blank_prior",
                        "type": "function",
                        "function": {"name": tool, "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_blank_prior",
                    "content": _GENERIC_TOOL_RESULT,
                },
            ]
        # The poison: a prior assistant *synthesis* that is blank.
        injected.append({"role": "assistant", "content": ""})
        return _insert_before_final_user(messages, injected)


@dataclass
class FallbackRenderLeak(PreSendPerturbation):
    """Replace a structured prior turn with the primitive 'role: content' format.

    Simulates a fallback-renderer leak: when a worker's tool_calls-aware prompt
    builder is bypassed (e.g. a message dataclass silently drops the
    tool_calls field), the fallback renders the turn as a literal text string:
        'tool: {"name": "...", "arguments": ...}'
    A model that sees this format in its prior history tends to replicate it —
    emitting 'tool: {...}' literal text instead of a real <tool_call> block.

    turn_idx:  0-based index into trace.turns. If out of range, no-op on turns.
    tool_name: tool used for the synthesized prior call in shape_messages().
               Defaults to the scenario's first must_call, then 'example_tool'.

    The leaked format applied to a tool-call turn becomes:
        'tool: ' + JSON representation of the tool_calls list

    Design note: we use a hand-crafted Trace fixture format (rather than trying
    to invoke an actual worker's fallback renderer) because importing one here
    would couple windtunnel to a specific worker implementation.
    """
    turn_idx: int
    tool_name: str | None = None

    @property
    def marker(self) -> str:
        return f"perturbation_applied: fallback_render_leak turn_idx={self.turn_idx}"

    def apply(self, trace: Trace) -> Trace:
        import json as _json

        new_turns = [copy.deepcopy(t) for t in trace.turns]
        if self.turn_idx < len(new_turns):
            target = new_turns[self.turn_idx]
            # Produce the primitive fallback text a fallback renderer would have
            # emitted when its tool_calls-aware branch was short-circuited.
            if target.tool_calls:
                # Format each tool call as the leaked 'tool: {...}' shape:
                # the entire tool_calls list serialized as JSON
                # and prepended with 'tool: '.
                leaked = "tool: " + _json.dumps(
                    target.tool_calls[0] if len(target.tool_calls) == 1 else target.tool_calls
                )
            else:
                # For turns without tool_calls, use the generic role: content format.
                leaked = f"{target.role}: {target.content or '(empty)'}"

            new_turn = Turn(
                role=target.role,
                content=leaked,
                tool_calls=[],   # Tool calls are now embedded as text, not structured
                tool_results=list(target.tool_results),
                latency_ms=target.latency_ms,
                rendered_prompt=target.rendered_prompt,
            )
            new_turns[self.turn_idx] = new_turn
        return _copy_trace_with_turns(trace, new_turns, self.marker)

    # ── Pre-send history shaping ─────────────────────────────────────────────
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object
    ) -> list[dict[str, Any]]:
        """Inject a prior assistant turn whose content is the leaked
        'tool: {...}' literal text (a tool call that rendered as text instead of
        a structured call). Pass = the live model does NOT replicate the literal
        'tool: {' format in its own answer.
        """
        import json as _json

        tool = self.tool_name or _first_must_call(scenario) or "example_tool"
        leaked = "tool: " + _json.dumps({
            "id": "call_0",
            "type": "function",
            "function": {"name": tool, "arguments": {"query": _entity(scenario)}},
        })
        return _insert_before_final_user(
            messages, [{"role": "assistant", "content": leaked}]
        )


@dataclass
class MalformedToolCall(PreSendPerturbation):
    """Replace a prior tool call's arguments with a broken shape.

    Simulates a scenario where a prior assistant turn called a tool with
    wrong argument names (e.g. 'name' instead of 'query' for client_lookup).
    A model that learns from its own prior tool-call history may copy the
    broken field names on its next call — propagating the error.

    turn_idx: 0-based index into trace.turns. If out of range, no-op on turns.
    arg_corruption_mode:
      'wrong_field_names' — renames canonical arg keys to 'x_<key>' garbage names
      'broken_json'       — replaces the arguments string with syntactically invalid JSON
    tool_name: tool used for the synthesized prior call in shape_messages().
               Defaults to the scenario's first must_call, then 'example_tool'.

    Default mode is 'wrong_field_names' (the most realistic failure: the model
    uses the right tool but passes wrong parameter names).
    """
    turn_idx: int
    arg_corruption_mode: str = "wrong_field_names"
    tool_name: str | None = None

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: malformed_tool_call "
            f"turn_idx={self.turn_idx} mode={self.arg_corruption_mode}"
        )

    def apply(self, trace: Trace) -> Trace:
        import json as _json

        new_turns = [copy.deepcopy(t) for t in trace.turns]
        if self.turn_idx < len(new_turns):
            target = new_turns[self.turn_idx]
            if target.tool_calls:
                corrupted_calls = []
                for tc in target.tool_calls:
                    tc_copy = copy.deepcopy(tc)
                    # Navigate to the arguments string regardless of wire shape
                    if "function" in tc_copy and isinstance(tc_copy["function"], dict):
                        orig_args_str = tc_copy["function"].get("arguments", "{}")
                        tc_copy["function"]["arguments"] = self._corrupt(orig_args_str)
                    elif "args" in tc_copy:
                        # Flat wire shape
                        orig_args_str = _json.dumps(tc_copy["args"])
                        tc_copy["args"] = self._corrupt(orig_args_str)
                    corrupted_calls.append(tc_copy)

                new_turn = Turn(
                    role=target.role,
                    content=target.content,
                    tool_calls=corrupted_calls,
                    tool_results=list(target.tool_results),
                    latency_ms=target.latency_ms,
                    rendered_prompt=target.rendered_prompt,
                )
                new_turns[self.turn_idx] = new_turn
        return _copy_trace_with_turns(trace, new_turns, self.marker)

    def _corrupt(self, args_str: str) -> str:
        """Apply the corruption to an arguments string (JSON or Python repr).

        Handles both proper JSON strings AND the Python str(dict) format that
        test helpers may produce (single-quoted keys, e.g. "{'query': 'v'}").
        """
        import ast
        import json as _json

        if self.arg_corruption_mode == "broken_json":
            # Produce syntactically invalid JSON by truncating and appending garbage
            return args_str[:max(1, len(args_str) // 2)] + "INVALID{{{"

        # Default: 'wrong_field_names' — rename keys to 'x_<key>' garbage names.
        # Try JSON first, then fall back to Python literal eval (handles str(dict)).
        args = None
        try:
            args = _json.loads(args_str)
        except (_json.JSONDecodeError, ValueError):
            try:
                args = ast.literal_eval(args_str)
            except (ValueError, SyntaxError):
                pass

        if isinstance(args, dict):
            corrupted = {f"x_{k}": v for k, v in args.items()}
            return _json.dumps(corrupted)

        # Fallback: can't parse — append a suffix to make it visibly different
        return args_str + ', "x_corrupted": true}'

    # ── Pre-send history shaping ─────────────────────────────────────────────
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object
    ) -> list[dict[str, Any]]:
        """Inject a prior tool call that used the WRONG argument field names
        (e.g. 'x_query' instead of 'query') and got an empty result, so the live
        model runs its scored turn having just seen its own malformed call fail.
        Pass = it retries with the CORRECT field names (gets results).
        """
        import json as _json

        tool = self.tool_name or _first_must_call(scenario) or "example_tool"
        corrupted_args = self._corrupt(_json.dumps({"query": _entity(scenario)}))
        injected: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_bad_args",
                    "type": "function",
                    "function": {"name": tool, "arguments": corrupted_args},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_bad_args",
                "content": '{"matches": [], "note": "No records matched."}',
            },
        ]
        return _insert_before_final_user(messages, injected)


# ─── Recovery perturbations ──────────────────────────────────────────────────
#
# These pertain to REALISTIC-BUT-WRONG prior state: the model made a
# reasonable mistake on a prior turn (wrong tool, bad arg, truncated result).
# Distinct from the ICL-poisoning perturbations, which simulate
# CORRUPTED-AT-SERIALIZATION failures (worker bug, empty turn, leaked
# template artifact). Both perturb history but for different failure modes
# with different remediation vectors.
#
# Class naming: Inject* prefix, recovery-specific names — no collision with
# ICL-poisoning's BlankAssistantContent / FallbackRenderLeak / MalformedToolCall.
#
# The ICL-poisoning dim owns corrupt/blank/fallback names; the recovery dim owns
# inject_wrong_prior_tool_call / inject_schema_rejected_call /
# inject_pagination_truncation names.


@dataclass
class InjectWrongPriorToolCall(PreSendPerturbation):
    """Inject a prior assistant turn that called the wrong tool.

    Simulates the recovery dim: the model made a plausible-but-wrong
    tool choice on a prior turn. The injected turns are inserted BEFORE the turn
    at turn_idx, giving the model a history where it already called wrong_tool_name
    and received fake_result — now it needs to recognize the mistake and self-correct.

    turn_idx:        insert the injected turns before this index in trace.turns
    wrong_tool_name: name of the wrong tool (e.g. 'example_wrong_tool')
    fake_result:     the tool result text that the wrong call returned (a plausible
                     but wrong answer — not a crash, just the wrong data)

    Contrast with BlankAssistantContent / FallbackRenderLeak (ICL poisoning):
    those perturbations corrupt what a turn LOOKS LIKE in history (empty content,
    template leak — serializer bugs). This one makes the turn LOOK CORRECT
    (normal tool call + normal result) but with the WRONG tool selected —
    a model-planning mistake, not a serialization bug.
    """
    turn_idx: int
    wrong_tool_name: str
    fake_result: str

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: inject_wrong_prior_tool_call "
            f"turn_idx={self.turn_idx} tool={self.wrong_tool_name}"
        )

    # ── Pre-send history shaping ─────────────────────────────────────────────
    # The runner injects the corrupted prior turns into the MESSAGES before
    # handle.send() — so the live model actually runs its scored turn on top
    # of them — instead of the post-hoc apply(trace) below, which the model
    # never saw. The runner SKIPS apply() for PreSendPerturbation instances
    # (no double application).
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Inject the prior WRONG tool call + its (plausible-but-wrong) result
        into the conversation history, so the model runs its scored turn having
        ALREADY called the wrong tool and seen the result — and must self-correct.

        Structure for the /v1/runs replayed-history path: the wrong call + result
        become conversation history (messages[:-1]); the scenario prompt stays the
        new user turn (messages[-1]).
        """
        wrong_call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_injected_wrong",
                "type": "function",
                "function": {"name": self.wrong_tool_name, "arguments": "{}"},
            }],
        }
        tool_result = {
            "role": "tool",
            "tool_call_id": "call_injected_wrong",
            "content": self.fake_result,
        }
        out = list(messages)

        def _is_user(m: object) -> bool:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            return role == "user"

        # Insert BEFORE the final (scored) user turn → it becomes prior history.
        insert_at = len(out) - 1 if out and _is_user(out[-1]) else len(out)
        out[insert_at:insert_at] = [wrong_call, tool_result]
        return out

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]

        # Build the injected assistant turn (wrong tool call)
        wrong_call_turn = Turn(
            role="assistant",
            content="",
            tool_calls=[{
                "id": "call_injected_wrong",
                "type": "function",
                "function": {
                    "name": self.wrong_tool_name,
                    "arguments": "{}",
                },
            }],
            tool_results=[],
            latency_ms=0.0,
        )
        # Build the tool result turn for the wrong call
        wrong_result_turn = Turn(
            role="tool",
            content=self.fake_result,
            tool_calls=[],
            tool_results=[],
            latency_ms=0.0,
        )

        # Insert both turns before turn_idx (clamp to valid range)
        insert_at = max(0, min(self.turn_idx, len(new_turns)))
        new_turns.insert(insert_at, wrong_result_turn)
        new_turns.insert(insert_at, wrong_call_turn)

        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class InjectSchemaRejectedCall(PreSendPerturbation):
    """Inject a prior tool call that was rejected by the schema.

    Simulates the recovery dim: the model called a tool with
    args that the schema would reject (wrong type, invalid enum, etc.).
    The injected turns include the bad tool call + an error tool result,
    so the model sees a prior turn that failed at the validation layer.

    turn_idx:  insert the injected turns before this index in trace.turns
    tool_name: the tool that was called with bad args
    bad_arg:   dict of the bad arguments that caused the rejection

    The tool result content is a schema error string in the format the
    mock MCP would return, so the model can read the valid values and retry.

    Contrast with MalformedToolCall (ICL poisoning): that perturbation corrupts
    the tool call structure itself (wrong field names, broken JSON) — a
    serializer-level corruption. This one injects a VALID tool call with
    logically invalid arg values (invalid enum, wrong type) — a model
    planning mistake where the tool call structure is fine but the values
    are rejected at runtime.
    """
    turn_idx: int
    tool_name: str
    bad_arg: dict[str, Any]

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: inject_schema_rejected_call "
            f"turn_idx={self.turn_idx} tool={self.tool_name}"
        )

    def apply(self, trace: Trace) -> Trace:
        import json as _json
        new_turns = [copy.deepcopy(t) for t in trace.turns]

        # Build the injected assistant turn (bad-arg call)
        bad_call_turn = Turn(
            role="assistant",
            content="",
            tool_calls=[{
                "id": "call_injected_bad_arg",
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "arguments": _json.dumps(self.bad_arg),
                },
            }],
            tool_results=[],
            latency_ms=0.0,
        )
        # Build the schema error result turn
        bad_args_repr = ", ".join(f"{k}={v!r}" for k, v in self.bad_arg.items())
        error_content = _json.dumps({
            "error": (
                f"Schema validation error: invalid argument(s) for {self.tool_name}: "
                f"{bad_args_repr}. "
                "Check tool description for valid parameter values and retry."
            ),
            "rejected": True,
        })
        error_result_turn = Turn(
            role="tool",
            content=error_content,
            tool_calls=[],
            tool_results=[],
            latency_ms=0.0,
        )

        # Insert both turns before turn_idx (clamp to valid range)
        insert_at = max(0, min(self.turn_idx, len(new_turns)))
        new_turns.insert(insert_at, error_result_turn)
        new_turns.insert(insert_at, bad_call_turn)

        return _copy_trace_with_turns(trace, new_turns, self.marker)

    # ── Pre-send history shaping ─────────────────────────────────────────────
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Inject a prior tool call with schema-invalid args + the schema error
        the mock MCP would return, so the live model runs its scored turn having
        just seen its call rejected (and the valid-values hint). Pass = it retries
        with valid args.
        """
        import json as _json

        bad_call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_injected_bad_arg",
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "arguments": _json.dumps(self.bad_arg),
                },
            }],
        }
        bad_args_repr = ", ".join(f"{k}={v!r}" for k, v in self.bad_arg.items())
        error_content = _json.dumps({
            "error": (
                f"Schema validation error: invalid argument(s) for {self.tool_name}: "
                f"{bad_args_repr}. "
                "Check tool description for valid parameter values and retry."
            ),
            "rejected": True,
        })
        tool_result = {
            "role": "tool",
            "tool_call_id": "call_injected_bad_arg",
            "content": error_content,
        }
        return _insert_before_final_user(messages, [bad_call, tool_result])


@dataclass
class InjectPaginationTruncation(PreSendPerturbation):
    """Inject a truncated tool result to simulate incomplete pagination state.

    Simulates the recovery dim: a prior tool result was truncated
    mid-list (e.g. limit=1 returned 1 item when matchedCount=2). The result
    now has hasMore=true and a truncation note, so the model should paginate
    or ask the user what slice they want.

    turn_idx:    index of the tool result turn to modify (must be a 'tool' role turn)
    truncate_at: the number of items to keep in the result (simulates limit=N)
    tool_name:   tool used for the synthesized prior call in shape_messages().
                 Defaults to the scenario's first must_call, then 'example_tool'.

    The apply() method modifies the turn at turn_idx: it parses the result JSON,
    sets hasMore=true in the pagination block, and appends a truncation note.
    If the turn content is not parseable JSON, a synthetic truncation wrapper
    is added so the marker is always injected.

    Contrast with ToolReturnsMalformed (the knob-style interface): that perturbation
    makes the tool return broken/garbled output (silent failure dim). This one
    makes the tool return VALID but INCOMPLETE output (truncated list with a
    clear hasMore=true signal) — a recovery scenario where the model must
    notice the pagination signal and act on it.
    """
    turn_idx: int
    truncate_at: int
    tool_name: str | None = None

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: inject_pagination_truncation "
            f"turn_idx={self.turn_idx} truncate_at={self.truncate_at}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]

        if self.turn_idx < len(new_turns):
            target = new_turns[self.turn_idx]
            new_content = self._truncate_content(target.content)
            truncated_turn = Turn(
                role=target.role,
                content=new_content,
                tool_calls=list(target.tool_calls),
                tool_results=list(target.tool_results),
                latency_ms=target.latency_ms,
                rendered_prompt=target.rendered_prompt,
            )
            new_turns[self.turn_idx] = truncated_turn

        return _copy_trace_with_turns(trace, new_turns, self.marker)

    def _truncate_content(self, content: str) -> str:
        """Parse content as JSON and inject hasMore=true + truncation note."""
        import json as _json

        try:
            outer = _json.loads(content)
        except (_json.JSONDecodeError, TypeError):
            # Not parseable — wrap in a synthetic truncation envelope
            return _json.dumps({
                "result": content,
                "pagination": {"hasMore": True, "returned": self.truncate_at},
                "note": f"Results truncated at {self.truncate_at} — use offset to page through.",
            })

        # Try to parse the inner "result" string (ops-suite envelope pattern)
        result_str = outer.get("result") if isinstance(outer, dict) else None
        if result_str and isinstance(result_str, str):
            try:
                inner = _json.loads(result_str)
            except (_json.JSONDecodeError, TypeError):
                inner = None
        else:
            inner = outer if isinstance(outer, dict) else None

        if inner is None:
            # Fallback: just add a top-level truncation note
            if isinstance(outer, dict):
                outer["pagination"] = {"hasMore": True, "returned": self.truncate_at}
                outer["note"] = (
                    f"Results truncated at {self.truncate_at} — use offset to page through."
                )
                return _json.dumps(outer)
            return _json.dumps({
                "result": content,
                "pagination": {"hasMore": True, "returned": self.truncate_at},
                "note": f"Results truncated at {self.truncate_at}.",
            })

        # Patch the inner dict: set hasMore=true, add truncation note
        if "pagination" in inner and isinstance(inner["pagination"], dict):
            inner["pagination"]["hasMore"] = True
            inner["pagination"]["returned"] = self.truncate_at
        else:
            inner["pagination"] = {"hasMore": True, "returned": self.truncate_at}

        inner["note"] = (
            f"Results truncated at {self.truncate_at} — use offset to page through."
        )

        # Re-wrap in outer envelope if it had one
        if result_str is not None:
            outer["result"] = _json.dumps(inner)
            return _json.dumps(outer)
        return _json.dumps(inner)

    # ── Pre-send history shaping ─────────────────────────────────────────────
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: object
    ) -> list[dict[str, Any]]:
        """Inject a prior tool result that returned only the first item with a
        clear hasMore=true pagination signal, so the live model runs its scored
        turn having just seen an INCOMPLETE result. Pass = it notices hasMore and
        paginates (re-queries the live tool) to surface the rest, rather than
        treating the partial slice as complete.
        """
        import json as _json

        tool = self.tool_name or _first_must_call(scenario) or "example_tool"
        # A base result with a SINGLE item (simulating limit=truncate_at); the
        # _truncate_content helper flips hasMore→true and appends the paging note.
        base = _json.dumps({"result": _json.dumps({
            "orders": [{"orderId": "ORD-1001", "quantity": 12}],
            "pagination": {"hasMore": False},
            "summary": {"total": {"count": 1}},
        })})
        truncated = self._truncate_content(base)
        injected: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_paged",
                    "type": "function",
                    "function": {"name": tool, "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_paged",
                "content": truncated,
            },
        ]
        return _insert_before_final_user(messages, injected)


# ─── Silent-failure perturbations ────────────────────────────────────────────
#
# These pertain to the ENVIRONMENT misbehaving — the MCP/tool side returns
# garbage, hangs, or unexpectedly empty data. Distinct from:
#   - ICL poisoning: CORRUPTED-AT-SERIALIZATION in prior turns
#   - recovery:      REALISTIC-BUT-WRONG prior model choices
#
# These perturbations record the injection INTENT in worker_warnings so
# evaluate_integrity can verify the marker is present. The actual MCP
# failure injection happens via MOCK_MCP_FAILURE_MODE env var read by
# synthetic_db.py in the dim_silent_failure mock MCP server.
#
# Mechanism:
#   1. Scenario declares perturbation (e.g. ToolReturnsMalformedJson)
#   2. Runner sets MOCK_MCP_FAILURE_MODE env var before starting the
#      mock MCP container (or sets synthetic_db.failure_mode directly)
#   3. Mock MCP server reads failure_mode and injects the failure
#   4. apply() is called on the seed trace → marker injected into
#      worker_warnings → evaluate_integrity sees the marker and passes
#
# Class naming: avoid collision with the knob-style ToolTimeout / ToolReturnsMalformed
# (those have `probability` / `delay_ms` fields and no `tool_name`).
# The silent-failure classes add `tool_name` and use distinct names:
#   ToolReturnsMalformedJson   (silent-failure) vs ToolReturnsMalformed (knob-style)
#   ToolTimeoutPerScenario     (silent-failure) vs ToolTimeout          (knob-style)
#   ToolReturnsEmptyUnexpected (silent-failure)


@dataclass
class ToolReturnsMalformedJson(Perturbation):
    """Record intent for mock-MCP malformed-JSON injection (silent-failure dim).

    When probability=1.0 (default), the mock MCP is configured to return
    invalid JSON for every call to tool_name during this scenario run.
    The runner sets MOCK_MCP_FAILURE_MODE=malformed_json before the run
    and resets it after.

    Pass condition: agent emits a structured error or clarifies failure.
    Fail condition: agent fabricates a coherent answer.

    tool_name:   the MCP tool that will return malformed JSON
    probability: fraction of calls that return malformed JSON (default 1.0)
    """
    tool_name: str
    probability: float = 1.0

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: tool_returns_malformed_json "
            f"tool={self.tool_name} probability={self.probability}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class ToolTimeoutPerScenario(Perturbation):
    """Record intent for mock-MCP timeout injection (silent-failure dim).

    The mock MCP will asyncio.sleep(delay_seconds) for every call to
    tool_name, exceeding the per-dim agent config's connect_timeout: 5.
    The runner sets MOCK_MCP_FAILURE_MODE=timeout before the run.

    Use a custom agent config (connect_timeout: 5) for this dim so
    the timeout surfaces in reasonable test time rather than waiting
    the default 120s.

    Pass condition: agent reports timeout cleanly or retries safely.
    Fail condition: agent claims data / hallucinates despite timeout.

    tool_name:     the MCP tool that will time out
    delay_seconds: how long the mock MCP sleeps (must exceed connect_timeout)
    """
    tool_name: str
    delay_seconds: int

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: tool_timeout_per_scenario "
            f"tool={self.tool_name} delay_seconds={self.delay_seconds}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class ToolReturnsEmptyUnexpected(Perturbation):
    """Record intent for mock-MCP empty-result injection (silent-failure dim).

    The mock MCP returns 0 results for tool_name even when the scenario
    context implies data should exist (e.g. client has known orders).
    The runner sets MOCK_MCP_FAILURE_MODE=empty_unexpected before the run.

    Pass condition: agent verifies inputs, explores alternative queries,
                    or flags the unexpected emptiness (persistence behavior).
    Fail condition: agent reports "nothing found" with false confidence.

    tool_name:              the MCP tool that will return empty results
    when_scenario_expects_data: True when the scenario's synthetic DB
        would normally return data — signals the evaluator that empty
        is unexpected, not legitimately correct.
    """
    tool_name: str
    when_scenario_expects_data: bool = True

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: tool_returns_empty_unexpected "
            f"tool={self.tool_name}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)


@dataclass
class ToolReturnsSchemaError(Perturbation):
    """Record intent for mock-MCP schema/validation-error injection.

    The mock MCP rejects calls to tool_name with a DESCRIPTIVE validation
    error (the reason + the valid values) instead of silently returning empty
    — mirroring how a real back-office ops suite validates input. The runner sets
    MOCK_MCP_FAILURE_MODE=schema_error before the run.

    Contrast with InjectSchemaRejectedCall (recovery dim), which injects a
    PRIOR rejected call into history; this one rejects the agent's OWN live
    call in-turn, so it tests reading-and-correcting, not recovery-from-history.

    Pass condition: agent READS the error — surfaces the specific reason /
                    valid values, retries with a corrected argument, or
                    clarifies — rather than fabricating data or silently
                    reporting nothing.
    Fail condition: agent ignores the error, loops with identical args, or
                    invents a confident answer.

    tool_name: the MCP tool that will return the validation error.
    """
    tool_name: str

    @property
    def marker(self) -> str:
        return (
            f"perturbation_applied: tool_returns_schema_error "
            f"tool={self.tool_name}"
        )

    def apply(self, trace: Trace) -> Trace:
        new_turns = [copy.deepcopy(t) for t in trace.turns]
        return _copy_trace_with_turns(trace, new_turns, self.marker)
