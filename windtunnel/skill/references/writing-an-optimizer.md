<!-- GENERATED from docs/writing-an-optimizer.md at 39841cf2a4a5 — do not edit; edit docs/writing-an-optimizer.md. -->
---
description: "Design guide for prompt optimizer implementations using Wind Tunnel failure classifications and fix vectors."
---
# Writing a Custom Prompt Optimizer

Wind Tunnel optimizers implement the `Optimizer` Protocol defined in
`windtunnel/triage/optimizer.py`. Any object with `propose_fix()` and
`apply_fix()` methods satisfies the Protocol.

Wind Tunnel 0.5.0 ships the Protocol and dataclasses only. The built-in
`GEPAOptimizer` is a stub whose methods raise `NotImplementedError`, and there
is no shipped optimizer CLI loop. Treat the GEPA and TextGrad sections below as
design sketches for downstream implementations, not runnable functionality.

## The interface

```python
from windtunnel.triage.optimizer import Optimizer, ProposedFix, AppliedFix

class MyOptimizer:
    def propose_fix(
        self,
        classification: FailureClassification,
        scenario: Scenario,
        trace: Trace,
    ) -> ProposedFix:
        ...

    def apply_fix(self, proposed: ProposedFix) -> AppliedFix:
        ...
```

## GEPA design sketch

GEPA (arXiv 2507.19457) uses natural-language gradients to evolve prompts.
The intended loop:

```
for each failed scenario in bench run:
    classification = classifier.classify(scenario, trace, score)
    proposed = optimizer.propose_fix(classification, scenario, trace)
    applied = optimizer.apply_fix(proposed)

    # Re-run the scenario with the patched artifact
    new_score = runner.run_scenario(scenario, runtime, config)

    # Keep only if Pareto-better: improved target + no regressions
    if new_score.outcome.passed and not any_regressions(full_suite):
        commit_fix(applied)
    else:
        revert_fix(applied)
```

The `FailureClassification.evidence` list is the key gradient signal — it
contains specific trace quotes explaining why the current prompt failed. The
optimizer turns those quotes into a concrete edit via `propose_fix()`.

## `propose_fix()` design sketch

```python
import anthropic

class MyGEPAOptimizer:
    def __init__(self, model: str = "claude-opus-4-5"):
        self.client = anthropic.Anthropic()
        self.model = model

    def propose_fix(self, classification, scenario, trace):
        evidence_text = "\n".join(f"- {e}" for e in classification.evidence)
        fix = classification.suggested_fix

        prompt = (
            f"An agent scenario named {scenario.name!r} failed.\n\n"
            f"Failure category: {classification.category}\n"
            f"Confidence: {classification.confidence:.0%}\n\n"
            f"Evidence from the trace:\n{evidence_text}\n\n"
            f"Suggested fix vector: {fix.fix_vector if fix else 'unknown'}\n"
            f"Rationale: {fix.rationale if fix else 'unknown'}\n\n"
            "Propose a specific, minimal edit to the agent's SOUL.md or tool "
            "description that would fix this failure. Output only the edit as "
            "a natural-language instruction or unified diff."
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        diff_text = resp.content[0].text
        return ProposedFix(
            fix_vector=fix.fix_vector if fix else "edit_soul_md",
            target=fix.target if fix else {"scenario": scenario.name},
            rationale=fix.rationale if fix else "",
            diff_text=diff_text,
        )
```

## `apply_fix()` design sketch

```python
    def apply_fix(self, proposed):
        from pathlib import Path
        from windtunnel.api.trace import compute_hash

        target_file = proposed.target.get("file", "SOUL.md")
        path = Path(target_file)

        if not path.exists():
            return AppliedFix(
                proposed_fix_id=compute_hash(proposed.diff_text or ""),
                status="failed",
                applied_to=proposed.target,
                details=f"Target file not found: {target_file}",
            )

        current = path.read_text()
        # Simple append strategy — for unified diff, use `patch` subprocess
        updated = current + "\n\n" + (proposed.diff_text or "")
        path.write_text(updated)

        return AppliedFix(
            proposed_fix_id=compute_hash(proposed.diff_text or ""),
            status="applied",
            applied_to=proposed.target,
            details=f"Appended fix to {target_file}",
        )
```

## TextGrad design sketch

TextGrad (arXiv 2406.07496) treats prompt text as optimizable variables.
The "gradient" is a textual critique of the current prompt. The optimizer
applies the gradient update to the prompt variable (SOUL.md, tool description).

This is not implemented in Wind Tunnel 0.5.0. A downstream implementation
would use the same Protocol with a different `propose_fix()` implementation:
- Use TextGrad's `Variable` and `TextLoss` abstractions.
- The `FailureClassification.evidence` list drives the `TextLoss`.
- The updated variable text becomes `ProposedFix.diff_text`.
- `apply_fix()` writes the updated variable back to the file.

## Fix vector reference

| fix_vector             | target shape                                        | what to modify          |
|------------------------|-----------------------------------------------------|-------------------------|
| `edit_soul_md`         | `{"file": "SOUL.md", "section": "<section>"}`      | Agent persona/directives|
| `edit_tool_description`| `{"tool": "<name>", "field": "description"}`        | MCP tool description    |
| `add_policy`           | `{"scenario": "<name>"}`                            | Scenario constraint     |
| `fix_serializer`       | `{"component": "chat_template_serializer"}`         | Platform serialization  |
| `adjust_sampler`       | `{"scenario": "<name>", "param": "temperature"}`   | Sampler config          |
| `add_memory_rule`      | `{"file": "SOUL.md", "section": "memory"}`         | Memory priority rules   |
| `add_recovery_prompt`  | `{"file": "SOUL.md", "section": "recovery"}`       | Recovery directives     |
| `route_to_stronger_model` | `{"current_model": "<name>"}`                  | Runtime model config    |

## Pareto check

The optimizer loop must check for regressions before committing a fix.
A fix is Pareto-better if:
1. The target scenario's pass rate improved.
2. No other scenario's pass rate degraded by more than the variance budget.

```python
def is_pareto_better(before: dict[str, float], after: dict[str, float],
                     target: str, variance_budget: float = 0.05) -> bool:
    if after[target] <= before[target]:
        return False
    for scenario, before_rate in before.items():
        if scenario == target:
            continue
        if after.get(scenario, 0.0) < before_rate - variance_budget:
            return False
    return True
```

## See also

- `windtunnel/triage/optimizer.py` — Protocol + GEPAOptimizer stub
- `windtunnel/triage/classifier.py` — FailureClassifier Protocol
- [failure-taxonomy.md](failure-taxonomy.md) — categories and fix vectors
- [writing-a-classifier.md](writing-a-classifier.md) — how to implement a classifier
