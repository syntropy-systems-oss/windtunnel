"""Failure classifier abstraction — Protocol, dataclasses, and category registry.

This module defines the CONTRACT for failure classification. It is intentionally
implementation-free: no rules, no LLM calls, no heuristics. Those live in
rule_based.py, llm_judge.py, and future plugin modules.

The intended consumer is the GEPA-style optimization loop:

    failure trace → classifier.classify() → FailureClassification
                                           ↓
                           optimizer.propose_fix() → ProposedFix
                                           ↓
                           optimizer.apply_fix() → AppliedFix (e.g. updated SOUL.md)
                                           ↓
                           re-run scenario → see if regression resolves

The CLASSIFIER side is this module + rule_based.py (baseline) + an unregistered
llm_judge.py implementation sketch.
The OPTIMIZER side is optimizer.py (stub contracts for GEPA/TextGrad).

See windtunnel/docs/writing-a-classifier.md to implement a custom classifier.
See windtunnel/docs/writing-an-optimizer.md to implement a custom optimizer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score
from windtunnel.api.trace import Trace

# ─── Taxonomy ─────────────────────────────────────────────────────────────────

VALID_CATEGORIES: frozenset[str] = frozenset(
    [
        # The 9 core categories of the remediation map
        "tool_affordance",       # Model doesn't understand a tool's contract
        "clarification",         # Model guesses or refuses instead of clarifying
        "policy",                # Model violates a declared constraint/policy
        "memory",                # Model uses stale/wrong/conflicting memory
        "template_corruption",   # Chat-template serialization failure
        "planning",              # Multi-step reasoning / context tracking fails
        "recovery",              # Model fails to self-correct after wrong prior turn
        "model_capacity",        # Task exceeds model's capability (not a prompt fix)
        "sampler_variance",      # Pass rate variance too high across sampling runs
        # Side-effect-safety dim addition
        "side_effect_safety_violation",  # Agent crossed effect-class boundary
        # Catch-all
        "unknown",               # No rule fired confidently
    ]
)


# ─── FixSuggestion ────────────────────────────────────────────────────────────

@dataclass
class FixSuggestion:
    """Optimizer-actionable hook — what to change and where.

    The fix_vector names the kind of change:
        edit_tool_description  — rewrite a tool's name/description/parameter docs
        edit_soul_md           — add/amend a directive in the agent's SOUL.md
        add_policy             — add a declarative policy block to scenario config
        fix_serializer         — fix the chat-template / tool_call serializer
        adjust_sampler         — change temperature/top_p/tool_choice for this scenario
        add_memory_rule        — add memory priority / conflict-resolution rule
        add_recovery_prompt    — add a "review prior turn" directive
        route_to_stronger_model — escalate to a larger / more capable model
        other                  — catch-all for custom fix vectors

    target: what to modify. Shape depends on fix_vector:
        edit_tool_description → {"tool": "<tool_name>", "field": "description"}
        edit_soul_md          → {"file": "SOUL.md", "section": "<section_name>"}
        add_policy            → {"scenario": "<scenario_name>"}
        etc.

    diff_text: optional natural-language description of the proposed change.
        An optimizer can populate this; a human reviewer can read it and decide.
    """
    fix_vector: str
    target: dict[str, Any]
    rationale: str
    diff_text: str | None = None


# ─── FailureClassification ────────────────────────────────────────────────────

@dataclass
class FailureClassification:
    """Result of classifying one failed run.

    category: one of VALID_CATEGORIES.
    confidence: 0.0–1.0. Rule-based classifiers use 1.0 for exact matches,
        lower for approximate heuristics. Ensemble classifiers (rules + LLM judge)
        can combine scores. When confidence < threshold → category is "unknown".
    evidence: list of human-readable strings explaining why this category was chosen.
        For rule-based: the rule name + matching trace segment.
        For LLM-judge: relevant quote from the judge's reasoning.
    suggested_fix: optimizer-actionable hook. None when the classifier cannot
        suggest a fix (e.g. "unknown" category). Non-None for all named categories.
    """
    category: str
    confidence: float
    evidence: list[str]
    suggested_fix: FixSuggestion | None = None


# ─── FailureClassifier Protocol ───────────────────────────────────────────────

@runtime_checkable
class FailureClassifier(Protocol):
    """Contract for all failure classifiers.

    Implementors must provide a single method: classify(scenario, trace, score)
    → FailureClassification. The method must be pure / side-effect-free —
    no network calls, no file writes, no mutations.

    Built-in implementations:
        RuleBasedClassifier  (windtunnel/triage/rule_based.py) — deterministic rules
        LLMJudgeClassifier   (windtunnel/triage/llm_judge.py)  — unregistered sketch

    Custom implementations:
        See windtunnel/docs/writing-a-classifier.md for the full extension contract.

    Protocol is runtime_checkable so isinstance(clf, FailureClassifier) works for
    dispatch without ABC inheritance.
    """

    def classify(
        self,
        scenario: Scenario,
        trace: Trace,
        score: Score,
    ) -> FailureClassification:
        """Classify a failed run into a taxonomy category.

        Args:
            scenario: The scenario that was run. Carries tags, must_call,
                forbidden_calls, requires_tool_use — all useful signals.
            trace: The full conversation trace. Carries turns (tool_calls,
                content), worker_warnings, and timing data.
            score: The four-layer score for this run. outcome.passed,
                trajectory.passed, constraint.passed, robustness.passed —
                and their detail strings — are all classification signals.

        Returns:
            FailureClassification with category in VALID_CATEGORIES.
            Must never raise — return unknown with confidence=0.0 on error.
        """
        ...
