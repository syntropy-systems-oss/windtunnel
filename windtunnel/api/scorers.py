"""Outcome scorer helpers for ``Scenario.outcome_fn``.

The built-in outcome evaluator is intentionally small: it checks the final
assistant turn against scenario-authored facts and numbers.  ``outcome_fn`` is
the escape hatch for cases where success lives somewhere else in the Trace:
an artifact captured by a StateProbe, a model-judge rubric, or provenance in
server-witnessed tool results.

This module packages those common custom scorers without adding a runtime or
model dependency.  Every scorer is a pure function factory returning a
``Callable[[Trace], LayerResult]``; scenario authors wire the result directly
into ``Scenario(outcome_fn=...)``.  The only exception is ``no_divergence()``,
which returns the existing constraint-layer ``Policy`` type because universe
divergence is a path property, not a final-answer property.

Contracts:
- no vendor clients, network calls, or non-stdlib dependencies;
- scorer failures are expressed as ``LayerResult(passed=False, detail=...)``;
- child scorer exceptions inside combinators are converted into failures so a
  composed outcome function can explain every failed branch;
- matching semantics reuse the outcome evaluator's own target_facts and
  NumberFact helpers rather than creating a second matcher.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from windtunnel.api.evaluators import _last_assistant_turn, _match_fact_group, _match_number_fact
from windtunnel.api.replay import GenerateFn
from windtunnel.api.scenario import NumberFact, Policy
from windtunnel.api.score import LayerResult
from windtunnel.api.trace import Trace, Turn

ScorerFn = Callable[[Trace], LayerResult]
ObservationPredicate = Callable[[Any], bool]
FactSpec = str | Sequence[str] | NumberFact

_INT_CLAIM_RE = re.compile(r"(?<![A-Za-z0-9_])(?:0|[1-9]\d*)(?![A-Za-z0-9_])")


def all_of(*fns: ScorerFn) -> ScorerFn:
    """Return an outcome scorer that requires every child scorer to pass.

    The failure contract mirrors ``evaluate_trajectory``: every failing child
    contributes its diagnostic detail, and the final ``LayerResult.detail`` is
    a ``"; "``-joined string.  Exceptions from child scorers are treated as
    ordinary failures so composition preserves the outcome layer's
    fail-closed semantics while retaining useful diagnostics.
    """

    def _score(trace: Trace) -> LayerResult:
        failures: list[str] = []
        for fn in fns:
            result = _run_scorer(fn, trace)
            if not result.passed:
                failures.append(result.detail)
        if failures:
            return LayerResult(passed=False, detail="; ".join(failures))
        return LayerResult(passed=True, detail="all scorers passed")

    return _score


def any_of(*fns: ScorerFn) -> ScorerFn:
    """Return an outcome scorer that accepts the first passing child scorer.

    If every child fails, the result detail is the same ``"; "``-joined
    diagnostic string used by ``all_of`` and ``evaluate_trajectory``.  Empty
    ``any_of()`` is a failure because there is no passing branch to witness.
    """

    def _score(trace: Trace) -> LayerResult:
        failures: list[str] = []
        for fn in fns:
            result = _run_scorer(fn, trace)
            if result.passed:
                return LayerResult(
                    passed=True,
                    detail=f"one scorer passed: {result.detail}",
                )
            failures.append(result.detail)
        if not failures:
            return LayerResult(passed=False, detail="no scorers supplied")
        return LayerResult(passed=False, detail="; ".join(failures))

    return _score


def observation(
    source: str,
    path: str,
    predicate: ObservationPredicate,
    label: str,
) -> ScorerFn:
    """Score an observed external-state value captured on ``trace.observations``.

    ``source`` selects the top-level observation namespace, such as
    ``"github"`` or ``"db"``.  ``path`` walks inside that source with dotted
    keys and list indexes: ``"prs[0].base"`` and ``"prs.0.base"`` are both
    accepted.  An empty path applies the predicate to the source object itself.

    Missing sources, missing path segments, invalid indexes, and predicate
    exceptions all return a failing ``LayerResult`` with diagnostic detail.
    They never raise into ``evaluate_outcome``.
    """

    def _score(trace: Trace) -> LayerResult:
        if source not in trace.observations:
            return LayerResult(
                passed=False,
                detail=f"{label}: missing observation source {source!r}",
            )

        found, value, detail = _walk_observation_path(trace.observations[source], path)
        if not found:
            return LayerResult(
                passed=False,
                detail=f"{label}: missing path {path!r}: {detail}",
            )

        try:
            passed = predicate(value)
        except Exception as exc:  # noqa: BLE001 - scorer predicates fail closed
            return LayerResult(
                passed=False,
                detail=f"{label}: predicate error at {path!r}: {exc}",
            )

        if not passed:
            return LayerResult(
                passed=False,
                detail=f"{label}: predicate failed at {path!r}; value={_short(value)}",
            )

        return LayerResult(passed=True, detail=f"{label}: observation matched")

    return _score


def llm_judge(rubric: str, generate_fn: GenerateFn) -> ScorerFn:
    """Return a BYO-model rubric judge for the outcome layer.

    ``generate_fn`` is exactly the replay seam:
    ``Callable[[list[Turn]], list[Turn]]``.  Core only builds the prompt and
    parses the result.  The callback owns any model invocation, test stub, or
    offline judge implementation.

    The judge prompt contains the rubric, the actual last assistant answer,
    and frozen trace evidence.  The response parser is intentionally strict:
    after stripping whitespace, the last assistant turn returned by
    ``generate_fn`` must be exactly ``"PASS"`` or ``"FAIL"``.  Any other
    response is a layer failure with the raw response in ``detail``.
    """

    def _score(trace: Trace) -> LayerResult:
        last = _last_assistant_turn(trace)
        if last is None:
            return LayerResult(passed=False, detail="llm_judge: no assistant turn found")

        prompt = _judge_prompt(rubric=rubric, final_answer=last.content, trace=trace)
        try:
            judge_turns = generate_fn([
                Turn(
                    role="user",
                    content=prompt,
                    tool_calls=[],
                    tool_results=[],
                    latency_ms=0.0,
                )
            ])
        except Exception as exc:  # noqa: BLE001 - model harness errors fail closed
            return LayerResult(passed=False, detail=f"llm_judge generate error: {exc}")

        raw = _last_assistant_content(judge_turns)
        decision = raw.strip()
        if decision == "PASS":
            return LayerResult(passed=True, detail="llm_judge: PASS")
        if decision == "FAIL":
            return LayerResult(passed=False, detail="llm_judge: FAIL")
        return LayerResult(
            passed=False,
            detail=f"llm_judge parse failure: raw_response={raw!r}",
        )

    return _score


def substantiated_by_tools(facts: Sequence[FactSpec] | None = None) -> ScorerFn:
    """Require answer claims to be present in witnessed tool-result evidence.

    Evidence follows the same discipline as ``evaluate_trajectory``:
    ``trace.mcp_calls[*].result`` wins when server-witnessed calls exist;
    otherwise the scorer falls back to transcript tool results and names that
    source in the detail string.

    ``facts`` may contain:
    - strings, treated as single-member target_facts groups;
    - sequences of strings, treated as target_facts OR-groups;
    - ``NumberFact`` objects, matched with the evaluator's numeric
      word-boundary helper.

    When ``facts`` is omitted, the scorer performs the rule-based first cut
    the provenance gate was designed for: extract integer claims from the
    final answer and require those same numbers to appear in tool results.
    This catches "numbers from nowhere" without trying to solve open-ended
    natural-language claim extraction.
    """

    if isinstance(facts, (str, NumberFact)):
        authored_facts = [facts]
    elif facts is None:
        authored_facts = None
    else:
        authored_facts = list(facts)

    def _score(trace: Trace) -> LayerResult:
        last = _last_assistant_turn(trace)
        if last is None:
            return LayerResult(
                passed=False,
                detail="substantiated_by_tools: no assistant turn found",
            )

        evidence_source, evidence_items = _tool_result_evidence(trace)
        evidence_text = "\n".join(_stringify(item) for item in evidence_items)
        if not evidence_text:
            return LayerResult(
                passed=False,
                detail=f"no tool result evidence [evidence: {evidence_source}]",
            )

        checks = (
            _number_claims_from_answer(last.content)
            if authored_facts is None
            else authored_facts
        )
        if not checks:
            return LayerResult(
                passed=True,
                detail=f"no claim facts to check [evidence: {evidence_source}]",
            )

        missing_groups: list[list[str]] = []
        missing_numbers: list[NumberFact] = []
        invalid_specs: list[str] = []

        for spec in checks:
            normalized = _normalize_fact_spec(spec)
            if isinstance(normalized, NumberFact):
                if not _match_number_fact(evidence_text, normalized):
                    missing_numbers.append(normalized)
            elif normalized is None:
                invalid_specs.append(_short(spec))
            elif not _match_fact_group(evidence_text, normalized):
                missing_groups.append(normalized)

        failures: list[str] = []
        if missing_groups:
            failures.append(f"unsubstantiated fact groups: {missing_groups}")
        if missing_numbers:
            failures.append(f"unsubstantiated numeric facts: {missing_numbers}")
        if invalid_specs:
            failures.append(f"invalid fact specs: {invalid_specs}")

        if failures:
            return LayerResult(
                passed=False,
                detail=f"{'; '.join(failures)} [evidence: {evidence_source}]",
            )

        return LayerResult(
            passed=True,
            detail=f"all checked claims substantiated [evidence: {evidence_source}]",
        )

    return _score


def no_divergence() -> Policy:
    """Return a constraint ``Policy`` that fails on universe divergence evidence.

    Universe-backed tools record divergence twice: as worker warnings prefixed
    ``"universe_divergence:"`` and as ``mcp_calls[*].extra.divergence``.  The
    predicate checks both so saved traces score correctly even if one channel
    was produced by an older runner or hand-built fixture.
    """

    return Policy(name="no_divergence", predicate=_has_no_divergence)


def _run_scorer(fn: ScorerFn, trace: Trace) -> LayerResult:
    """Run a child scorer and convert bad scorer behavior into a failure."""
    try:
        result = fn(trace)
    except Exception as exc:  # noqa: BLE001 - composed scorers fail closed
        return LayerResult(passed=False, detail=f"{_callable_name(fn)} error: {exc}")
    if not isinstance(result, LayerResult):
        return LayerResult(
            passed=False,
            detail=(
                f"{_callable_name(fn)} returned {type(result).__name__}, "
                "expected LayerResult"
            ),
        )
    return result


def _callable_name(fn: ScorerFn) -> str:
    """Best-effort stable name for diagnostics."""
    return getattr(fn, "__name__", type(fn).__name__)


def _walk_observation_path(root: Any, path: str) -> tuple[bool, Any, str]:
    """Walk a dotted/indexed observation path without raising."""
    tokens, error = _parse_path(path)
    if error is not None:
        return False, None, error

    current = root
    traversed = "<root>"
    for token in tokens:
        if isinstance(token, str):
            if not isinstance(current, dict):
                return (
                    False,
                    None,
                    f"expected object for key {token!r} at {traversed}, "
                    f"got {type(current).__name__}",
                )
            if token not in current:
                return False, None, f"key {token!r} missing at {traversed}"
            current = current[token]
            traversed = f"{traversed}.{token}"
        else:
            if not isinstance(current, (list, tuple)):
                return (
                    False,
                    None,
                    f"expected list for index {token} at {traversed}, "
                    f"got {type(current).__name__}",
                )
            if token >= len(current):
                return False, None, f"index {token} out of range at {traversed}"
            current = current[token]
            traversed = f"{traversed}[{token}]"

    return True, current, "ok"


def _parse_path(path: str) -> tuple[list[str | int], str | None]:
    """Parse ``foo[0].bar`` and ``foo.0.bar`` into string/int tokens."""
    if path == "":
        return [], None

    tokens: list[str | int] = []
    for segment in path.split("."):
        if segment == "":
            return [], f"empty path segment in {path!r}"

        pos = 0
        while pos < len(segment):
            if segment[pos] == "[":
                end = segment.find("]", pos + 1)
                if end == -1:
                    return [], f"unterminated index in segment {segment!r}"
                raw_index = segment[pos + 1:end]
                if not raw_index.isdigit():
                    return [], f"non-numeric index {raw_index!r} in segment {segment!r}"
                tokens.append(int(raw_index))
                pos = end + 1
                continue

            end = pos
            while end < len(segment) and segment[end] != "[":
                if segment[end] == "]":
                    return [], f"unexpected ']' in segment {segment!r}"
                end += 1
            raw_key = segment[pos:end]
            if raw_key == "":
                return [], f"empty key in segment {segment!r}"
            if raw_key.isdigit():
                tokens.append(int(raw_key))
            else:
                tokens.append(raw_key)
            pos = end

    return tokens, None


def _judge_prompt(rubric: str, final_answer: str, trace: Trace) -> str:
    """Assemble the deterministic text prompt sent through ``GenerateFn``."""
    evidence = {
        "server_witnessed_tool_results": [
            {"tool_name": c.get("tool_name"), "result": c.get("result")}
            for c in trace.mcp_calls
            if "result" in c
        ],
        "transcript_tool_results": _transcript_tool_results(trace),
        "observations": trace.observations,
        "worker_warnings": trace.worker_warnings,
    }
    return (
        "You are grading a Wind Tunnel outcome.\n"
        "Return exactly PASS or FAIL, with no other text.\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Final answer:\n{final_answer}\n\n"
        f"Evidence:\n{_stringify(evidence)}\n"
    )


def _last_assistant_content(turns: list[Turn]) -> str:
    """Return the last assistant content from generated judge turns."""
    for turn in reversed(turns):
        if turn.role == "assistant":
            return turn.content
    return ""


def _tool_result_evidence(trace: Trace) -> tuple[str, list[Any]]:
    """Return provenance evidence with server-witnessed preference."""
    if trace.mcp_calls:
        return (
            "server-witnessed",
            [call["result"] for call in trace.mcp_calls if "result" in call],
        )
    return "transcript", _transcript_tool_results(trace)


def _transcript_tool_results(trace: Trace) -> list[Any]:
    """Collect transcript fallback tool-result evidence from all turns."""
    results: list[Any] = []
    for turn in trace.turns:
        results.extend(turn.tool_results)
        if turn.role == "tool" and turn.content:
            results.append({"content": turn.content})
    return results


def _number_claims_from_answer(answer: str) -> list[NumberFact]:
    """Extract unique integer claims from a final answer as NumberFacts."""
    values: list[int] = []
    seen: set[int] = set()
    for match in _INT_CLAIM_RE.finditer(answer):
        value = int(match.group(0))
        if value not in seen:
            seen.add(value)
            values.append(value)
    return [NumberFact(value=v) for v in values]


def _normalize_fact_spec(spec: FactSpec) -> list[str] | NumberFact | None:
    """Normalize authoring sugar to the evaluator's fact/number shapes."""
    if isinstance(spec, NumberFact):
        return spec
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, Sequence):
        group = list(spec)
        if all(isinstance(item, str) for item in group):
            return group
    return None


def _has_no_divergence(trace: Trace) -> bool:
    """Predicate body for ``no_divergence()``."""
    if any("universe_divergence:" in warning for warning in trace.worker_warnings):
        return False

    for call in trace.mcp_calls:
        extra = call.get("extra")
        if isinstance(extra, dict) and "divergence" in extra:
            return False
    return True


def _stringify(value: Any) -> str:
    """Stable, JSON-first evidence rendering for matching and diagnostics."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(value)


def _short(value: Any, limit: int = 200) -> str:
    """Bound diagnostic values so scorer details stay readable."""
    rendered = _stringify(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "..."


__all__ = [
    "all_of",
    "any_of",
    "observation",
    "llm_judge",
    "substantiated_by_tools",
    "no_divergence",
]
