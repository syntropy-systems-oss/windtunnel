"""Replay: re-run a captured Trace against a different variant.

The generate() callback is intentionally stubbed here — a runtime driver
supplies the real gateway/model invocation. This module owns only the trace
machinery: thread the user turns through generate(), collect the new
assistant turns, produce a second Trace for diff.

generate() contract:
    Input:  list[Turn] — the turns seen so far (user + prior assistant)
    Output: list[Turn] — the new assistant (+ tool) turns produced by
            the variant under test.

For testing without the gateway, pass a lambda or stub that returns a
copy of the original assistant turns (identity generate); a real driver
replaces the stub.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from windtunnel.api.trace import Trace, Turn

GenerateFn = Callable[[list[Turn]], list[Turn]]


def replay(
    original: Trace,
    variant_id: str,
    generate: GenerateFn,
    model: str | None = None,
    quant: str | None = None,
) -> Trace:
    """Re-run *original* against a new variant, producing a second Trace.

    Steps:
    1. Extract the user-side turns (role != "assistant") as the seed.
    2. Call generate(turns_so_far) to get the new assistant turns.
    3. Collect all turns (user seed + new assistant turns) into a Trace
       with a fresh run_id and timestamps.

    Identity semantics: if generate returns structurally identical turns
    (same content, tool_calls, rendered_prompt) to the originals, the
    resulting Trace will have identical turn content — meaning
    rendered_prompt_hash values match — and will differ only in
    timestamps and run_id. In other words, an identity replay is
    byte-identical except timestamps and run ids.

    model / quant overrides: optional — allows running the same scenario
    against a different model/quant combination (sampler-sensitivity
    dim). Defaults to original.model / original.quant.
    """
    now = datetime.now(UTC)

    new_turns = generate(list(original.turns))

    return Trace(
        run_id=str(uuid.uuid4()),
        scenario_id=original.scenario_id,
        agent_id=original.agent_id,
        variant_id=variant_id,
        model=model if model is not None else original.model,
        quant=quant if quant is not None else original.quant,
        sampler=dict(original.sampler),
        started_at=now,
        finished_at=datetime.now(UTC),
        turns=new_turns,
        tool_schema_hash=original.tool_schema_hash,
        worker_warnings=[],
    )
