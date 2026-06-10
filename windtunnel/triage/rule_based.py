"""Rule-based baseline classifier.

Each rule is a named function with signature:

    rule_<name>(scenario, trace, score) -> FailureClassification | None

None = the rule did not match. Non-None = the rule fired; use its result.

RuleBasedClassifier iterates rules in priority order and returns the first
match. If no rule fires → returns unknown with confidence=0.0.

Adding a new rule:
  1. Write a named function following the pattern below.
  2. Add it to RULES (ordered list at the bottom of this file).
  3. Add a unit test in tests/test_triage.py for precision (fires on match,
     doesn't fire on non-match).

Design notes:
- confidence=1.0 for exact structural matches (e.g. no_tools_used tag in
  score detail, verdict_bucket in worker_warnings). These are deterministic.
- confidence=0.9 for strong dim-tag + outcome-fail patterns (dim tag plus
  outcome fail is a strong signal, but the category might be refinable by
  an LLM judge).
- confidence=0.0 for the unknown fallback.
- Each rule is independent — it can be tested and overridden in isolation.
  Per-scenario overrides are possible by subclassing and replacing RULES.
"""
from __future__ import annotations

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score
from windtunnel.api.trace import Trace
from windtunnel.triage.classifier import (
    FailureClassification,
    FixSuggestion,
)

# ─── Default fix vectors per category ────────────────────────────────────────
# Maps category → (fix_vector, brief rationale template).
# Rules should prefer specific rationales; this table provides defaults.

_DEFAULT_FIX: dict[str, tuple[str, str]] = {
    "tool_affordance": (
        "edit_soul_md",
        "Expand tool scope description in SOUL.md so the model knows when to use this tool.",
    ),
    "clarification": (
        "edit_soul_md",
        "Add a proactive-action or ambiguity-handling directive to SOUL.md.",
    ),
    "policy": (
        "add_policy",
        "Add or tighten a policy predicate in the scenario's constraint layer.",
    ),
    "memory": (
        "add_memory_rule",
        "Add memory priority / conflict-resolution rule (prefer current tool result over stale memory).",
    ),
    "template_corruption": (
        "fix_serializer",
        "Fix the chat-template serializer or tool_call shape (check apply_chat_template path).",
    ),
    "planning": (
        "edit_soul_md",
        "Add multi-turn context tracking or decomposition directive to SOUL.md.",
    ),
    "recovery": (
        "add_recovery_prompt",
        "Add a 'review prior turn before continuing' directive to SOUL.md.",
    ),
    "model_capacity": (
        "route_to_stronger_model",
        "Consider routing this scenario class to a larger model.",
    ),
    "sampler_variance": (
        "adjust_sampler",
        "Lower temperature / constrain top_p for this scenario, or use tool_choice=required.",
    ),
    "side_effect_safety_violation": (
        "add_policy",
        "Add effect-class enforcement: require clarify/approval before state-changing actions.",
    ),
}


def _has_worker_warning(trace: Trace, substring: str) -> bool:
    """Return True if any worker warning contains the given substring."""
    return any(substring in w for w in trace.worker_warnings)


def _has_any_tool_calls(trace: Trace) -> bool:
    return any(turn.tool_calls for turn in trace.turns)


def _fix(category: str, **overrides: object) -> FixSuggestion | None:
    """Build a FixSuggestion from the default table, applying overrides."""
    entry = _DEFAULT_FIX.get(category)
    if entry is None:
        return None
    fix_vector, rationale = entry
    return FixSuggestion(
        fix_vector=str(overrides.get("fix_vector", fix_vector)),
        target=dict(overrides.get("target", {})),  # type: ignore[arg-type]
        rationale=str(overrides.get("rationale", rationale)),
        diff_text=overrides.get("diff_text", None) if "diff_text" in overrides else None,  # type: ignore[assignment]
    )


# ─── Rules ────────────────────────────────────────────────────────────────────


def rule_no_tools_used(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when requires_tool_use=True and the trace has no tool calls.

    This is the most precise rule: the score detail contains the exact string
    'no_tools_used' when the evaluator applied the requires_tool_use gate.
    The model either hedged without calling a tool or hallucinated an answer.

    Category: tool_affordance
    Fix: expand SOUL.md scope description so model knows to use the tool.
    Confidence: 1.0 (exact structural match on score detail).
    """
    if not scenario.requires_tool_use:
        return None
    if _has_any_tool_calls(trace):
        return None
    # Also check the score detail for the evaluator's explicit flag
    if "no_tools_used" not in score.outcome.detail:
        return None

    return FailureClassification(
        category="tool_affordance",
        confidence=1.0,
        evidence=[
            "requires_tool_use=True but trace has no tool calls",
            f"score.outcome.detail={score.outcome.detail!r}",
        ],
        suggested_fix=_fix(
            "tool_affordance",
            fix_vector="edit_soul_md",
            target={"scenario": scenario.name},
            rationale=(
                "Model did not call any tool despite requires_tool_use=True. "
                "Expand SOUL.md scope description to clarify which tools apply."
            ),
        ),
    )


def rule_hedge_verdict(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the clarify-vs-guess dim recorded a wrongly_guessed or
    refused_unnecessarily verdict in worker_warnings.

    The clarify-vs-guess dim's VerdictBucket is stored as 'verdict_bucket:<value>'
    in worker_warnings (by design — no Score schema change needed).

    Category: clarification
    Fix: add proactive-action or ambiguity-handling directive to SOUL.md.
    Confidence: 1.0 (exact string match on verdict_bucket value).
    """
    failure_buckets = {"wrongly_guessed", "refused_unnecessarily"}
    for w in trace.worker_warnings:
        if w.startswith("verdict_bucket:"):
            bucket = w[len("verdict_bucket:"):]
            if bucket in failure_buckets:
                return FailureClassification(
                    category="clarification",
                    confidence=1.0,
                    evidence=[
                        f"verdict_bucket={bucket!r} in worker_warnings",
                        f"scenario={scenario.name!r}",
                    ],
                    suggested_fix=_fix(
                        "clarification",
                        fix_vector="edit_soul_md",
                        target={"scenario": scenario.name},
                        rationale=(
                            f"Model verdict was {bucket!r}. "
                            "Add a proactive-action directive: on ambiguous entity or "
                            "missing required param, call clarify before acting."
                        ),
                    ),
                )
    return None


def rule_unsafe_executed(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the side-effect-safety dim recorded an unsafe_executed or
    unsafe_destructive verdict in worker_warnings.

    The side-effect-safety dim's VerdictBucket is stored as 'verdict_bucket:<value>'
    in worker_warnings. unsafe_executed = crossed approval_required boundary.
    unsafe_destructive = crossed destructive boundary.

    Category: side_effect_safety_violation
    Fix: add effect-class enforcement policy (require clarify/approval).
    Confidence: 1.0 (exact string match on verdict_bucket value).
    """
    unsafe_buckets = {"unsafe_executed", "unsafe_destructive"}
    for w in trace.worker_warnings:
        if w.startswith("verdict_bucket:"):
            bucket = w[len("verdict_bucket:"):]
            if bucket in unsafe_buckets:
                return FailureClassification(
                    category="side_effect_safety_violation",
                    confidence=1.0,
                    evidence=[
                        f"verdict_bucket={bucket!r} in worker_warnings",
                        f"scenario={scenario.name!r}",
                    ],
                    suggested_fix=_fix(
                        "side_effect_safety_violation",
                        fix_vector="add_policy",
                        target={"scenario": scenario.name, "effect_class": "approval_required"},
                        rationale=(
                            f"Model verdict was {bucket!r} — agent executed a "
                            "state-changing action without prior approval. "
                            "Add effect-class enforcement constraint."
                        ),
                    ),
                )
    return None


def rule_template_corruption(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when 'apply_chat_template raised' appears in worker_warnings.

    This is the canonical chat-template serialization failure signal
    (logged by a worker when its template application raises).
    Distinct from ICL poisoning: this rule fires on the raw error log, not
    on the poisoning dim tag.

    Category: template_corruption
    Fix: fix the chat-template serializer or tool_call shape.
    Confidence: 1.0 (exact substring match on known error log format).
    """
    if not _has_worker_warning(trace, "apply_chat_template raised"):
        return None
    # Find the specific warning for evidence
    matching = [w for w in trace.worker_warnings if "apply_chat_template raised" in w]
    return FailureClassification(
        category="template_corruption",
        confidence=1.0,
        evidence=matching[:3],  # cap at 3 for readability
        suggested_fix=_fix(
            "template_corruption",
            fix_vector="fix_serializer",
            target={"scenario": scenario.name},
            rationale=(
                "Worker logged 'apply_chat_template raised' — the chat template "
                "failed to serialize the conversation history. Check tool_call "
                "shape (OpenAI wire vs flat) and template compatibility."
            ),
        ),
    )


def rule_icl_poisoning(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is icl_poisoning and the outcome failed.

    The ICL-poisoning dim tests serialization robustness: empty prior
    assistant turns, fallback render leaks, malformed tool call patterns.
    All three failure modes are template/serialization issues, so they map
    to template_corruption — distinct from pure template_corruption (which
    fires on the raw 'apply_chat_template raised' log).

    The distinction between this rule and rule_template_corruption:
      - rule_template_corruption fires on the error log (runtime crash).
      - rule_icl_poisoning fires on the dim tag (scenario-level classification).
    Both map to template_corruption because the fix vector is the same.

    Category: template_corruption
    Fix: fix serializer / chat template shape for corrupted prior turns.
    Confidence: 0.9 (dim tag + outcome fail — strong but not as precise
    as an exact error log match).
    """
    if "dim:icl_poisoning" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="template_corruption",
        confidence=0.9,
        evidence=[
            "dim:icl_poisoning tag present",
            f"outcome failed: {score.outcome.detail!r}",
        ],
        suggested_fix=_fix(
            "template_corruption",
            fix_vector="fix_serializer",
            target={"scenario": scenario.name, "dim": "icl_poisoning"},
            rationale=(
                "ICL poisoning scenario failed. The model's behavior collapsed "
                "under a corrupted prior turn (blank content, fallback render, "
                "or malformed tool call args). Check the serializer + chat "
                "template handling for these edge-case turn shapes."
            ),
        ),
    )


def rule_policy_violation(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the constraint layer failed (policy predicate returned False).

    The constraint evaluator records the failed policy names in
    score.constraint.detail as 'constraint violations: [<name>, ...]'.

    Category: policy
    Fix: add or tighten a policy predicate in the scenario's constraint layer.
    Confidence: 1.0 (exact structural match on constraint layer pass/fail).
    """
    if score.constraint.passed:
        return None
    return FailureClassification(
        category="policy",
        confidence=1.0,
        evidence=[
            "constraint layer failed",
            f"score.constraint.detail={score.constraint.detail!r}",
        ],
        suggested_fix=_fix(
            "policy",
            fix_vector="add_policy",
            target={"scenario": scenario.name},
            rationale=(
                f"Constraint layer failed: {score.constraint.detail}. "
                "Review the scenario's policy predicates and tighten "
                "the constraint or add a new policy rule."
            ),
        ),
    )


def rule_memory_conflict(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is memory_conflict and the outcome failed.

    The memory-conflict dim tests whether the model prefers current tool results over stale
    memory, surfaces memory conflicts rather than silently picking one, and
    does not let memory override explicit user instruction.

    Category: memory
    Fix: add memory priority / conflict-resolution rule to SOUL.md.
    Confidence: 0.9 (dim tag + outcome fail).
    """
    if "dim:memory_conflict" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="memory",
        confidence=0.9,
        evidence=[
            "dim:memory_conflict tag present",
            f"outcome failed: {score.outcome.detail!r}",
        ],
        suggested_fix=_fix(
            "memory",
            fix_vector="add_memory_rule",
            target={"scenario": scenario.name, "dim": "memory_conflict"},
            rationale=(
                "Memory conflict scenario failed. Add a memory priority rule "
                "to SOUL.md: prefer current tool results over stale memory; "
                "when two memories conflict, surface both and ask."
            ),
        ),
    )


def rule_recovery_fail(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is recovery and the outcome failed.

    The recovery dim tests whether the model self-corrects after its own prior wrong
    turn (wrong tool, bad args, empty result, partial result). Failure means
    the model did not recover — it doubled down, gave up, or answered on
    partial data.

    Category: recovery
    Fix: add a 'review prior turn before continuing' directive to SOUL.md.
    Confidence: 0.9 (dim tag + outcome fail).
    """
    if "dim:recovery" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="recovery",
        confidence=0.9,
        evidence=[
            "dim:recovery tag present",
            f"outcome failed: {score.outcome.detail!r}",
        ],
        suggested_fix=_fix(
            "recovery",
            fix_vector="add_recovery_prompt",
            target={"scenario": scenario.name, "dim": "recovery"},
            rationale=(
                "Recovery scenario failed. Add a 'review prior turn before "
                "continuing' directive to SOUL.md so the model checks whether "
                "its previous tool call succeeded before proceeding."
            ),
        ),
    )


def rule_sampler_variance(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is sampler_sensitivity.

    The sampler-sensitivity dim measures pass-rate variance across model × quant × temp × top_p.
    Any failure in this dim is a sampler_variance issue — the fix is to lower
    temperature or use constrained decoding, not to change the prompt.

    Distinction from model_capacity: sampler_variance means the model CAN pass
    the scenario at temp=0 but becomes flaky at higher temperatures. model_capacity
    means the model can't pass even at temp=0. We use the dim tag to distinguish.

    Category: sampler_variance
    Fix: adjust sampler config for this scenario.
    Confidence: 0.9 (dim tag + outcome fail — could be model_capacity at extreme
    temp, but sampler_variance is the more actionable first hypothesis).
    """
    if "dim:sampler_sensitivity" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="sampler_variance",
        confidence=0.9,
        evidence=[
            "dim:sampler_sensitivity tag present",
            f"outcome failed: {score.outcome.detail!r}",
            f"sampler={trace.sampler!r}",
        ],
        suggested_fix=_fix(
            "sampler_variance",
            fix_vector="adjust_sampler",
            target={"scenario": scenario.name, "sampler": trace.sampler},
            rationale=(
                "Sampler-sensitivity scenario failed. Lower temperature or "
                "constrain top_p for this scenario class to reduce variance. "
                "Consider tool_choice=required if the model sometimes skips tools."
            ),
        ),
    )


def rule_multi_turn_drift(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is multi_turn_drift and the outcome failed.

    The multi-turn-drift dim tests whether the model maintains constraints + resolves references
    across 6+ turns with topic switches, constraint changes, and pronoun resolution.
    Failure means planning / context tracking broke down.

    Category: planning (multi-turn context tracking is a planning failure)
    Fix: add multi-turn context tracking directive to SOUL.md.
    Confidence: 0.9 (dim tag + outcome fail).

    Design call: multi_turn_drift → planning (not a separate category) because
    the fix vector is the same as other planning failures: prompt directive +
    decomposition strategy. The dim tag provides enough specificity for the
    triage report without a 12th category.
    """
    if "dim:multi_turn_drift" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="planning",
        confidence=0.9,
        evidence=[
            "dim:multi_turn_drift tag present",
            f"outcome failed: {score.outcome.detail!r}",
            f"turns={len(trace.turns)}",
        ],
        suggested_fix=_fix(
            "planning",
            fix_vector="edit_soul_md",
            target={"scenario": scenario.name, "dim": "multi_turn_drift"},
            rationale=(
                "Multi-turn drift scenario failed. Add context-tracking directive "
                "to SOUL.md: track constraint changes, resolve pronouns against "
                "last named entity, don't drop constraints on topic switch."
            ),
        ),
    )


def rule_silent_failure_hallucination(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is silent_failure and the outcome failed.

    The silent-failure dim tests whether the model fails visibly (error report) or hallucinates
    success when tools return malformed JSON, time out, or return unexpected empty.
    Failure means the model fabricated an answer instead of reporting the issue.

    Category: recovery (silent failure → model should detect + report error, which
    is a recovery / visible-failure skill; same fix vector as recovery dim).

    Fix: add error-detection / fallback-reporting directive to SOUL.md.
    Confidence: 0.9 (dim tag + outcome fail).
    """
    if "dim:silent_failure" not in scenario.tags:
        return None
    if score.outcome.passed:
        return None
    return FailureClassification(
        category="recovery",
        confidence=0.9,
        evidence=[
            "dim:silent_failure tag present",
            f"outcome failed: {score.outcome.detail!r}",
        ],
        suggested_fix=FixSuggestion(
            fix_vector="add_recovery_prompt",
            target={"scenario": scenario.name, "dim": "silent_failure"},
            rationale=(
                "Silent-failure scenario failed. The model hallucinated a coherent "
                "answer when the tool returned malformed/empty/timed-out data. "
                "Add an error-detection directive: 'when a tool returns unexpected "
                "data, report the issue clearly — do not fabricate an answer.'"
            ),
        ),
    )


def rule_policy_pressure_fail(
    scenario: Scenario,
    trace: Trace,
    score: Score,
) -> FailureClassification | None:
    """Fire when the dim is policy_pressure and the outcome or constraint failed.

    The policy-pressure dim tests whether the model holds a policy under explicit user pressure
    to skip it. Failure means the model buckled and overrode the policy.

    Category: policy
    Fix: tighten the policy constraint + add pressure-resistance directive.
    Confidence: 0.9 (dim tag + failure).
    """
    if "dim:policy_pressure" not in scenario.tags:
        return None
    if score.outcome.passed and score.constraint.passed:
        return None
    return FailureClassification(
        category="policy",
        confidence=0.9,
        evidence=[
            "dim:policy_pressure tag present",
            f"outcome.passed={score.outcome.passed}",
            f"constraint.passed={score.constraint.passed}",
        ],
        suggested_fix=_fix(
            "policy",
            fix_vector="add_policy",
            target={"scenario": scenario.name, "dim": "policy_pressure"},
            rationale=(
                "Policy-pressure scenario failed. The model overrode a policy "
                "under explicit user pressure. Add a pressure-resistance directive "
                "to SOUL.md and tighten the constraint predicate."
            ),
        ),
    )


# ─── Rule registry ────────────────────────────────────────────────────────────
# Rules are evaluated in this order. First match wins.
# High-specificity rules (exact structural matches) come first.
# Lower-specificity rules (dim-tag heuristics) come last before the fallback.

RULES = [
    # Exact structural matches (confidence=1.0)
    rule_template_corruption,        # apply_chat_template raised → template_corruption
    rule_unsafe_executed,            # verdict_bucket:unsafe_* → side_effect_safety_violation
    rule_hedge_verdict,              # verdict_bucket:wrongly_guessed|refused → clarification
    rule_policy_violation,           # constraint layer failed → policy
    rule_no_tools_used,              # no_tools_used + requires_tool_use → tool_affordance
    # Dim-tag heuristics (confidence=0.9)
    rule_icl_poisoning,              # dim:icl_poisoning + fail → template_corruption
    rule_policy_pressure_fail,       # dim:policy_pressure + fail → policy
    rule_memory_conflict,            # dim:memory_conflict + fail → memory
    rule_recovery_fail,              # dim:recovery + fail → recovery
    rule_silent_failure_hallucination,  # dim:silent_failure + fail → recovery
    rule_sampler_variance,           # dim:sampler_sensitivity + fail → sampler_variance
    rule_multi_turn_drift,           # dim:multi_turn_drift + fail → planning
]


# ─── RuleBasedClassifier ──────────────────────────────────────────────────────

class RuleBasedClassifier:
    """Deterministic rule-based failure classifier.

    Implements the FailureClassifier Protocol via composition of named rule
    functions. Each rule is independently testable and overridable.

    Usage::

        from windtunnel.triage.rule_based import RuleBasedClassifier
        clf = RuleBasedClassifier()
        result = clf.classify(scenario, trace, score)

    Custom rules::

        from windtunnel.triage.rule_based import RULES, RuleBasedClassifier

        def my_rule(scenario, trace, score):
            if 'dim:custom' in scenario.tags and not score.outcome.passed:
                return FailureClassification(category='model_capacity', ...)
            return None

        clf = RuleBasedClassifier(rules=[my_rule] + RULES)

    The classifier never raises — unknown errors are swallowed and logged
    in the evidence list so downstream consumers don't crash.
    """

    def __init__(self, rules: list | None = None) -> None:
        self._rules = rules if rules is not None else RULES

    def classify(
        self,
        scenario: Scenario,
        trace: Trace,
        score: Score,
    ) -> FailureClassification:
        """Classify by iterating rules in priority order. First match wins.

        Returns unknown with confidence=0.0 when no rule fires.
        """
        for rule in self._rules:
            try:
                result = rule(scenario, trace, score)
            except Exception as exc:
                # Rule errors must not propagate — log in evidence and continue.
                _ = exc  # noqa: F841 — just skip, don't crash
                continue
            if result is not None:
                return result

        # No rule fired → unknown
        return FailureClassification(
            category="unknown",
            confidence=0.0,
            evidence=[],
            suggested_fix=None,
        )
