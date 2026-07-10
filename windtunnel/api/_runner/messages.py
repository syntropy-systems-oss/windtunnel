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
