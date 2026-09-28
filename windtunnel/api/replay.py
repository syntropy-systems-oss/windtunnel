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

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from windtunnel.api.trace import Trace, Turn

GenerateFn = Callable[[list[Turn]], list[Turn]]

REPLAY_PREFIX_MARKER = "replay_prefix:"


@dataclass(frozen=True)
class HistoryPrefix:
    """Recorded turns a probe freezes as history before the live turns.

    ``turns`` are sent to the model exactly as the runner builds history for a
    live multi-turn run (user and assistant content, in order) and are copied
    to the start of the new trace. ``source`` names where they came from (the
    source run id) for the ``replay_prefix:`` worker warning.
    """

    turns: tuple[Turn, ...]
    source: str

    @property
    def marker(self) -> str:
        return f"{REPLAY_PREFIX_MARKER} source={self.source} turns={len(self.turns)}"

    def messages(self) -> list[dict[str, str]]:
        """The prefix as OpenAI-format history messages (content only)."""
        return [
            {"role": turn.role, "content": turn.content}
            for turn in self.turns
            if turn.role in ("user", "assistant")
        ]


def prefix_length(trace: Trace) -> int:
    """Number of frozen prefix turns at the start of ``trace`` (0 for ordinary runs)."""
    for warning in trace.worker_warnings:
        if not str(warning).startswith(REPLAY_PREFIX_MARKER):
            continue
        for field_text in str(warning).split():
            if field_text.startswith("turns="):
                try:
                    return max(0, int(field_text.removeprefix("turns=")))
                except ValueError:
                    return 0
    return 0


def scoring_view(trace: Trace) -> Trace:
    """Return ``trace`` without its frozen replay prefix, for scoring.

    A probe records the frozen history at the start of its trace so the file
    reads as the whole conversation, but that history is the ORIGINAL run's
    behavior: its tool calls, forbidden calls, and policy-relevant turns must
    neither satisfy nor fail the probe. Every scorer reads this view. A trace
    without a ``replay_prefix:`` marker is returned unchanged.
    """
    skip = prefix_length(trace)
    if skip == 0:
        return trace
    return dataclasses.replace(trace, turns=list(trace.turns[skip:]))


def with_prefix(view: Trace, full: Trace) -> Trace:
    """Put ``full``'s frozen prefix back in front of a scored ``view``."""
    skip = prefix_length(full)
    if skip == 0:
        return view
    return dataclasses.replace(view, turns=list(full.turns[:skip]) + list(view.turns))


def split_at_user_turn(trace: Trace, from_turn: int | None) -> tuple[HistoryPrefix, list[str]]:
    """Split ``trace`` before its ``from_turn``-th user turn (1-based).

    Returns the frozen prefix (every turn before that user turn) and the user
    turns to run live from there. ``None`` means the last user turn, the one a
    scenario scores. Raises ValueError when the trace has no such user turn.
    """
    user_positions = [index for index, turn in enumerate(trace.turns) if turn.role == "user"]
    if not user_positions:
        raise ValueError(f"trace {trace.run_id} has no user turns to replay from")
    count = len(user_positions)
    chosen = count if from_turn is None else from_turn
    if not 1 <= chosen <= count:
        raise ValueError(
            f"--from-turn {from_turn} is out of range: trace {trace.run_id} has "
            f"{count} user turn(s) (1..{count})"
        )
    cut = user_positions[chosen - 1]
    live = [trace.turns[position].content for position in user_positions[chosen - 1:]]
    prefix = HistoryPrefix(turns=tuple(trace.turns[:cut]), source=trace.run_id)
    return prefix, live


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
