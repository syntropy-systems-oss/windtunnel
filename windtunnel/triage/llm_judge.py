"""LLM-judge classifier stub — plug point for GEPA / Claude-judge / similar.

This module is INTENTIONALLY UNIMPLEMENTED. Its purpose is to define the
interface contract so a future implementer can drop in an LLM-based classifier
without touching anything else in Wind Tunnel.

How to implement this stub
--------------------------
1. Read windtunnel/docs/writing-a-classifier.md for the full contract.
2. Implement LLMJudgeClassifier.classify() using the pattern below.
3. Register your implementation in the CLI via --classifier llm_judge.

The classify() method receives (scenario, trace, score) and must return a
FailureClassification. It should NOT raise — catch all errors internally and
return FailureClassification(category='unknown', confidence=0.0, evidence=[error_msg]).

Suggested implementation sketch (Claude / Anthropic tool-use pattern)
----------------------------------------------------------------------
::

    def classify(self, scenario, trace, score):
        # 1. Format the trace as a human-readable conversation summary
        prompt = _format_trace_for_judge(scenario, trace, score)

        # 2. Ask an LLM to label the failure category + propose a fix
        #    Use structured output / tool-use to parse the response:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            tools=[CLASSIFY_TOOL_SCHEMA],
            messages=[{"role": "user", "content": prompt}],
        )

        # 3. Parse the tool-use response into FailureClassification
        tool_call = _extract_tool_call(response)
        return FailureClassification(
            category=tool_call["category"],
            confidence=tool_call["confidence"],
            evidence=tool_call["evidence"],
            suggested_fix=FixSuggestion(**tool_call["suggested_fix"])
                          if tool_call.get("suggested_fix") else None,
        )

GEPA integration sketch
-----------------------
GEPA (arXiv 2507.19457) uses natural-language gradients to evolve prompts.
The LLM judge acts as the "gradient" signal:

1. Run scenario → get FailureClassification
2. Pass classification to GEPAOptimizer.propose_fix() → ProposedFix
   The LLM judge proposes a natural-language edit to SOUL.md / tool description
3. Apply the fix → re-run the scenario
4. If pass rate improves AND no regressions on other scenarios → keep the fix
5. Repeat until convergence or budget exhausted

The LLMJudgeClassifier's evidence list is the key input to step 2: it gives
the optimizer specific quotes from the trace that explain the failure.

TextGrad integration sketch
---------------------------
TextGrad (arXiv 2406.07496) treats text as optimizable variables. The trace is
the "forward pass"; the FailureClassification is the "loss". The LLM judge
computes the "gradient" (text feedback on what to change). The optimizer
applies the gradient to the prompt variables (SOUL.md, tool descriptions).

Same interface — different optimizer. The FailureClassifier Protocol is
optimizer-agnostic by design.

Input format for the judge
--------------------------
The judge sees:
  - scenario.name, scenario.prompt, scenario.tags
  - scenario.must_call, scenario.forbidden_calls, scenario.requires_tool_use
  - trace.turns (as a formatted conversation)
  - trace.worker_warnings
  - score.outcome.passed, score.outcome.detail
  - score.trajectory.passed, score.trajectory.detail
  - score.constraint.passed, score.constraint.detail

Output contract
---------------
Must return FailureClassification with:
  - category in VALID_CATEGORIES
  - confidence in [0.0, 1.0]
  - evidence: list of trace quotes / reasoning steps from the judge
  - suggested_fix: a FixSuggestion if the judge can propose one; None otherwise

Error handling
--------------
  - LLM API errors → return unknown, confidence=0.0, evidence=[error message]
  - Invalid category from LLM → coerce to 'unknown'
  - Rate limit → retry with exponential backoff, max 3 attempts; then unknown

See windtunnel/docs/writing-a-classifier.md for the full extension contract.
"""
from __future__ import annotations

from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score
from windtunnel.api.trace import Trace
from windtunnel.triage.classifier import (
    FailureClassification,
)


class LLMJudgeClassifier:
    """Stub LLM-judge classifier — implements FailureClassifier Protocol.

    All methods raise NotImplementedError. This class exists to:
      1. Confirm the Protocol can be satisfied by an LLM-based impl.
      2. Serve as the registration point for `--classifier llm_judge` in the CLI.
      3. Document the interface contract (see module docstring).

    To implement: replace the raise in classify() with an LLM API call.
    See the module docstring for a full sketch.
    """

    def classify(
        self,
        scenario: Scenario,
        trace: Trace,
        score: Score,
    ) -> FailureClassification:
        """Classify a failed run using an LLM judge.

        NOT IMPLEMENTED. This is the plug point for GEPA / Anthropic-tool-use /
        Claude-judge / similar LLM-driven failure classification.

        To implement:
          1. Format (scenario, trace, score) into an LLM prompt.
          2. Ask the LLM to label the failure category from VALID_CATEGORIES.
          3. Ask the LLM to propose a fix (FixSuggestion shape).
          4. Parse the structured response into FailureClassification.
          5. Handle errors by returning unknown with confidence=0.0.

        See windtunnel/docs/writing-a-classifier.md for the full contract
        including input format, output schema, error handling, and GEPA/TextGrad
        integration sketches.

        Raises:
            NotImplementedError: always, until implemented.
        """
        raise NotImplementedError(
            "LLMJudgeClassifier.classify() is not implemented. "
            "This is the plug point for GEPA / Anthropic-tool-use / Claude-judge "
            "/ similar LLM-driven failure classification. "
            "To implement: take (scenario, trace, score), ask an LLM to label "
            "the failure category + propose a fix, parse the response into "
            "FailureClassification. "
            "See windtunnel/docs/writing-a-classifier.md for the full contract."
        )
