"""Span-level evidence for the run viewer — recomputed, never re-guessed.

Scoring is pure over (Scenario, Trace), so the viewer can re-run the SAME
matching primitives against a saved trace and return where each expectation
was met or missed. The design law: the viewer must never disagree with the
scorer. It is enforced structurally —

  - fact/number spans come from the span variants in api/_matching.py, whose
    boolean equivalence with the scoring matchers is pinned by
    tests/test_matching_spans.py;
  - the forbidden-facts gate and its span scan are ONE algorithm
    (evaluators.has_any_forbidden delegates to the span scanner);
  - trajectory evidence uses the same evidence-source decision and
    tool_name_matches comparisons evaluate_trajectory uses;
  - opaque callables (outcome_fn, Policy predicates, custom
    TrajectoryChecks) are reported by name only — their verdicts live in
    the layer detail strings, and no span is fabricated for them.

Everything returned is plain JSON-serializable data.
"""

from __future__ import annotations

from typing import Any

from windtunnel.api._evidence import mcp_evidence_state
from windtunnel.api._matching import (
    TextSpan,
    extract_server_tool_names,
    extract_tool_names,
    find_forbidden_assertion_spans,
    has_tool_calls,
    last_assistant_turn,
    match_fact_group_spans,
    match_number_fact_span,
    tool_name_matches,
)
from windtunnel.api.evaluators import NEGATION_CUES
from windtunnel.api.scenario import PolicyVerdict, Scenario
from windtunnel.api.trace import Trace


def compute_evidence(scenario: Scenario, trace: Trace) -> dict[str, Any]:
    """Return per-layer, span-level evidence for one saved run."""
    return {
        "outcome": _outcome_evidence(scenario, trace),
        "trajectory": _trajectory_evidence(scenario, trace),
        "constraint": _constraint_evidence(scenario, trace),
        "integrity": _integrity_evidence(scenario, trace),
    }


# ─── outcome ─────────────────────────────────────────────────────────────────


def _span_dict(span: TextSpan) -> dict[str, int]:
    return {"start": span.start, "end": span.end}


def _outcome_evidence(scenario: Scenario, trace: Trace) -> dict[str, Any]:
    """Fact-group / number / forbidden-fact evidence over the scored turn.

    Spans index the last assistant turn's content — the exact text
    evaluate_outcome scores. When the scenario declares a custom outcome_fn
    the layer verdict is fully owned by that callable, so no fact evidence
    is computed (custom_outcome_fn flags it for the UI).
    """
    last = last_assistant_turn(trace)
    answer_turn_index = None
    if last is not None:
        for index in range(len(trace.turns) - 1, -1, -1):
            if trace.turns[index] is last:
                answer_turn_index = index
                break

    evidence: dict[str, Any] = {
        "answer_turn_index": answer_turn_index,
        "tool_use_required": bool(scenario.requires_tool_use),
        "tool_use_observed": has_tool_calls(trace),
        "custom_outcome_fn": scenario.outcome_fn is not None,
        "fact_groups": [],
        "numbers": [],
        "forbidden_facts": [],
    }
    if last is None or scenario.outcome_fn is not None:
        return evidence

    answer = last.content

    for group in scenario.target_facts:
        matches = match_fact_group_spans(answer, group)
        evidence["fact_groups"].append(
            {
                "group": list(group),
                "matched": bool(matches),
                "spans": [
                    {"fact": fact, "turn_index": answer_turn_index, **_span_dict(span)}
                    for fact, span in matches
                ],
            }
        )

    for number in scenario.target_numbers:
        span = match_number_fact_span(answer, number)
        evidence["numbers"].append(
            {
                "value": number.value,
                "unit": number.unit,
                "matched": span is not None,
                "span": (
                    {"turn_index": answer_turn_index, **_span_dict(span)}
                    if span is not None
                    else None
                ),
            }
        )

    if scenario.forbidden_facts:
        assertions = find_forbidden_assertion_spans(
            answer, scenario.forbidden_facts, NEGATION_CUES
        )
        asserted_spans: dict[str, list[dict[str, int]]] = {}
        for fact, span in assertions:
            asserted_spans.setdefault(fact, []).append(_span_dict(span))
        evidence["forbidden_facts"] = [
            {
                "fact": fact,
                "asserted": fact in asserted_spans,
                "spans": [
                    {"turn_index": answer_turn_index, **span}
                    for span in asserted_spans.get(fact, [])
                ],
            }
            for fact in scenario.forbidden_facts
        ]

    return evidence


# ─── trajectory ──────────────────────────────────────────────────────────────


def _must_call_alternatives(entry: str | list[str]) -> list[str]:
    """Normalise a must_call entry to its alternatives list (str = 1 alt)."""
    return entry if isinstance(entry, list) else [entry]


def _match_token_span(canonical: str, full: str) -> dict[str, Any] | None:
    """Span of the canonical name inside an observed name, or None.

    Mirrors tool_name_matches exactly: an exact match spans the whole name;
    a decorated match (``mcp_acme_ops_client_lookup``, ``ops.client_lookup``)
    spans the canonical suffix. The page marks precisely this token when a
    contract entry is illuminated — the grep hit, not the whole block.
    """
    if not tool_name_matches(canonical, full):
        return None
    start = len(full) - len(canonical)
    return {"text": canonical, "start": start, "end": len(full)}


def _map_witnessed_to_transcript(
    observed: list[str], claimed: list[str]
) -> list[dict[str, Any]]:
    """Map server-witnessed calls onto the transcript's claimed-call walk.

    Greedy in-order walk: for each witnessed call, the next not-yet-consumed
    claimed call whose name matches (in either decoration direction — the
    server log usually holds canonical bare names while the transcript may
    carry platform-decorated ones) becomes its transcript position. A
    witnessed call with no match maps to ``transcript_index: None`` — the
    explicit unmapped state for count/name divergence between what the
    server saw and what the transcript claims. Claimed calls the walk skips
    are simply never illuminated from the witnessed side.
    """
    mapping: list[dict[str, Any]] = []
    cursor = 0
    for index, witnessed_name in enumerate(observed):
        found = None
        for position in range(cursor, len(claimed)):
            claimed_name = claimed[position]
            if (
                claimed_name == witnessed_name
                or tool_name_matches(witnessed_name, claimed_name)
                or tool_name_matches(claimed_name, witnessed_name)
            ):
                found = position
                cursor = position + 1
                break
        mapping.append({"observed_index": index, "transcript_index": found})
    return mapping


def _trajectory_evidence(scenario: Scenario, trace: Trace) -> dict[str, Any]:
    """must_call / forbidden_calls evidence over the observed call list.

    Mirrors evaluate_trajectory's evidence-source decision: server-witnessed
    mcp_calls when available, transcript tool_calls as the fallback, and an
    honest "unavailable" when the runtime marked MCP evidence unusable.
    All name comparisons go through tool_name_matches, so platform-decorated
    observed names match their canonical scenario names.
    """
    state = mcp_evidence_state(trace.worker_warnings)
    if state == "unavailable":
        source = "unavailable"
        observed: list[str] = []
    elif trace.mcp_calls or state == "available":
        source = "server-witnessed"
        observed = extract_server_tool_names(trace)
    else:
        source = "transcript"
        observed = extract_tool_names(trace)

    must_call_entries: list[dict[str, Any]] = []
    for entry in scenario.must_call:
        alternatives = _must_call_alternatives(entry)
        matched_indices = [
            index
            for index, name in enumerate(observed)
            if any(tool_name_matches(alt, name) for alt in alternatives)
        ]
        must_call_entries.append(
            {
                "entry": list(entry) if isinstance(entry, list) else entry,
                "satisfied": bool(matched_indices),
                "matched_calls": matched_indices,
            }
        )

    forbidden_entries: list[dict[str, Any]] = []
    for name in scenario.forbidden_calls:
        offending = [
            index
            for index, observed_name in enumerate(observed)
            if tool_name_matches(name, observed_name)
        ]
        forbidden_entries.append(
            {
                "name": name,
                "violated": bool(offending),
                "offending_calls": offending,
            }
        )

    order_satisfied: bool | None = None
    if scenario.order_matters and scenario.must_call:
        # Same subsequence walk as the built-in _CallGroups order check.
        expected_alts = [_must_call_alternatives(entry) for entry in scenario.must_call]
        all_required = [alt for alts in expected_alts for alt in alts]
        filtered = [
            name
            for name in observed
            if any(tool_name_matches(alt, name) for alt in all_required)
        ]
        entry_index = 0
        for name in filtered:
            if entry_index < len(expected_alts) and any(
                tool_name_matches(alt, name) for alt in expected_alts[entry_index]
            ):
                entry_index += 1
        order_satisfied = entry_index >= len(expected_alts)

    # Per-observed-call annotations: which must_call entries each call
    # satisfied and which forbidden names it matched — the server-computed
    # source of truth for transcript highlighting (the page only maps these
    # indices onto DOM nodes; it never re-implements name matching). Each
    # entry also carries matched_token: the exact [start, end) span of the
    # canonical name inside the observed (possibly platform-decorated)
    # name, so the page can mark the precise token that matched rather
    # than the whole call block. Built-in trajectory checks match on names
    # only, so there are no argument-level anchors to compute.
    call_details = []
    for index, name in enumerate(observed):
        matched_entries = [
            entry_index
            for entry_index, entry in enumerate(must_call_entries)
            if index in entry["matched_calls"]
        ]
        matched_forbidden = [
            entry["name"]
            for entry in forbidden_entries
            if index in entry["offending_calls"]
        ]
        # The token that matched: a forbidden name wins (it colors the call
        # red), else the first matching must_call alternative.
        token_candidates = list(matched_forbidden)
        for entry_index in matched_entries:
            entry_value = scenario.must_call[entry_index]
            token_candidates.extend(_must_call_alternatives(entry_value))
        matched_token = None
        for canonical in token_candidates:
            matched_token = _match_token_span(canonical, name)
            if matched_token is not None:
                break
        call_details.append(
            {
                "index": index,
                "name": name,
                "must_call_entries": matched_entries,
                "forbidden": matched_forbidden,
                "matched_token": matched_token,
            }
        )

    # Witnessed -> transcript mapping: when the evidence source is the
    # server's own call log, the transcript pane still renders the trace's
    # CLAIMED calls — a different list. Map each witnessed call onto the
    # claimed walk (same enumeration order as extract_tool_names) by a
    # greedy in-order name walk, so illumination can light both surfaces.
    # A witnessed call with no matching claimed call maps to None — an
    # explicit unmapped state the page must surface, never silence. For
    # today's aggregated single-turn traces (every claimed call flattened
    # into one assistant turn) the claimed walk is simply that turn's
    # calls in stored order, so the mapping works without per-step turns.
    transcript_call_map = None
    if source == "server-witnessed":
        claimed = extract_tool_names(trace)
        transcript_call_map = _map_witnessed_to_transcript(observed, claimed)

    return {
        "evidence_source": source,
        "observed_calls": observed,
        "observed_call_details": call_details,
        "transcript_call_map": transcript_call_map,
        "must_call": must_call_entries,
        "forbidden_calls": forbidden_entries,
        "order_matters": bool(scenario.order_matters),
        "order_satisfied": order_satisfied,
        "custom_checks": [type(check).__name__ for check in scenario.trajectory_checks],
    }


# ─── constraint / integrity ──────────────────────────────────────────────────


def _constraint_evidence(scenario: Scenario, trace: Trace) -> dict[str, Any]:
    """Per-policy evidence: anchors when the policy offers them, honesty when not.

    A Policy predicate may return a PolicyVerdict carrying EvidenceAnchor
    references (api.scenario). Policies are pure predicates over the saved
    Trace, so — exactly like the fact matchers — the viewer re-runs them
    here to collect anchors. The VERDICT authority is untouched: the run's
    recorded constraint detail (the sidecar) stays the displayed truth;
    ``recomputed_passed`` is named for what it is, and a re-run that raises
    degrades that policy to the opaque (unanchored) presentation.

    Policies that return a plain bool have no anchors — the UI must present
    them as opaque ("no transcript anchor"), never as silently inert
    interactive entries.
    """
    entries: list[dict[str, Any]] = []
    for policy in scenario.policies:
        entry: dict[str, Any] = {
            "name": policy.name,
            "effect_class": policy.effect_class,
            "anchorable": False,
            "recomputed_passed": None,
            "detail": None,
            "call_anchors": [],
            "span_anchors": [],
            "locators": [],
        }
        try:
            result = policy.predicate(trace)
        except Exception as exc:  # noqa: BLE001 - degrade to opaque, keep the viewer up
            entry["detail"] = f"policy could not be re-evaluated: {type(exc).__name__}: {exc}"
            entries.append(entry)
            continue
        if isinstance(result, PolicyVerdict):
            entry["recomputed_passed"] = result.passed
            entry["detail"] = result.detail or None
            for anchor in result.anchors:
                if anchor.kind == "witnessed_call":
                    entry["call_anchors"].append(
                        {"call_index": anchor.call_index, "note": anchor.note}
                    )
                elif anchor.kind == "span":
                    entry["span_anchors"].append(
                        {
                            "turn_index": anchor.turn_index,
                            "start": anchor.start,
                            "end": anchor.end,
                            "note": anchor.note,
                        }
                    )
                else:  # locator
                    entry["locators"].append(anchor.note)
            entry["anchorable"] = bool(entry["call_anchors"] or entry["span_anchors"])
        else:
            entry["recomputed_passed"] = bool(result)
        entries.append(entry)
    return {"policies": entries}


def _integrity_evidence(scenario: Scenario, trace: Trace) -> dict[str, Any]:
    """Marker presence per declared perturbation — evaluate_integrity's check."""
    markers = []
    for perturbation in scenario.perturbations:
        marker = perturbation.marker
        warning_index = next(
            (
                index
                for index, warning in enumerate(trace.worker_warnings)
                if marker in warning
            ),
            None,
        )
        markers.append(
            {
                "type": type(perturbation).__name__,
                "marker": marker,
                "applied": warning_index is not None,
                "warning_index": warning_index,
            }
        )
    return {"markers": markers}
