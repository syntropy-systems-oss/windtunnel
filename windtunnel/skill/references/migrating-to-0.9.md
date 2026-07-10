<!-- GENERATED from docs/migrating-to-0.9.md at dfb868ab1eae — do not edit; edit docs/migrating-to-0.9.md. -->
---
description: "Migration guide for Wind Tunnel 0.9 scoring gates, experiment integrity, failure risk, and persisted artifact versions."
---
# Migrating to 0.9

Wind Tunnel 0.9 makes scenario intent decide the verdict. It also separates
agent robustness from test-harness integrity and versions the native artifacts
that carry those decisions. Runtime SPI method signatures are unchanged.

## Declared expectations now gate by default

In 0.8, only outcome decided the aggregate verdict. Trajectory and constraint
failures were diagnostic even when the scenario explicitly declared
`must_call`, `forbidden_calls`, a custom `TrajectoryCheck`, or a `Policy`.

In 0.9, `Scenario.resolved_gate_layers()` infers:

- `outcome` for every scenario;
- `trajectory` when any trajectory expectation is declared; and
- `constraint` when any policy is declared.

This means a scenario cannot report `PASS` while violating one of its authored
expectations. If a check is intentionally diagnostic during exploration, say so
explicitly:

```python
Scenario(
    name="observe_lookup_path",
    prompt="Find the account.",
    target_facts=[["ACC-123"]],
    must_call=["account_lookup"],
    gate_layers=["outcome"],
)
```

Remove the explicit `gate_layers` once the trajectory assertion is ready to
gate. `gate_layers=[]` creates a diagnostic-only scenario, but experiment
integrity is still mandatory.

## Integrity and robustness now mean different things

`Score.integrity` answers: *did the declared experiment condition actually
happen?* For perturbations, it checks the evidence markers recorded on the
trace. A failed integrity check makes the aggregate `INVALID`; it does not
count as an agent pass or failure.

Robustness answers: *did the agent satisfy its gate under a valid adverse
condition?* Reports calculate robustness from gate performance on scenarios
that declare perturbations. A suite without perturbation scenarios reports
robustness as `N/A`.

For source compatibility, 0.9 still accepts `Score(..., robustness=...)`,
exposes `score.robustness`, and exports `evaluate_robustness()`. These are 0.8
compatibility spellings for integrity; new code should use `integrity` and
`evaluate_integrity()`.

## Failure cost is operational

`FailureCost` now has a deterministic `risk_weight`:

| Input | Weight |
|---|---:|
| severity `low` / `medium` / `high` / `critical` | 1 / 4 / 16 / 64 |
| customer visible | +2 |
| irreversible | +4 |
| side effect performed | +8 |

Aggregates expose `failure_risk = risk_weight × (1 - pass_rate)`. Reports,
ledger rows, and `wt compare` surface this value and rank regressions by it.
Risk does not relax the gate: every gated regression still fails.

## Scenario authoring is less duplicative

- `prompt` may be omitted when `user_turns` is present.
- `target_facts` defaults to `[]`, so an `outcome_fn` needs no placeholder.
- `scenario.scored_prompt` returns the final `user_turns` entry or the
  single-turn `prompt` and is used by reports and triage.
- Empty names, missing both prompt forms, empty user turns, duplicate or
  unknown gates, and `order_matters=True` without `must_call` fail at authoring
  time.

## Persisted artifacts have explicit versions

New native traces carry `"windtunnel_trace": 1`, score sidecars carry
`"windtunnel_score": 2`, and ledger records carry `"windtunnel_ledger": 1`.
The v2 score stores `integrity` instead of the old `robustness` marker field.

Readers migrate unversioned 0.8 traces and scores in memory. They reject
unknown future versions instead of guessing at their meaning. Contract A
interchange and Contract B universe files remain version 1: additive unknown
fields within v1 are tolerated, while unknown version numbers are rejected.

## Upgrade checklist

1. Run the suite and inspect scenarios whose trajectory or constraint layer
   had been failing diagnostically; those failures now affect the verdict.
2. Use explicit `gate_layers=["outcome"]` only where diagnostic behavior is
   intentional and documented.
3. Rename direct `score.robustness` reads to `score.integrity` and
   `evaluate_robustness` calls to `evaluate_integrity`.
4. Treat `INVALID` as a bench/setup problem that requires rerunning after the
   condition is repaired, not as an agent regression.
5. Confirm artifact consumers tolerate the new version keys and `integrity`
   field. Prefer `score_from_dict()` over parsing score dictionaries directly.
6. Review `FailureCost` annotations so risk-ranked comparisons reflect the
   operational consequence of each scenario.
