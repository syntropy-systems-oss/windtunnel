"""Conversation construction and runtime-response normalization."""
from __future__ import annotations

from typing import Any


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
