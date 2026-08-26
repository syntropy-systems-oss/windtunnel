"""Shared trace-matching primitives used by evaluators, scorers, and packs.

These helpers used to live as private functions in ``api.evaluators`` even
though outcome scorers and built-in scenario packs also depended on them.
Keeping the mechanics here makes that ownership explicit while
``api.evaluators`` continues to expose its historical module attributes for
compatibility.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from windtunnel.api._evidence import mcp_evidence_state
from windtunnel.api.scenario import NumberFact
from windtunnel.api.trace import Trace, Turn


@dataclass(frozen=True)
class TextSpan:
    """Half-open ``[start, end)`` character offsets into a matched text.

    Span-returning matcher variants exist so evidence surfaces (the run
    viewer) can show *where* a fact matched, under one design law: a span
    variant finds at least one span **iff** its boolean counterpart returns
    True. The boolean matchers stay the scoring authority; spans only ever
    decorate their verdict, never diverge from it.

    Case-insensitive matchers compute on ``str.lower()`` of both sides, the
    exact operation the boolean matchers use. For the rare Unicode texts
    where lowering changes string length the boolean equivalence still
    holds, but offsets then index the lowered text and may drift against
    the original — a display caveat, never a verdict one.
    """

    start: int
    end: int


def find_fact_spans(text: str, fact: str) -> list[TextSpan]:
    """Return every case-insensitive occurrence of ``fact`` in ``text``.

    Non-empty iff ``fact.lower() in text.lower()`` — the exact membership
    test ``match_fact_group`` applies per group member.
    """
    text_lower = text.lower()
    fact_lower = fact.lower()
    if not fact_lower:
        return []
    spans: list[TextSpan] = []
    start = 0
    while True:
        index = text_lower.find(fact_lower, start)
        if index == -1:
            return spans
        spans.append(TextSpan(start=index, end=index + len(fact_lower)))
        start = index + len(fact_lower)


def match_fact_group_spans(text: str, group: list[str]) -> list[tuple[str, TextSpan]]:
    """Span variant of match_fact_group: every matching member's occurrences.

    Non-empty iff ``match_fact_group(text, group)`` is True (any member of
    the AND-of-OR group appears in the text).
    """
    return [(fact, span) for fact in group for span in find_fact_spans(text, fact)]


def match_number_fact_span(answer: str, fact: NumberFact) -> TextSpan | None:
    """Span variant of match_number_fact — same regex, same unit window.

    Returns the span of the first word-boundary occurrence of the value
    exactly when ``match_number_fact(answer, fact)`` is True: like the
    boolean matcher, only the FIRST occurrence's ±30-character window is
    checked for the unit, so a later occurrence near the unit does not
    rescue a first occurrence that lacks it.
    """
    pattern = rf"\b{re.escape(str(fact.value))}\b"
    match = re.search(pattern, answer)
    if not match:
        return None
    span = TextSpan(start=match.start(), end=match.end())
    if fact.unit is None:
        return span
    window_start = max(0, match.start() - 30)
    window_end = min(len(answer), match.end() + 30)
    unit_pattern = rf"\b{re.escape(fact.unit)}\b"
    if re.search(unit_pattern, answer[window_start:window_end], re.IGNORECASE):
        return span
    return None


def find_forbidden_assertion_spans(
    text: str, forbidden: list[str], cues: Sequence[str]
) -> list[tuple[str, TextSpan]]:
    """Every ASSERTED (non-negated) forbidden-fact occurrence, with spans.

    This is the single implementation of the negation-aware forbidden gate:
    ``evaluators.has_any_forbidden`` is ``bool()`` of this scan, so the
    boolean gate and the evidence spans cannot drift. See has_any_forbidden
    for the semantics (word boundaries for bare numbers and single
    identifiers, before-window clipped at the last clause boundary,
    after-window clipped at the first sentence boundary).

    Offsets index ``text.lower()`` — see the TextSpan docstring caveat.
    """
    t = text.lower()
    assertions: list[tuple[str, TextSpan]] = []
    for fact in forbidden:
        f = fact.lower()
        if not f:
            continue
        is_bare_number = f.strip().isdigit()
        is_single_identifier = bool(re.fullmatch(r"[a-z_][a-z0-9_]*", f.strip()))
        use_word_boundary = is_bare_number or is_single_identifier
        start = 0
        while True:
            if use_word_boundary:
                m = re.search(rf"\b{re.escape(f)}\b", t[start:])
                if m is None:
                    break
                idx = start + m.start()
            else:
                idx = t.find(f, start)
                if idx == -1:
                    break
            # Clip the BEFORE window at the LAST sentence/clause boundary so a
            # negation in a PRIOR sentence/clause doesn't spuriously excuse
            # this occurrence.
            before_raw = t[max(0, idx - 30):idx]
            before = re.split(r"[.!?;\n]", before_raw)[-1]
            # Clip after-window at the first sentence/clause end so a negation
            # in a later sentence doesn't spuriously excuse this occurrence.
            after_raw = t[idx + len(f): idx + len(f) + 40]
            after = re.split(r"[.!?\n]", after_raw, maxsplit=1)[0]
            if not any(cue in (before + " " + after) for cue in cues):
                assertions.append((fact, TextSpan(start=idx, end=idx + len(f))))
            start = idx + len(f)
    return assertions


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
