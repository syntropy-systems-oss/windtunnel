"""Reset-isolation canary — a packaged, nonce-based conformance probe.

The scariest silent failure in any driver is an incomplete reset: state
leaking between scenarios makes every score a lie (see api/state_reset.py
for a real incident — a search index answering from a deleted transcript,
not the fixture under test).

`run_reset_canary()` automates the check by hand: mint a random nonce,
seed it into a session, call `reset_state()`, then open a *different*
session and probe for the nonce coming back. If it does, isolation is
broken and the canary proves it.

The claim is deliberately asymmetric, and the result is worded to match:
a recalled nonce **proves** contamination (`leaked=True` is a red X you
can point at). A clean run does **not** prove isolation — the nonce may
have leaked somewhere the probe turns never happened to query (the
search-index incident class). This converts one class of leak into a
red X; passing is evidence, not proof. Pass `state_probe` to also inspect
stateful backends directly and catch that class too.

Two intended homes:
  - Recall mode (`probe_recall=True`, the default) is the bring-up check
    for a box with a live model — it seeds, resets, then asks a fresh
    session to recall the nonce. This is what `wt doctor` runs.
  - Hermetic mode (`probe_recall=False`) is for CI runners without a live
    model: seeding still uses `send()` (fine against a stubbed model —
    it only needs ingestion, not a coherent reply), but reset is verified
    by scanning `state_probe`'s post-reset snapshot directly. No probe
    turns, no second session, nothing calls `send()` after `reset_state()`.
    This is the pytest-only path — see `run_reset_canary`'s docstring.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from windtunnel.spi.agent_runtime import AgentConfig, AgentRuntime
from windtunnel.spi.state_probe import StateProbe

_DEFAULT_PROBE_TEXTS: tuple[str, ...] = (
    "What did I just tell you?",
    "Tell me the code I gave you earlier.",
)


@dataclass
class CanaryResult:
    """Outcome of one reset-isolation canary run.

    passed:   True iff no leak was observed. NOT proof of isolation —
              see the module docstring's asymmetry note.
    leaked:   True iff the nonce was recovered post-reset. This IS proof
              of contamination.
    nonce:    the UUID hex minted for this run (useful for debugging a
              failure by grepping raw logs).
    evidence: the post-reset replies/observations that contained the
              nonce. Empty when clean.
    detail:   human-readable verdict, worded to match the asymmetric claim.
    """
    passed: bool
    leaked: bool
    nonce: str
    evidence: list[str] = field(default_factory=list)
    detail: str = ""


def _extract_reply(response: dict[str, Any]) -> str:
    """Extract assistant text from an AgentHandle.send() response.

    Mirrors windtunnel.api.runner._extract_reply — tolerates the same
    response shapes accepted by the SPI (see spi/agent_runtime.py
    AgentHandle.send): OpenAI chat-completions, flat message, or
    wrapped message. Missing/None content normalizes to "".
    """
    msg: dict[str, Any] = {}
    choices = response.get("choices")
    if choices:
        msg = choices[0].get("message") or {}
    elif isinstance(response.get("message"), dict):
        msg = response["message"]
    elif "choices" not in response:
        msg = response  # flat shape: the response IS the message
    content = msg.get("content") or ""
    return str(content)


def run_reset_canary(
    runtime: AgentRuntime,
    config: AgentConfig | None = None,
    *,
    mcps: list | None = None,
    probe_texts: list[str] | None = None,
    state_probe: StateProbe | None = None,
    probe_recall: bool = True,
) -> CanaryResult:
    """Run the reset-isolation canary against one runtime.

    Recall mode (`probe_recall=True`, the default) is the bring-up check
    for a box with a live model: provision a handle, seed a random nonce
    into session A, call `reset_state()`, then drive probe turns in a
    *different* session B and scan every reply for the nonce. Any hit
    proves a leak. If `state_probe` is given, its post-reset snapshot is
    scanned too — catching the class of leak (e.g. a search index) that
    transcript probing alone misses. This is what `wt doctor` runs.

    Hermetic mode (`probe_recall=False`) is for CI runners without a live
    model: `state_probe` becomes REQUIRED (a `ValueError` is raised
    immediately if it is None) because a canary that cannot observe
    anything must refuse to run rather than report a vacuous pass. The
    flow is seed (session A, still via `send()` — fine against a stubbed
    model, since seeding only needs ingestion, not a coherent reply) →
    `reset_state()` → scan `state_probe`'s post-reset snapshot for the
    nonce. There are no probe turns and no session B; nothing calls
    `send()` after `reset_state()` in this mode. Use this from pytest in
    a driver repo's CI where no live model is available.

    A clean result (`passed=True`) is evidence the driver did not leak
    through the channels this canary happened to probe. It is NOT a
    proof of isolation — see the module docstring. In hermetic mode this
    is narrower still: only store-visible state was scanned, recall was
    never probed, and the result wording says so.

    For continuous in-pack isolation assertions, use
    ``windtunnel.hooks.state_probe.StateProbeHook``; this ``state_probe``
    parameter remains the doctor/seeded-nonce canary path and is not
    deprecated.

    Raises whatever `handle.send()` raises during probing (wrapped with
    context) rather than silently reporting a pass: a broken probe must
    never look like a clean canary. `handle.teardown()` is always called,
    with exceptions swallowed, matching run_scenario()'s behavior.
    """
    if not probe_recall and state_probe is None:
        raise ValueError(
            "run_reset_canary(probe_recall=False) requires a state_probe: "
            "a hermetic canary that cannot observe anything must refuse to "
            "run rather than report a vacuous pass."
        )

    config = config or AgentConfig()
    texts = list(probe_texts) if probe_texts else list(_DEFAULT_PROBE_TEXTS)
    nonce = uuid.uuid4().hex
    nonce_lower = nonce.lower()

    handle = runtime.provision(config, mcps=mcps)
    try:
        session_a = str(uuid.uuid4())
        seed_prompt = (
            f"Remember this code for me: {nonce}. Repeat it back to confirm."
        )
        handle.send([{"role": "user", "content": seed_prompt}], session_a)

        handle.reset_state()

        evidence: list[str] = []

        if probe_recall:
            session_b = str(uuid.uuid4())
            assert session_b != session_a  # canary invariant: distinct sessions

            for text in texts:
                try:
                    response = handle.send(
                        [{"role": "user", "content": text}], session_b
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"reset canary: send() raised while probing for leaked "
                        f"state (nonce={nonce}): {exc}"
                    ) from exc
                reply = _extract_reply(response)
                if nonce_lower in reply.lower():
                    evidence.append(reply)

        if state_probe is not None:
            snapshot = state_probe.capture()
            serialized = str(snapshot)
            if nonce_lower in serialized.lower():
                evidence.append(serialized)

        leaked = bool(evidence)
        if leaked:
            detail = (
                "leak proven: nonce recalled after reset — reset_state() "
                "did not isolate this session from the prior one"
            )
        else:
            detail = (
                "no leak observed (not proof of isolation) — the nonce "
                "was not recovered by the probes run, but state may have "
                "leaked somewhere they didn't query"
            )
        if not probe_recall:
            detail += " (hermetic mode: stores scanned, recall not probed)"
        return CanaryResult(
            passed=not leaked,
            leaked=leaked,
            nonce=nonce,
            evidence=evidence,
            detail=detail,
        )
    finally:
        try:
            handle.teardown()
        except Exception:
            pass
