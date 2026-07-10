"""Four-layer evaluators — each takes a Trace + Scenario, returns LayerResult.

evaluate_outcome   — AND-of-OR facts + typed numeric matching + last-turn semantics
                     + negation-aware forbidden_facts gate
evaluate_trajectory — must_call + forbidden_calls + optional ordering,
                     compiled into TrajectoryCheck objects + custom
                     scenario.trajectory_checks (ANDed)
evaluate_constraint — predicate composition over policies
evaluate_robustness — perturbation-applied marker check

Quality bars (must not regress):

1. LAST-TURN SEMANTICS: score the ACTUAL last assistant turn, not the last
   non-empty one. If the conversation ends with a pure tool-call turn
   (content=""), that empty string IS the final answer → FAIL.
   Do NOT retroactively pull text from intermediate turns.

2. REQUIRES_TOOL_USE GATE: when scenario.requires_tool_use=True and the
   trace has no tool calls at all, the outcome is FAIL even if
   target facts happen to appear in the final answer. Closes the
   "model guessed from training" false-positive.

3. NUMERIC WORD-BOUNDARY: NumberFact matching uses \\b<value>\\b regex so
   "3" does not match "B003CCC" or "BATCH-2026". Unit proximity is
   checked when specified (within 30 chars).

4. AND-OF-OR SEMANTICS: every outer target_facts group must have at least
   one member present. If any outer group has zero members matching, the
   outcome fails regardless of other groups.

5. FORBIDDEN_FACTS GATE: scenario.forbidden_facts lists strings that must
   NOT be asserted (non-negated) in the last assistant turn. Matching is
   negation-aware via NEGATION_CUES — a forbidden term in a disclaiming
   context ("X is NOT the bug") does not trip the gate. Bare-number
   entries AND single-identifier tokens (e.g. "add", "multiply") use
   word-boundary matching so "add" does not false-fire on "additional".
   Multi-word phrases use plain substring matching. An asserted forbidden
   fact fails the outcome even when all target_facts are present.

6. CANONICAL TOOL NAMES: scenario must_call/forbidden_calls declare the
   bare tool name as the MCP server defines it (e.g. "client_lookup");
   trace tool names may be platform-decorated (e.g.
   "mcp_acme_ops_client_lookup", "ops.client_lookup"). All trajectory
   comparisons go through tool_name_matches (suffix-at-word-boundary;
   public, exported from windtunnel.api for custom TrajectoryChecks).

7. SERVER-WITNESSED TRAJECTORY: when trace.mcp_calls is non-empty (a
   logging mock MCP server was in play), trajectory evidence comes from
   the server's own call log — what actually reached the tool server —
   never the transcript's self-reported tool_calls. The transcript can
   claim calls that were never made; the server log can't be faked by
   the model. Falls back to transcript tool_calls only when mcp_calls
   is empty (e.g. the in_memory runtime, which ignores MCP servers).
   The LayerResult detail names the evidence source used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from windtunnel.api._evidence import mcp_evidence_state
from windtunnel.api._matching import (
    any_tool_name_matches as _any_tool_name_matches,
)
from windtunnel.api._matching import (
    extract_server_tool_names as _extract_server_tool_names,
)
from windtunnel.api._matching import (
    extract_tool_names as _extract_tool_names,
)
from windtunnel.api._matching import (
    has_tool_calls as _has_tool_calls,
)
from windtunnel.api._matching import (
    last_assistant_turn as _last_assistant_turn,
)
from windtunnel.api._matching import (
    match_fact_group as _match_fact_group,
)
from windtunnel.api._matching import (
    match_number_fact as _match_number_fact,
)
from windtunnel.api._matching import (
    tool_name_matches as tool_name_matches,
)
from windtunnel.api.scenario import NumberFact, Scenario, TrajectoryCheck
from windtunnel.api.score import LayerResult
from windtunnel.api.trace import Trace

# ─── Negation-aware forbidden_facts gate ─────────────────────────────────────

# Negation cues that flip a forbidden token from "false claim" to "correctly
# disclaimed". "not " also covers did/does/is/could-not; "n't" covers
# contractions; "no " covers no-match/no-such/no-client.
NEGATION_CUES: tuple[str, ...] = (
    "no ", "not ", "n't", "without", "invalid", "unknown",
    "no such", "non-existent", "nonexistent",
)


def has_any_forbidden(text: str, forbidden: list[str]) -> bool:
    """Return True if any forbidden fact is ASSERTED (non-negated) in text.

    A forbidden token in a negated/disclaiming context — e.g. "X did not
    match" or "no client X" — is correct behaviour, NOT a false claim.
    Those occurrences are skipped.  An occurrence whose surrounding clause
    contains no negation cue IS counted as an assertion.

    Matching is case-insensitive.  Bare-number facts (e.g. "3") and
    single-identifier facts (e.g. "add", "multiply", "DataPoint") use a
    word-boundary regex so they don't spuriously fire inside larger tokens
    like "B003CCC" or "additional".  Multi-word phrase facts (e.g. "Bluewing
    Logistics") use plain substring matching.

    The after-window is clipped at the first clause/sentence boundary
    ([.!?\\n]) so a negation in a LATER sentence doesn't excuse an earlier
    bare assertion.
    """
    t = text.lower()
    for fact in forbidden:
        f = fact.lower()
        # Use word-boundary regex for bare numbers AND single-identifier tokens
        # (letters/digits/underscore only, no internal spaces) so "add" does not
        # match "additional" and "multiply" does not match "multiplying".
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
            # negation in a PRIOR sentence/clause ("add is not the bug. multiply is
            # the bug" / "it is not add; multiply is wrong") doesn't spuriously
            # excuse this occurrence. Includes ';' (clause) on top of the after
            # window's sentence set — keep only the text after the last boundary.
            before_raw = t[max(0, idx - 30):idx]
            before = re.split(r"[.!?;\n]", before_raw)[-1]
            # Clip after-window at the first sentence/clause end so a negation
            # in a later sentence doesn't spuriously excuse this occurrence.
            after_raw = t[idx + len(f): idx + len(f) + 40]
            after = re.split(r"[.!?\n]", after_raw, maxsplit=1)[0]
            if not any(cue in (before + " " + after) for cue in NEGATION_CUES):
                return True  # asserted without negation → real false claim
            start = idx + len(f)
    return False


# ─── Outcome evaluator ────────────────────────────────────────────────────────

def evaluate_outcome(trace: Trace, scenario: Scenario) -> LayerResult:
    """Evaluate the outcome layer: did the user-visible task succeed?

    Algorithm:
    1. Find the actual last assistant turn (may have empty content).
    2. If requires_tool_use and no tool calls → FAIL immediately.
    3. Check AND-of-OR target_facts against the last turn's content.
    4. Check all target_numbers against the last turn's content.
    5. Check forbidden_facts: any asserted (non-negated) forbidden term → FAIL.
    6. Pass iff steps 3, 4, and 5 all pass.
    """
    last = _last_assistant_turn(trace)
    if last is None:
        return LayerResult(
            passed=False,
            detail="no assistant turn found in trace",
        )

    # requires_tool_use gate (see module-docstring quality bars)
    if scenario.requires_tool_use and not _has_tool_calls(trace):
        return LayerResult(
            passed=False,
            detail="no_tools_used: scenario requires tool use but trace has no tool calls",
        )

    # Custom outcome evaluator (e.g. artifact/observation-based scoring) fully owns
    # the outcome when provided — target_facts/target_numbers/forbidden_facts are
    # not consulted. A raised exception is scored as a failure (Policy-style), so a
    # buggy predicate can't silently pass a run.
    if scenario.outcome_fn is not None:
        try:
            return scenario.outcome_fn(trace)
        except Exception as exc:  # noqa: BLE001 — a throwing scorer must fail, not crash the sweep
            return LayerResult(passed=False, detail=f"outcome_fn error: {exc}")

    answer = last.content  # The ACTUAL last turn — may be empty

    # AND-of-OR target_facts
    missing_groups: list[int] = []
    for i, group in enumerate(scenario.target_facts):
        if not _match_fact_group(answer, group):
            missing_groups.append(i)

    if missing_groups:
        failed_groups = [scenario.target_facts[i] for i in missing_groups]
        return LayerResult(
            passed=False,
            detail=f"missing fact groups: {failed_groups}; answer[:200]={answer[:200]!r}",
        )

    # Typed numeric matching
    missing_numbers: list[NumberFact] = []
    for nf in scenario.target_numbers:
        if not _match_number_fact(answer, nf):
            missing_numbers.append(nf)

    if missing_numbers:
        return LayerResult(
            passed=False,
            detail=f"missing numeric facts: {missing_numbers}; answer[:200]={answer[:200]!r}",
        )

    # Forbidden facts gate: a hallucinated/false claim fails the
    # verdict even when all required facts are present.
    if scenario.forbidden_facts and has_any_forbidden(answer, scenario.forbidden_facts):
        return LayerResult(
            passed=False,
            detail=(
                f"forbidden fact asserted in answer; "
                f"forbidden_facts={scenario.forbidden_facts!r}; "
                f"answer[:200]={answer[:200]!r}"
            ),
        )

    return LayerResult(passed=True, detail="all facts and numbers found in last assistant turn")


# ─── Trajectory evaluator ─────────────────────────────────────────────────────

def _must_call_alternatives(entry: str | list[str]) -> list[str]:
    """Normalise a must_call entry to a list of alternatives.

    A plain str becomes a single-element list; a list[str] is returned as-is.
    This lets the evaluator treat every entry uniformly as 'at least one of
    these alternatives must appear'.
    """
    if isinstance(entry, list):
        return entry
    return [entry]


# ─── Built-in trajectory checks ───────────────────────────────────────────────
# The Scenario sugar fields (must_call / forbidden_calls / order_matters)
# compile into these TrajectoryCheck objects inside evaluate_trajectory, so
# built-in and custom checks run through ONE pipeline. They stay private:
# the sugar fields remain the authoring surface for the common cases.


@dataclass
class _ForbiddenCalls(TrajectoryCheck):
    """No observed call may match any canonical forbidden name."""

    forbidden: list[str]

    def check(self, calls: list[str]) -> tuple[bool, str]:
        found = [
            t for t in calls
            if any(tool_name_matches(f, t) for f in self.forbidden)
        ]
        if found:
            return False, f"forbidden tools called: {found}"
        return True, "no forbidden tools called"


@dataclass
class _CallGroups(TrajectoryCheck):
    """must_call presence (+ optional in-order subsequence) over alternatives groups.

    Each must_call entry is a str (single canonical name) or a list[str]
    (any-of alternatives group). Presence: every entry needs >=1 alternative
    observed. Order (order_matters=True): the entries must appear as a
    subsequence of the observed calls — other tools may interleave; for an
    alternatives group, the first matching observed call represents the
    group's position.
    """

    must_call: list[str | list[str]]
    order_matters: bool = False

    def check(self, calls: list[str]) -> tuple[bool, str]:
        # Presence — each entry requires ≥1 alternative present
        missing_entries: list[str | list[str]] = []
        for entry in self.must_call:
            alts = _must_call_alternatives(entry)
            if not any(_any_tool_name_matches(a, calls) for a in alts):
                missing_entries.append(entry)
        if missing_entries:
            return False, f"required tools not called: {missing_entries}"

        # Order (subsequence) — walk the observed calls that satisfy ANY
        # entry and match entries in declared order.
        if self.order_matters and self.must_call:
            expected_alts = [_must_call_alternatives(e) for e in self.must_call]
            all_required = [a for alts in expected_alts for a in alts]
            filtered = [
                t for t in calls
                if any(tool_name_matches(a, t) for a in all_required)
            ]

            ei = 0
            for name in filtered:
                if ei < len(expected_alts) and any(
                    tool_name_matches(a, name) for a in expected_alts[ei]
                ):
                    ei += 1
            if ei < len(expected_alts):
                return False, (
                    f"tool order violated: expected subsequence {self.must_call}, "
                    f"got {filtered}"
                )

        return True, "required tools called"


def evaluate_trajectory(trace: Trace, scenario: Scenario) -> LayerResult:
    """Evaluate the trajectory layer: right tools, right order, right path?

    The sugar fields compile into built-in TrajectoryCheck objects:
    - forbidden_calls → _ForbiddenCalls (none of these may appear)
    - must_call (+ order_matters) → _CallGroups (presence, then optional
      in-order subsequence; see _CallGroups for alternatives-group semantics)

    Custom scenario.trajectory_checks run AFTER the built-ins, over the same
    observed-call list. Checks may also override ``check_trace(trace, calls)``
    when they need command arguments or observations. The layer passes iff ALL
    checks pass; failure details are joined ("; ") with the evidence source
    appended once. Passing custom-check details are appended as annotations. A
    custom check that raises is recorded as a failure (same forgiveness as
    Policy predicates).

    Scenario tool names are CANONICAL bare names (e.g. ``client_lookup``);
    observed tool names may be platform-decorated (e.g.
    ``mcp_acme_ops_client_lookup``).  All built-in comparisons go through
    tool_name_matches (suffix-at-word-boundary); custom checks should too.

    EVIDENCE SOURCE (quality bar 7): when trace.mcp_calls is non-empty the
    observed names come from the tool server's own call log — the transcript
    can claim calls it never made; the server log can't be faked.  Two
    deliberately separate paths:

    - server-witnessed (mcp_calls non-empty): perturbation-INJECTED history
      never reaches the tool server, so the log is already free of the fake
      forbidden calls that injected turns plant in the transcript — no
      clipping or filtering is needed on this path.
    - transcript fallback (mcp_calls empty, e.g. in_memory runtime):
      unchanged legacy semantics, scoring turns[*].tool_calls as before.
    """
    evidence_state = mcp_evidence_state(trace.worker_warnings)
    if evidence_state == "unavailable":
        return LayerResult(
            passed=False,
            detail="MCP evidence unavailable; refusing transcript fallback [evidence: unavailable]",
        )
    if trace.mcp_calls or evidence_state == "available":
        tool_names = _extract_server_tool_names(trace)
        evidence = "server-witnessed"
    else:
        tool_names = _extract_tool_names(trace)
        evidence = "transcript"

    built_in_checks: list[TrajectoryCheck] = [
        _ForbiddenCalls(forbidden=scenario.forbidden_calls),
        _CallGroups(must_call=scenario.must_call, order_matters=scenario.order_matters),
    ]
    checks: list[tuple[TrajectoryCheck, bool]] = [
        *((check, False) for check in built_in_checks),
        *((check, True) for check in scenario.trajectory_checks),
    ]

    failures: list[str] = []
    annotations: list[str] = []
    for check, is_custom in checks:
        try:
            passed, detail = check.check_trace(trace, tool_names)
        except Exception as exc:
            passed, detail = False, f"{type(check).__name__}(error: {exc})"
        if not passed:
            failures.append(detail)
        elif is_custom and detail:
            annotations.append(detail)

    if failures:
        return LayerResult(
            passed=False,
            detail=f"{'; '.join(failures)} [evidence: {evidence}]",
        )

    detail = "trajectory requirements satisfied"
    if annotations:
        detail += "; " + "; ".join(annotations)
    return LayerResult(passed=True, detail=f"{detail} [evidence: {evidence}]")


# ─── Constraint evaluator ─────────────────────────────────────────────────────

def evaluate_constraint(trace: Trace, scenario: Scenario) -> LayerResult:
    """Evaluate the constraint layer: policies/permissions respected?

    Each policy is a named predicate over the trace. All must pass.
    Failed policy names are collected for the diagnostic detail.
    """
    failed_policies: list[str] = []
    for policy in scenario.policies:
        try:
            if not policy.predicate(trace):
                failed_policies.append(policy.name)
        except Exception as exc:
            failed_policies.append(f"{policy.name}(error: {exc})")

    if failed_policies:
        return LayerResult(
            passed=False,
            detail=f"constraint violations: {failed_policies}",
        )

    return LayerResult(passed=True, detail="all constraints satisfied")


# ─── Robustness evaluator ─────────────────────────────────────────────────────

def evaluate_robustness(trace: Trace, scenario: Scenario) -> LayerResult:
    """Evaluate the robustness layer: were declared perturbations applied?

    Pass = no perturbations declared (trivially robust) OR all declared
           perturbations have their marker in trace.worker_warnings.
    Fail = perturbations declared but at least one marker is absent.

    The runner is responsible for calling perturbation.apply()
    before the scenario run and passing the marked trace to the evaluator.
    This evaluator just verifies the contract was honoured.
    """
    if not scenario.perturbations:
        return LayerResult(passed=True, detail="no perturbations declared")

    missing_markers: list[str] = []
    for perturbation in scenario.perturbations:
        marker = perturbation.marker
        if not any(marker in w for w in trace.worker_warnings):
            missing_markers.append(marker)

    if missing_markers:
        return LayerResult(
            passed=False,
            detail=f"perturbations declared but not applied: {missing_markers}",
        )

    return LayerResult(passed=True, detail="all perturbations applied")
