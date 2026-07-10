"""Prompt-optimizer plugin contracts — Protocol, dataclasses, and GEPA stub.

This module defines the CONTRACT for prompt optimization. It is intentionally
unimplemented on the optimizer side. The GEPAOptimizer stub documents the
full GEPA loop so a future implementer can plug it in without touching anything
else in Wind Tunnel.

The optimization loop (future work)
------------------------------------
::

    failure trace → classifier.classify() → FailureClassification(category, fix_suggestion)
                                           ↓
                           optimizer.propose_fix() → ProposedFix
                           (LLM proposes a natural-language edit)
                                           ↓
                           optimizer.apply_fix(proposed) → AppliedFix
                           (edit is written to SOUL.md / tool description / config)
                                           ↓
                           re-run scenario → new Score
                                           ↓
                           if pass_rate improved AND no regressions on other scenarios:
                               keep the fix (Pareto-better)
                           else:
                               discard, try next candidate

GEPA loop (arXiv 2507.19457)
-----------------------------
GEPA (Gradient-based Evolutionary Prompt Adaptation) uses natural-language
gradients to evolve prompts. The key insight: the LLM judge's "evidence" field
in FailureClassification IS the gradient signal — specific trace quotes that
explain why the current prompt failed. The optimizer turns those quotes into
a proposed edit via another LLM call.

To implement GEPAOptimizer.propose_fix():
  1. Take classification.evidence + classification.suggested_fix.rationale
  2. Ask an LLM: "Given this failure evidence, what specific edit to
     [SOUL.md / tool description / sampler config] would fix it?"
  3. Return ProposedFix with the natural-language diff.

To implement GEPAOptimizer.apply_fix():
  1. Take proposed.diff_text + proposed.target
  2. Apply the edit to the target file (SOUL.md, tool description, etc.)
  3. Return AppliedFix with status='applied' or status='failed'

TextGrad loop (arXiv 2406.07496)
---------------------------------
TextGrad treats prompt text as an optimizable variable. The "gradient" is
a textual critique of the current prompt variable. apply_fix() writes the
updated text back to the variable (SOUL.md, tool description, etc.).

The Optimizer Protocol is the same — only propose_fix() and apply_fix()
implementations differ.

See windtunnel/docs/writing-an-optimizer.md for the full extension contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import Trace
from windtunnel.triage.classifier import FailureClassification

# ─── ProposedFix ──────────────────────────────────────────────────────────────

@dataclass
class ProposedFix:
    """A proposed fix from an optimizer — input to apply_fix().

    fix_vector: what kind of change (matches FixSuggestion.fix_vector).
    target: what to modify (tool name + field, SOUL.md path, etc.).
    rationale: why this fix is expected to help (from classifier evidence).
    diff_text: the natural-language or unified-diff change to apply.
        Required for apply_fix() to do anything meaningful — the optimizer
        fills this in via propose_fix().
    """
    fix_vector: str
    target: dict[str, Any]
    rationale: str
    diff_text: str | None = None


# ─── AppliedFix ───────────────────────────────────────────────────────────────

@dataclass
class AppliedFix:
    """Result of applying a ProposedFix — output of apply_fix().

    proposed_fix_id: links back to the ProposedFix (e.g. content hash or UUID).
    status: 'applied' | 'failed' | 'skipped'
        applied  — edit was written successfully.
        failed   — edit failed (parse error, file not found, etc.).
        skipped  — optimizer decided not to apply (e.g. confidence too low).
    applied_to: the actual file/field that was modified.
    details: human-readable description of what changed.
    """
    proposed_fix_id: str
    status: str
    applied_to: dict[str, Any]
    details: str


# ─── Optimizer Protocol ───────────────────────────────────────────────────────

@runtime_checkable
class Optimizer(Protocol):
    """Contract for all prompt optimizers.

    Two methods:
      propose_fix(classification, scenario, trace) → ProposedFix
          Given a FailureClassification, produce a ProposedFix with a
          natural-language or diff edit to apply.

      apply_fix(proposed) → AppliedFix
          Write the proposed edit to the target artifact (SOUL.md, tool
          description, sampler config, etc.) and return the result.

    Built-in stubs:
        GEPAOptimizer  — stub for GEPA / TextGrad / LLM-gradient loops.

    Custom implementations:
        See windtunnel/docs/writing-an-optimizer.md for the full contract.

    Protocol is runtime_checkable so isinstance(opt, Optimizer) works.
    """

    def propose_fix(
        self,
        classification: FailureClassification,
        scenario: Scenario,
        trace: Trace,
    ) -> ProposedFix:
        """Propose a fix for the classified failure.

        Args:
            classification: the output of a FailureClassifier.classify() call.
                The evidence list is the key input — it contains specific trace
                quotes that explain why the current prompt failed.
            scenario: the scenario that was run (for context).
            trace: the full conversation trace (for context).

        Returns:
            ProposedFix with diff_text populated. The diff_text is what
            apply_fix() will write to the target artifact.
        """
        ...

    def apply_fix(self, proposed: ProposedFix) -> AppliedFix:
        """Apply a proposed fix to the target artifact.

        Args:
            proposed: the output of propose_fix(). proposed.target identifies
                what to modify; proposed.diff_text is the change to apply.

        Returns:
            AppliedFix with status='applied' on success, 'failed' on error.
        """
        ...


# ─── GEPAOptimizer stub ───────────────────────────────────────────────────────

class GEPAOptimizer:
    """Stub GEPA-style optimizer — implements the Optimizer Protocol.

    All methods raise NotImplementedError. This class exists to:
      1. Confirm the Protocol can be satisfied by a gradient-style optimizer.
      2. Serve as the registration point for future `--optimizer gepa` in the CLI.
      3. Document the GEPA loop contract (see module docstring + below).

    GEPA loop this optimizer implements (once not-stub)
    ---------------------------------------------------
    ::

        for each failed scenario in bench run:
            classification = classifier.classify(scenario, trace, score)
            proposed = optimizer.propose_fix(classification, scenario, trace)
            applied = optimizer.apply_fix(proposed)

            # Re-run the scenario with the patched artifact
            new_score = runner.run_scenario(scenario, runtime, config)

            # Keep only if Pareto-better: improved target + no regressions
            if new_score.outcome.passed and not _any_regressions(full_suite_scores):
                commit_fix(applied)
            else:
                revert_fix(applied)

    propose_fix() implementation sketch
    -------------------------------------
    ::

        def propose_fix(self, classification, scenario, trace):
            evidence_text = "\\n".join(classification.evidence)
            prompt = (
                f"The following agent failure was classified as "
                f"{classification.category!r}:\\n\\n"
                f"Evidence:\\n{evidence_text}\\n\\n"
                f"Suggested fix vector: {classification.suggested_fix.fix_vector}\\n"
                f"Rationale: {classification.suggested_fix.rationale}\\n\\n"
                f"Propose a specific, minimal edit to the agent's SOUL.md or "
                f"tool description that would fix this failure. Output the edit "
                f"as a unified diff or a natural-language instruction."
            )
            response = llm_client.complete(prompt)
            return ProposedFix(
                fix_vector=classification.suggested_fix.fix_vector,
                target=classification.suggested_fix.target,
                rationale=classification.suggested_fix.rationale,
                diff_text=response.text,
            )

    apply_fix() implementation sketch
    -----------------------------------
    ::

        def apply_fix(self, proposed):
            target_file = proposed.target.get("file", "SOUL.md")
            current = Path(target_file).read_text()
            updated = _apply_diff(current, proposed.diff_text)
            Path(target_file).write_text(updated)
            return AppliedFix(
                proposed_fix_id=compute_hash(proposed.diff_text or ""),
                status="applied",
                applied_to=proposed.target,
                details=f"Applied diff to {target_file}",
            )

    See windtunnel/docs/writing-an-optimizer.md for the full contract.
    """

    def propose_fix(
        self,
        classification: FailureClassification,
        scenario: Scenario,
        trace: Trace,
    ) -> ProposedFix:
        """Propose a fix using GEPA-style natural-language gradients.

        NOT IMPLEMENTED. This is the plug point for the GEPA optimization loop.

        To implement: use classification.evidence as the gradient signal.
        Ask an LLM to turn the evidence into a specific edit to the target
        artifact (SOUL.md, tool description, sampler config).

        See windtunnel/docs/writing-an-optimizer.md for the full contract
        and a complete implementation sketch.

        Raises:
            NotImplementedError: always, until implemented.
        """
        raise NotImplementedError(
            "GEPAOptimizer.propose_fix() is not implemented. "
            "This is the plug point for GEPA / TextGrad / LLM-gradient-based "
            "prompt optimization. "
            "To implement: use classification.evidence as the gradient signal, "
            "ask an LLM to propose a specific edit to the target artifact "
            "(SOUL.md / tool description / sampler config), return ProposedFix "
            "with diff_text populated. "
            "See windtunnel/docs/writing-an-optimizer.md for the full contract."
        )

    def apply_fix(self, proposed: ProposedFix) -> AppliedFix:
        """Apply a proposed GEPA fix to the target artifact.

        NOT IMPLEMENTED. This is the plug point for writing edits back to
        SOUL.md, tool descriptions, sampler configs, etc.

        To implement: read proposed.target to find the file/field, apply
        proposed.diff_text, return AppliedFix with status='applied'.

        See windtunnel/docs/writing-an-optimizer.md for the full contract
        and a complete implementation sketch.

        Raises:
            NotImplementedError: always, until implemented.
        """
        raise NotImplementedError(
            "GEPAOptimizer.apply_fix() is not implemented. "
            "This is the plug point for writing GEPA-proposed edits back to "
            "SOUL.md / tool descriptions / sampler configs. "
            "To implement: read proposed.target to locate the artifact, apply "
            "proposed.diff_text, return AppliedFix(status='applied', ...). "
            "See windtunnel/docs/writing-an-optimizer.md for the full contract."
        )
