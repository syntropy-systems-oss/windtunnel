"""Shared trace-matching primitives used by evaluators, scorers, and packs.

These helpers used to live as private functions in ``api.evaluators`` even
though outcome scorers and built-in scenario packs also depended on them.
Keeping the mechanics here makes that ownership explicit while
``api.evaluators`` continues to expose its historical module attributes for
compatibility.
"""
from __future__ import annotations

import re

from windtunnel.api._evidence import mcp_evidence_state
from windtunnel.api.scenario import NumberFact
from windtunnel.api.trace import Trace, Turn


def extract_tool_names(trace: Trace) -> list[str]:
    """Return the ordered tool names claimed by the transcript."""
    names: list[str] = []
    for turn in trace.turns:
        for tool_call in turn.tool_calls:
            if "function" in tool_call and isinstance(tool_call["function"], dict):
                name = tool_call["function"].get("name")
            elif "name" in tool_call:
                name = tool_call["name"]
            else:
                name = None
            if name:
                names.append(str(name))
    return names


def extract_server_tool_names(trace: Trace) -> list[str]:
    """Return server-witnessed tool names in chronological order."""
    ordered = sorted(trace.mcp_calls, key=lambda call: call.get("timestamp_ms") or 0.0)
    return [str(call["tool_name"]) for call in ordered if call.get("tool_name")]


def last_assistant_turn(trace: Trace) -> Turn | None:
    """Return the actual last assistant turn, including empty-content turns."""
    for turn in reversed(trace.turns):
        if turn.role == "assistant":
            return turn
    return None


def has_tool_calls(trace: Trace) -> bool:
    """Return tool-use truth from the strongest available evidence."""
    state = mcp_evidence_state(trace.worker_warnings)
    if state == "unavailable":
        return False
    if state == "available" or trace.mcp_calls:
        return bool(trace.mcp_calls)
    return any(turn.tool_calls for turn in trace.turns)


def match_number_fact(answer: str, fact: NumberFact) -> bool:
    """Match a numeric fact with word boundaries and optional unit proximity."""
    pattern = rf"\b{re.escape(str(fact.value))}\b"
    match = re.search(pattern, answer)
    if not match:
        return False
    if fact.unit is None:
        return True
    window_start = max(0, match.start() - 30)
    window_end = min(len(answer), match.end() + 30)
    unit_pattern = rf"\b{re.escape(fact.unit)}\b"
    return bool(re.search(unit_pattern, answer[window_start:window_end], re.IGNORECASE))


def match_fact_group(text: str, group: list[str]) -> bool:
    """Return whether any member of an AND-of-OR fact group appears in text."""
    text_lower = text.lower()
    return any(fact.lower() in text_lower for fact in group)


def tool_name_matches(canonical: str, full: str) -> bool:
    """Match a canonical tool name against an optionally decorated name."""
    return (
        full == canonical
        or full.endswith("_" + canonical)
        or full.endswith("." + canonical)
    )


def any_tool_name_matches(canonical: str, full_names: list[str]) -> bool:
    """Return whether any observed name matches the canonical tool name."""
    return any(tool_name_matches(canonical, full) for full in full_names)
