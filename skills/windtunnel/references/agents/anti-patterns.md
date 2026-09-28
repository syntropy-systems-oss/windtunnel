<!-- GENERATED from docs/agents/anti-patterns.md at 6a8ec84628f2 — do not edit; edit docs/agents/anti-patterns.md. -->
---
description: "Agent-only list of Wind Tunnel integration mistakes that produce misleading benches or hard validation failures."
agent:
  only: true
---
# Anti-patterns

These are not style preferences. Each one maps to a failure mode Wind Tunnel is
designed to make visible.

## Re-running the whole pack to check one fix, or to "see what's working"

A scenario fails, you change one thing, and you re-run every scenario to find
out whether that one thing worked. That spends the pack's wall-clock time on a
question one scenario answers, and on a slow bench it turns a five-minute
debugging step into an hour. Running everything "to see what's working" is the
same mistake: `wt results` already answers that from the runs on disk. `wt run`
refuses both: a regression must be earned by a focused pass on the current
artifact since the last regression, and every scenario that failed in its
latest regression needs a focused pass of its own.

Climb the ladder instead, and say what each rung is for:

```bash
# probe: replay the broken step on its recorded history (full-history runtimes)
uv run wt run --from-trace runs/lookup_order/.../<trace>.json \
  --question "does the schema error reach the model now?" --expect pass
# focused: the one scenario, a few runs
uv run wt run --scenario lookup_order --runs 3 \
  --question "does the fix hold across samples?" --expect pass
# regression: everything, once the fixes have earned it
uv run wt run --pack my_pack --runs 3 \
  --question "did the fix break anything else?" --expect pass
```

Any edit after a focused pass makes it stale. A sweep that runs out of budget
exits non-zero; shrink the experiment instead of raising the budget.

## JSON-stringified tool-call arguments

Contract A and Contract C both require tool-call arguments as JSON objects, not
JSON strings. A producer that emits `"arguments": "{\"query\":\"Bluewing\"}"`
does not produce a lossy warning; it produces a hard parse error. Fix the
exporter or endpoint at the boundary so the bench receives objects:

```json
{ "name": "client_lookup", "arguments": { "query": "Bluewing" } }
```

Validate the shape before importing or running:

```bash
uv run wt validate --strict incident.wtin.json
```

## Hand-rolled interchange emitters

Do not write an emitter by eyeballing a trace and hoping it matches Contract A.
The repo has a golden fixture corpus for valid, invalid, and lint-only
interchange envelopes. Conform to that corpus, then run strict validation in
producer CI:

```bash
uv run pytest tests/test_wt_validate.py -q
uv run wt validate --strict incident.wtin.json
```

The validation parser is the importer parser. If `wt validate --strict` is red,
`wt import` is not the next debugging step.

## Trusting reset without proving it

`reset_state()` and `/wt/reset` are load-bearing. If reset is incomplete, a run
can pass because it remembers a prior run, not because the agent solved the
scenario. A reset that returns before derived state is gone is still broken.

Use the packaged canary:

```bash
uv run wt doctor --runtime <your-runtime>
```

For hermetic CI without a live model, call `run_reset_canary(...,
probe_recall=False, state_probe=...)` from pytest and inspect the backing state
directly.

## Treating import output as authored

`wt import` deliberately emits a failing scenario. The importer can reconstruct
the prompt, observed tools, fixture, and provenance; it cannot decide what
should count as correct. A green imported scenario that nobody authored is a
bad import workflow, not a successful regression.

After import, review `IMPORTED.md`, replace the placeholder gate in
`scenario.py` or `scorer.py`, and only then add the scenario to a pack.

## Inventing a Contract C payload mapping

The `http_inject` runtime has one fixed wire contract: `POST /wt/inject` and
`POST /wt/reset`, with `wt_inject`, `session_id`, `text`, `timeout_s`, `reply`,
and ordered `tool_calls`. Do not add a configurable mapper for local field
names. The fixed shape is what prevents a bench from silently testing the wrong
payload.

Conform to Contract C exactly, then prove it:

```bash
WT_INJECT_URL=http://127.0.0.1:8647 uv run wt doctor --runtime http_inject
```
