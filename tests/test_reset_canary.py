"""Reset-isolation canary tests.

Fakes are modeled on tests/test_runtime_conformance.py idioms (a minimal
AgentHandle-shaped object + a runtime that provisions it), not the real
InMemoryRuntime — the canary needs handles whose send()/reset_state()
behavior differs deliberately (leaky vs. clean) rather than scripted
replies.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from windtunnel.api.canary import CanaryResult, run_reset_canary
from windtunnel.spi.agent_runtime import AgentConfig

# ─── Fakes ─────────────────────────────────────────────────────────────────

class _LeakyHandle:
    """Deliberately broken: reset_state() does NOT wipe session history,
    and send() echoes back any nonce it has ever seen, regardless of
    session_id. Models a driver whose reset is a no-op.
    """

    def __init__(self) -> None:
        self.seen_nonces: list[str] = []
        self.reset_count = 0
        self.teardown_count = 0
        self.sessions_seen: list[str] = []

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self.sessions_seen.append(session_id)
        text = messages[-1]["content"]
        # Record any nonce-shaped content the seeding turn contains.
        for word in text.replace(":", " ").replace(".", " ").split():
            if len(word) == 32:  # uuid4().hex length
                self.seen_nonces.append(word)
        # Echo back everything ever seen — the leak.
        reply = " ".join(self.seen_nonces) if self.seen_nonces else "ok"
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}

    def reset_state(self) -> None:
        self.reset_count += 1
        # Deliberately does NOT clear self.seen_nonces — the bug under test.

    def teardown(self) -> None:
        self.teardown_count += 1


class _CleanHandle:
    """Well-behaved: reset_state() wipes everything; post-reset probes
    never see the nonce.
    """

    def __init__(self) -> None:
        self.seen_nonces: list[str] = []
        self.reset_count = 0
        self.teardown_count = 0
        self.sessions_seen: list[str] = []

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self.sessions_seen.append(session_id)
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "I don't have that."}}
            ]
        }

    def reset_state(self) -> None:
        self.reset_count += 1

    def teardown(self) -> None:
        self.teardown_count += 1


class _RaisingHandle:
    """send() raises during the probe phase — must surface as an error,
    never a silent pass.
    """

    def __init__(self) -> None:
        self.teardown_count = 0
        self._calls = 0

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self._calls += 1
        if self._calls == 1:
            # Seeding turn succeeds.
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        raise RuntimeError("upstream exploded")

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        self.teardown_count += 1


class _FakeRuntime:
    """Runtime that always provisions the same pre-built handle."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def provision(self, config: AgentConfig, mcps: list | None = None) -> Any:
        return self._handle


# ─── Tests ─────────────────────────────────────────────────────────────────

class TestLeakyHandle:
    def test_leak_detected(self) -> None:
        handle = _LeakyHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(runtime, AgentConfig())

        assert isinstance(result, CanaryResult)
        assert result.leaked is True
        assert result.passed is False
        assert result.evidence != []
        assert "leak proven" in result.detail.lower()

    def test_teardown_called_even_on_failure(self) -> None:
        handle = _LeakyHandle()
        runtime = _FakeRuntime(handle)
        run_reset_canary(runtime, AgentConfig())
        assert handle.teardown_count == 1

    def test_distinct_session_ids_before_and_after_reset(self) -> None:
        handle = _LeakyHandle()
        runtime = _FakeRuntime(handle)
        run_reset_canary(runtime, AgentConfig())
        # First send() is the seeding turn (session A); subsequent sends
        # are the probe turns (session B). They must differ.
        session_a = handle.sessions_seen[0]
        probe_sessions = set(handle.sessions_seen[1:])
        assert probe_sessions
        assert session_a not in probe_sessions
        # All probe turns share one session id.
        assert len(probe_sessions) == 1


class TestCleanHandle:
    def test_no_leak_detected(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(runtime, AgentConfig())

        assert result.leaked is False
        assert result.passed is True
        assert result.evidence == []

    def test_detail_states_not_proof_of_isolation(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(runtime, AgentConfig())
        assert "not proof of isolation" in result.detail.lower()

    def test_teardown_called_on_success(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)
        run_reset_canary(runtime, AgentConfig())
        assert handle.teardown_count == 1

    def test_nonce_is_a_uuid_hex(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(runtime, AgentConfig())
        # Must round-trip as a valid uuid4 hex — never a memorable phrase.
        assert uuid.UUID(hex=result.nonce)


class TestStateProbePath:
    def test_clean_transcript_but_leaky_probe_snapshot_is_proven_leak(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)

        # The nonce is minted inside run_reset_canary, so to exercise the
        # "clean transcript, dirty index" class we wire a probe that echoes
        # back whatever the seeding turn wrote — like a search index that
        # still has the nonce even though reset_state() cleared the
        # conversation itself.
        class _EchoingProbe:
            def __init__(self, source_handle: _CleanHandle) -> None:
                self._handle = source_handle
                self.capture_count = 0

            def capture(self) -> dict[str, Any]:
                self.capture_count += 1
                # Recover the nonce from the seeding turn the clean handle
                # saw (transcript itself was wiped from the agent's replies,
                # but the "index" still has it — the incident class).
                return {"index": {"cached_turn": self._handle.last_seed_text}}

            def reset(self) -> None:
                pass

        # Give _CleanHandle a way to remember the seed text (index leak).
        handle.last_seed_text = ""
        original_send = handle.send

        def send_and_record(messages, session_id):
            if len(messages) == 1 and "Remember this code" in messages[0]["content"]:
                handle.last_seed_text = messages[0]["content"]
            return original_send(messages, session_id)

        handle.send = send_and_record  # type: ignore[method-assign]

        probe = _EchoingProbe(handle)
        result = run_reset_canary(runtime, AgentConfig(), state_probe=probe)

        assert result.leaked is True
        assert result.passed is False
        assert probe.capture_count == 1
        assert any(result.nonce in ev for ev in result.evidence)

    def test_state_probe_not_called_when_omitted(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(runtime, AgentConfig())
        assert result.passed is True


class TestRaisingHandle:
    def test_send_raising_during_probe_surfaces_as_error(self) -> None:
        handle = _RaisingHandle()
        runtime = _FakeRuntime(handle)
        with pytest.raises(RuntimeError, match="reset canary"):
            run_reset_canary(runtime, AgentConfig())

    def test_teardown_still_called_when_probe_raises(self) -> None:
        handle = _RaisingHandle()
        runtime = _FakeRuntime(handle)
        with pytest.raises(RuntimeError):
            run_reset_canary(runtime, AgentConfig())
        assert handle.teardown_count == 1


class TestCustomProbeTexts:
    def test_custom_probe_texts_are_used(self) -> None:
        handle = _LeakyHandle()
        runtime = _FakeRuntime(handle)
        result = run_reset_canary(
            runtime, AgentConfig(), probe_texts=["Custom probe question?"]
        )
        assert result.leaked is True


# ─── Hermetic (recall-free) mode ────────────────────────────────────────────

class _SeedRecordingHandle:
    """Minimal handle for hermetic-mode tests: records the seed prompt text
    so a state_probe fake can "echo" it back (modeling a store that still
    holds the nonce post-reset), and never needs a coherent recall reply
    since hermetic mode never sends a probe turn.
    """

    def __init__(self) -> None:
        self.last_seed_text = ""
        self.teardown_count = 0

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self.last_seed_text = messages[-1]["content"]
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def reset_state(self) -> None:
        pass

    def teardown(self) -> None:
        self.teardown_count += 1


class _EchoingHermeticProbe:
    """capture() either echoes the seed text (leaking store) or returns an
    unrelated snapshot (clean store), depending on construction.
    """

    def __init__(self, handle: _SeedRecordingHandle, *, leaking: bool) -> None:
        self._handle = handle
        self._leaking = leaking
        self.capture_count = 0

    def capture(self) -> dict[str, Any]:
        self.capture_count += 1
        if self._leaking:
            return {"store": {"cached": self._handle.last_seed_text}}
        return {"store": {"cached": "nothing interesting"}}

    def reset(self) -> None:
        pass


class _OrderRecordingHandle:
    """Records send()/reset_state() calls IN ORDER, so a test can prove
    hermetic mode issues no send() after reset_state() — the invariant
    that distinguishes hermetic mode from recall mode.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.teardown_count = 0

    def send(self, messages: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
        self.events.append(f"send:{session_id}")
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def reset_state(self) -> None:
        self.events.append("reset")

    def teardown(self) -> None:
        self.teardown_count += 1


class _StaticProbe:
    """A state_probe whose capture() always returns the same snapshot."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self.capture_count = 0

    def capture(self) -> dict[str, Any]:
        self.capture_count += 1
        return self._snapshot

    def reset(self) -> None:
        pass


class TestHermeticMode:
    def test_clean_stores_pass_with_hermetic_wording(self) -> None:
        handle = _SeedRecordingHandle()
        runtime = _FakeRuntime(handle)
        probe = _EchoingHermeticProbe(handle, leaking=False)

        result = run_reset_canary(
            runtime, AgentConfig(), probe_recall=False, state_probe=probe
        )

        assert result.passed is True
        assert result.leaked is False
        assert result.evidence == []
        assert "hermetic mode" in result.detail.lower()
        assert "recall not probed" in result.detail.lower()
        assert probe.capture_count == 1

    def test_nonce_in_probe_snapshot_is_proven_leak(self) -> None:
        handle = _SeedRecordingHandle()
        runtime = _FakeRuntime(handle)
        probe = _EchoingHermeticProbe(handle, leaking=True)

        result = run_reset_canary(
            runtime, AgentConfig(), probe_recall=False, state_probe=probe
        )

        assert result.leaked is True
        assert result.passed is False
        assert "leak proven" in result.detail.lower()
        assert "hermetic mode" in result.detail.lower()
        assert any(result.nonce in ev for ev in result.evidence)

    def test_probe_recall_false_without_state_probe_raises_value_error(self) -> None:
        handle = _CleanHandle()
        runtime = _FakeRuntime(handle)

        with pytest.raises(ValueError, match="state_probe"):
            run_reset_canary(runtime, AgentConfig(), probe_recall=False)

    def test_hermetic_mode_never_sends_after_reset(self) -> None:
        handle = _OrderRecordingHandle()
        runtime = _FakeRuntime(handle)
        probe = _StaticProbe({"store": "clean"})

        result = run_reset_canary(
            runtime, AgentConfig(), probe_recall=False, state_probe=probe
        )

        assert result.passed is True
        # Exactly one send() (the seeding turn), followed by reset — nothing
        # after it. This is the hermetic-mode invariant: no probe turns, no
        # session B, no send() calls once reset_state() has run.
        assert handle.events[-1] == "reset"
        assert handle.events.count("reset") == 1
        assert sum(1 for e in handle.events if e.startswith("send:")) == 1

    def test_hermetic_mode_teardown_still_called(self) -> None:
        handle = _SeedRecordingHandle()
        runtime = _FakeRuntime(handle)
        probe = _StaticProbe({"store": "clean"})
        run_reset_canary(runtime, AgentConfig(), probe_recall=False, state_probe=probe)
        assert handle.teardown_count == 1
