"""Wind Tunnel triage — failure classification + optimizer plugin contracts.

Public surface:

    from windtunnel.triage.classifier import (
        FailureClassifier,
        FailureClassification,
        FixSuggestion,
        VALID_CATEGORIES,
    )
    from windtunnel.triage.rule_based import RuleBasedClassifier
    from windtunnel.triage.llm_judge import LLMJudgeClassifier
    from windtunnel.triage.optimizer import Optimizer, GEPAOptimizer, ProposedFix, AppliedFix

Design note: this package is deliberately abstraction-heavy / implementation-light.
The RuleBasedClassifier is the baseline; LLMJudgeClassifier and GEPAOptimizer are
stub contracts for future GEPA/TextGrad/LLM-judge plugins. See
windtunnel/docs/writing-a-classifier.md and windtunnel/docs/writing-an-optimizer.md
for the extension contracts.
"""
