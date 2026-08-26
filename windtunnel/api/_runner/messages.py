"""Conversation construction and runtime-response normalization."""
from __future__ import annotations

from typing import Any

from windtunnel.api.trace import Turn


def build_messages(
    user_turns: list[str],
    assistant_responses: list[str],
) -> list[dict[str, Any]]:
    """Interleave user turns with their prior assistant responses."""
    messages: list[dict[str, Any]] = []
    for index, user_text in enumerate(user_turns):
        messages.append({"role": "user", "content": user_text})
        if index < len(assistant_responses):
            messages.append(
                {"role": "assistant", "content": assistant_responses[index]}
            )
    return messages


def extract_reply(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Extract normalized content and tool calls from an SPI response."""
    message: dict[str, Any] = {}
    choices = response.get("choices")
    if choices:
        message = choices[0].get("message") or {}
    elif isinstance(response.get("message"), dict):
        message = response["message"]
    elif "choices" not in response:
        message = response
    content: str = message.get("content") or ""
    tool_calls: list[dict[str, Any]] = message.get("tool_calls") or []
    return content, tool_calls


# Exactly the Turn fields a runtime may supply per step. Anything else in a
# step dict is a shape error and rejects the whole adoption (fall back to
# the aggregated turn) — a misspelled field silently dropped would falsify
# the per-step record.
_STEP_REQUIRED_KEYS = frozenset({"role", "content"})
_STEP_OPTIONAL_KEYS = frozenset(
    {"tool_calls", "tool_results", "latency_ms", "rendered_prompt", "error"}
)
_STEP_ROLES = frozenset({"assistant", "tool"})


def adopt_response_turns(
    response: dict[str, Any],
    reply_text: str,
    measured_latency_ms: float,
) -> tuple[list[Turn] | None, list[str]]:
    """Adopt a runtime's per-step ``response["turns"]`` into Trace turns.

    The optional enrichment channel for runtimes that can reconstruct the
    agent loop step by step: ``turns`` is a list of Turn-shaped dicts (one
    per assistant step — interstitial thought text as ``content``, that
    step's ``tool_calls``, shape-faithful ``tool_results``, optional
    ``error`` — plus optional ``role: "tool"`` result turns). It rides
    ALONGSIDE the normal reply shape, never instead of it: the flat
    message / choices content remains what the conversation history and
    reply extraction use.

    Returns ``(turns, warnings)``. ``None`` turns means "not adopted": the
    key was absent (the common case — aggregated behavior is untouched) or
    the shape failed validation, in which case a
    ``response_turns_rejected: …`` warning is emitted for the trace and
    the caller falls back to the aggregated single turn. Validation is
    fail-closed and loud, never a crash:

      - ``turns`` must be a non-empty list of dicts
      - each step carries exactly ``role``+``content`` plus any of
        ``tool_calls`` / ``tool_results`` / ``latency_ms`` /
        ``rendered_prompt`` / ``error`` — unknown or missing fields reject
      - ``role`` must be "assistant" or "tool" (user turns are the
        runner's own record, never a runtime's)
      - the LAST assistant step's ``content`` must equal the response's
        reply text — answer-turn selection scores the last assistant
        turn, so a divergent final step would change what gets scored
      - a response-level ``error`` marker lands on the last assistant
        step when that step carries none
      - when no step carries ``latency_ms``, the measured send() latency
        is recorded on the last assistant step so wall-clock totals
        survive adoption
    """
    if "turns" not in response:
        return None, []

    def reject(reason: str) -> tuple[None, list[str]]:
        return None, [
            f"response_turns_rejected: {reason}; falling back to the aggregated turn"
        ]

    raw = response["turns"]
    if not isinstance(raw, list) or not raw:
        return reject("turns must be a non-empty list")

    steps: list[Turn] = []
    any_latency = False
    for position, step in enumerate(raw):
        if not isinstance(step, dict):
            return reject(f"turns[{position}] is not an object")
        keys = set(step)
        unknown = keys - _STEP_REQUIRED_KEYS - _STEP_OPTIONAL_KEYS
        if unknown:
            return reject(f"turns[{position}] has unknown field(s): {sorted(unknown)}")
        missing = _STEP_REQUIRED_KEYS - keys
        if missing:
            return reject(f"turns[{position}] is missing field(s): {sorted(missing)}")
        role = step["role"]
        if role not in _STEP_ROLES:
            return reject(f"turns[{position}] role must be one of {sorted(_STEP_ROLES)}")
        content = step["content"]
        if not isinstance(content, str):
            return reject(f"turns[{position}] content must be a string")
        tool_calls = step.get("tool_calls", [])
        tool_results = step.get("tool_results", [])
        for field_name, value in (("tool_calls", tool_calls), ("tool_results", tool_results)):
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                return reject(f"turns[{position}] {field_name} must be a list of objects")
        latency = step.get("latency_ms", 0.0)
        if isinstance(latency, bool) or not isinstance(latency, int | float):
            return reject(f"turns[{position}] latency_ms must be a number")
        if "latency_ms" in step:
            any_latency = True
        rendered_prompt = step.get("rendered_prompt")
        if rendered_prompt is not None and not isinstance(rendered_prompt, str):
            return reject(f"turns[{position}] rendered_prompt must be a string or null")
        error = step.get("error")
        if error is not None and not isinstance(error, str):
            return reject(f"turns[{position}] error must be a string or null")
        steps.append(
            Turn(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_results=tool_results,
                latency_ms=float(latency),
                rendered_prompt=rendered_prompt,
                error=error,
            )
        )

    last_assistant = next(
        (turn for turn in reversed(steps) if turn.role == "assistant"), None
    )
    if last_assistant is None:
        return reject("turns must contain at least one assistant step")
    if last_assistant.content != reply_text:
        return reject(
            "the last assistant step's content must equal the response reply text "
            "(answer-turn selection scores the last assistant turn)"
        )

    if last_assistant.error is None:
        last_assistant.error = extract_turn_error(response)
    if not any_latency:
        last_assistant.latency_ms = measured_latency_ms
    return steps, []


def extract_turn_error(response: dict[str, Any]) -> str | None:
    """Return the runtime-reported turn error from a response, or None.

    A runtime that knows the turn failed inside its platform reports it as
    a non-empty string under ``"error"`` — on the message (flat or inside
    choices) or at the response top level — instead of smuggling error text
    into content. Anything that is not a non-empty string is honest
    absence: this is an OPTIONAL signal, and old/unaware runtimes are
    unaffected. The runner records it on Turn.error, where a scored-turn
    error makes the run INVALID (see evaluate_integrity).
    """
    message: dict[str, Any] = {}
    choices = response.get("choices")
    if choices:
        message = choices[0].get("message") or {}
    elif isinstance(response.get("message"), dict):
        message = response["message"]
    elif "choices" not in response:
        message = response
    for candidate in (message.get("error"), response.get("error")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def extract_response_worker_warnings(response: dict[str, Any]) -> list[str]:
    """Return normalized runtime-supplied warnings from a response."""
    if "worker_warnings" not in response:
        return []
    warnings = response["worker_warnings"]
    if not isinstance(warnings, list):
        return [
            "runtime_warning_shape: response worker_warnings must be a list, "
            f"got {type(warnings).__name__}",
        ]
    return [str(warning) for warning in warnings]
