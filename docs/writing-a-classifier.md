# Writing a Custom Failure Classifier

Wind Tunnel classifiers implement the `FailureClassifier` Protocol defined in
`windtunnel/triage/classifier.py`. Any object with a `classify(scenario, trace,
score) → FailureClassification` method satisfies the Protocol.

## The interface

```python
from windtunnel.triage.classifier import (
    FailureClassifier,
    FailureClassification,
    FixSuggestion,
    VALID_CATEGORIES,
)

class MyClassifier:
    def classify(
        self,
        scenario: Scenario,
        trace: Trace,
        score: Score,
    ) -> FailureClassification:
        ...
```

## Input signals available

| Signal | Where | Notes |
|--------|-------|-------|
| `scenario.tags` | `Scenario` | dim tags: `dim:tool_affordance`, `dim:clarify_vs_guess`, etc. |
| `scenario.requires_tool_use` | `Scenario` | True → failure with no tools is tool_affordance |
| `scenario.must_call` / `forbidden_calls` | `Scenario` | trajectory expectations |
| `trace.turns` | `Trace` | full conversation: role, content, tool_calls, tool_results |
| `trace.worker_warnings` | `Trace` | `verdict_bucket:<value>`, `apply_chat_template raised`, etc. |
| `score.outcome.passed` / `.detail` | `Score` | outcome layer; detail contains `no_tools_used` flag |
| `score.trajectory.passed` / `.detail` | `Score` | trajectory layer |
| `score.constraint.passed` / `.detail` | `Score` | constraint layer; detail names failed policies |
| `score.robustness.passed` / `.detail` | `Score` | robustness layer |

## Output contract

```python
@dataclass
class FailureClassification:
    category: str        # must be in VALID_CATEGORIES
    confidence: float    # 0.0–1.0
    evidence: list[str]  # trace quotes / reasoning steps
    suggested_fix: FixSuggestion | None
```

- `category` **must** be in `VALID_CATEGORIES`. Coerce unknown LLM output to `"unknown"`.
- `confidence` should be calibrated: high-confidence classifications should agree
  with hand-labels more often than low-confidence ones.
- `evidence` should be human-readable trace quotes or reasoning steps that explain
  why this category was chosen. This is the "gradient" input to the optimizer.
- `suggested_fix` should be non-None for all named categories. Use `None` only for
  `"unknown"`.
- **Never raise.** Catch all errors and return `FailureClassification(category="unknown",
  confidence=0.0, evidence=[str(exc)])`.

## LLM-judge status

Wind Tunnel 0.5.0 ships the deterministic `RuleBasedClassifier`. It also ships
an `LLMJudgeClassifier` class and a `wt triage --classifier llm_judge` parser
choice as a registration point, but the class raises `NotImplementedError`.

The sketch below is design guidance for a future implementation or downstream
fork. It is not runnable as shipped.

## LLM-judge design sketch

```python
import anthropic

CLASSIFY_TOOL = {
    "name": "classify_failure",
    "description": "Classify an agent failure into a taxonomy category.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(VALID_CATEGORIES)},
            "confidence": {"type": "number"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "fix_vector": {"type": "string"},
            "fix_rationale": {"type": "string"},
        },
        "required": ["category", "confidence", "evidence"],
    },
}

class MyLLMJudgeClassifier:
    def __init__(self, model: str = "claude-opus-4-5"):
        self.client = anthropic.Anthropic()
        self.model = model

    def classify(self, scenario, trace, score):
        try:
            prompt = _format_prompt(scenario, trace, score)
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "classify_failure"},
                messages=[{"role": "user", "content": prompt}],
            )
            call = next(b for b in resp.content if b.type == "tool_use")
            inp = call.input
            fix = None
            if inp.get("fix_vector"):
                fix = FixSuggestion(
                    fix_vector=inp["fix_vector"],
                    target={"scenario": scenario.name},
                    rationale=inp.get("fix_rationale", ""),
                )
            return FailureClassification(
                category=inp["category"] if inp["category"] in VALID_CATEGORIES else "unknown",
                confidence=float(inp["confidence"]),
                evidence=inp["evidence"],
                suggested_fix=fix,
            )
        except Exception as exc:
            return FailureClassification(
                category="unknown",
                confidence=0.0,
                evidence=[f"LLMJudge error: {exc}"],
            )
```

## Registering with the CLI

There is no classifier entry-point group in 0.5.0. The shipped CLI supports:

- `rule_based`: implemented.
- `llm_judge`: reserved, but raises `NotImplementedError` until implemented.

For an in-repo classifier, add it to the `--classifier` choices and dispatch in
`windtunnel/cli.py`:

```python
# In _cmd_triage():
if args.classifier == "my_classifier":
    from my_package import MyClassifier
    clf = MyClassifier()
```

## Testing your classifier

Add labeled fixtures to `tests/fixtures/labeled_failures/`. Each fixture is a
JSON file with the shape:

```json
{
    "expected_category": "tool_affordance",
    "trace": { ... },
    "scenario": { "name": "...", "prompt": "...", ... },
    "score": {
        "outcome": {"passed": false, "detail": "..."},
        "trajectory": {"passed": true, "detail": "ok"},
        "constraint": {"passed": true, "detail": "ok"},
        "robustness": {"passed": true, "detail": "ok"}
    }
}
```

Run the agreement test:

```bash
uv run pytest tests/test_triage.py::TestLabeledFixtures::test_classifier_agreement -v
```

Target: >= 80% agreement on the hand-labeled set.

## See also

- `windtunnel/triage/rule_based.py` — the baseline rule-based classifier
- `windtunnel/triage/llm_judge.py` — the LLM-judge stub with implementation notes
- [failure-taxonomy.md](failure-taxonomy.md) — category definitions and fix vectors
- [writing-an-optimizer.md](writing-an-optimizer.md) — how to implement an optimizer
