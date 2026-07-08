---
description: "Task guide for capturing prompt-surface goldens and gating steering changes with wt surface."
---
# Surface Goldens

`Trace.surface` freezes the prompt surface a run used: system instructions, tool
definitions, and framework-injected text beyond the conversation itself. It
exists for forensics. Every run should carry the steering surface it ran against
when the runtime can report one, so later triage does not reconstruct prompts
from git archaeology, container images, and deployment memory.

Surface capture is evidence about the run, not the behavior being tested. A
broken surface probe should be loud, but it should not destroy an expensive
bench run.

## The four surface states

`Trace.surface` is captured once per run, after `reset_state()` and before the
first user turn. A captured surface block has one of four `status` values:

| Status | Meaning |
|---|---|
| `rendered` | Driver-side truth. The runtime rendered the prompt surface itself, so the block is the surface the model was actually driven with. Scripted or worker-side runtimes, including `in_memory` when given a scripted surface, can use this label. |
| `reported` | Endpoint-reported configuration, usually from an external Contract C runtime via `/wt/surface`. A driver cannot verify what the model saw through an inject boundary, so `reported` is permanent labeling, not a transitional state waiting to become truth. |
| `unavailable` | Honest absence. The handle or endpoint cannot report a surface. For Contract C, a 404 or 501 from `/wt/surface` conforms and records this state. |
| `invalid` | The surface route existed but returned a malformed response, raised, or failed validation. This is a probe-level failure: the run proceeds, but surface gates treat it as hard failure. Strict gate, resilient run. |

If a handle has no surface-introspection method at all, the trace may carry no
surface block. That is different from a probed `status: unavailable`, but both
mean there is no surface to record as a golden.

## Record, diff, check

The `wt surface` command probes a runtime without running scenarios or making a
model call. It provisions the runtime, resets it, asks for the surface, tears it
down, and compares the result with a golden.

Record the first golden:

```bash
uv run wt surface record --runtime <your-runtime> --golden windtunnel/surface.golden.json
```

Review changes during development:

```bash
uv run wt surface diff --runtime <your-runtime> --golden windtunnel/surface.golden.json
```

Gate CI:

```bash
uv run wt surface check --runtime <your-runtime> --golden windtunnel/surface.golden.json
```

Use `--soul PATH` when the runtime should be probed with the same SOUL/system
prompt override you pass to `wt run`. Use `--label LABEL` when you want the probe
variant recorded under a specific label. The default golden path is
`surface.golden.json`; pass `--golden` so teams can put goldens beside the bench
configuration they govern.

The golden format is hashes at its core:

```json
{
  "windtunnel_surface_golden": 1,
  "status": "reported",
  "system_instructions": "sha256:...",
  "tool_order": ["lookup_invoice", "draft_note"],
  "tool_definitions": {
    "lookup_invoice": "sha256:...",
    "draft_note": "sha256:..."
  },
  "extra_segments": {
    "tool_progress": "sha256:..."
  }
}
```

No prompt text is stored by default. That is the point: hash-only goldens are
committable even when the real prompt surface is private IP.

`--store-text` is an explicit opt-in for teams whose prompts are public and who
want textual PR review:

```bash
uv run wt surface record --runtime <your-runtime> --golden windtunnel/surface.golden.json --store-text
```

That embeds the complete prompt surface in the golden under a human-facing text
sidecar plus a sensitivity warning. Treat that file as sensitively as the system
prompt itself. Comparison still reads hashes only; the text is for humans.

## Semantics to keep straight

A surface hash change means: run the bench before merge. It is a tripwire for
steering drift.

An unchanged hash proves nothing. Harness code, model behavior, tool
implementation, upstream latency, and fixture state can all move while the prompt
text stays identical. Passing `wt surface check` is never a skip token for the
bench.

`wt surface diff` is informative. It prints per-segment changes and exits 0 after
a successful comparison, even when hashes changed.

`wt surface check` is the gate. It exits nonzero on any changed hash, on an
invalid current surface, or on an absent current surface when the golden promises
one. There is deliberately no warn-vs-error knob. If the golden says "this runtime
has a surface," then failing to produce that surface is a change requiring
attention.

## CI wiring

A minimal CI shape is:

```yaml
jobs:
  surface:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run wt surface check --runtime <your-runtime> --golden windtunnel/surface.golden.json
```

On failure, do not bless the new hash reflexively. Review the diff, run the bench
against the changed surface, and then re-record the golden:

```bash
uv run wt surface diff --runtime <your-runtime> --golden windtunnel/surface.golden.json
uv run wt run --runtime <your-runtime> --runs 5 --label surface-review
uv run wt surface record --runtime <your-runtime> --golden windtunnel/surface.golden.json
```

Endpoint authors implementing `/wt/surface` should follow the Contract C surface
shape in [Design 0002: inject protocol](design/0002-inject-protocol.md#surface-introspection-optional).
