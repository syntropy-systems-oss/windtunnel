"""Tests for Trace + Turn data types, serialization, hashing, and replay.

TDD red phase — these tests define the contract. They must fail before
any implementation exists.

Worker-path coverage note (carry-forward):
  The fixture-based tests build tool_call dicts in BOTH the OpenAI wire
  shape AND the flat shape. This is the scaffolding required so that
  when the real worker parse_job path is wired in, the test bed already
  exercises both shapes rather than only the OpenAI shape that
  session.json fixtures carry.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import pytest

from windtunnel.api.replay import replay
from windtunnel.api.trace import (
    TRACE_FORMAT_VERSION,
    Hash,
    Trace,
    TraceFormatError,
    Turn,
    compute_hash,
    load_trace,
    save_trace,
    storage_path,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _openai_tool_call(name: str, args: dict | str, call_id: str = "call_0") -> dict:
    """OpenAI wire shape — {id, type, function: {name, arguments}}."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args),
        },
    }


def _flat_tool_call(name: str, args: dict, call_id: str = "call_0") -> dict:
    """Flat shape — {id, name, args}, as emitted by some proxy/queue paths."""
    return {"id": call_id, "name": name, "args": args}


def _make_turn(
    role: str = "assistant",
    content: str = "Hello",
    tool_calls: list[dict] | None = None,
    tool_results: list[dict] | None = None,
    latency_ms: float = 120.5,
    rendered_prompt: str | None = None,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        latency_ms=latency_ms,
        rendered_prompt=rendered_prompt,
    )


def _make_trace(**overrides: Any) -> Trace:
    defaults: dict[str, Any] = dict(
        scenario_id="s01",
        agent_id="agent-bluewing",
        variant_id="baseline",
        model="test-model",
        quant="q4",
        sampler={"temperature": 0.7, "top_p": 1.0},
        started_at=_ts("2026-05-27T12:00:00+00:00"),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=[
            _make_turn(
                role="user",
                content="List storage for Bluewing",
                rendered_prompt=None,
            ),
            _make_turn(
                role="assistant",
                content="",
                tool_calls=[_openai_tool_call("order_query", {"client": "Bluewing"})],
                rendered_prompt="<|im_start|>user\nList storage for Bluewing<|im_end|>",
            ),
            _make_turn(
                role="tool",
                content='{"orders": []}',
                tool_results=[{"tool_call_id": "call_0", "content": '{"orders": []}'}],
            ),
            _make_turn(
                role="assistant",
                content="No orders found in storage.",
                rendered_prompt="<|im_start|>user\nList storage for Bluewing<|im_end|><|im_start|>assistant<|im_end|>",
            ),
        ],
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )
    defaults.update(overrides)
    return Trace(**defaults)


# ─── 1. Turn fields ────────────────────────────────────────────────────────────

class TestTurnFields:
    def test_rendered_prompt_hash_present_on_assistant_turns(self):
        """Assistant turns with rendered_prompt must have rendered_prompt_hash."""
        turn = _make_turn(
            role="assistant",
            rendered_prompt="<|im_start|>user\nHello<|im_end|>",
        )
        assert turn.rendered_prompt_hash is not None
        assert turn.rendered_prompt_hash.startswith("sha256:")

    def test_rendered_prompt_hash_none_when_no_prompt(self):
        """Non-assistant turns with no rendered_prompt have None hash."""
        turn = _make_turn(role="user", rendered_prompt=None)
        assert turn.rendered_prompt_hash is None

    def test_rendered_prompt_hash_algorithm(self):
        """Hash must be sha256 of the UTF-8 prompt, hex-encoded, prefixed sha256:."""
        prompt = "<|im_start|>user\nHello<|im_end|>"
        turn = _make_turn(role="assistant", rendered_prompt=prompt)
        expected = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
        assert turn.rendered_prompt_hash == expected

    def test_tool_calls_openai_shape_preserved(self):
        """OpenAI-shape tool_calls survive the Turn constructor unchanged."""
        tc = _openai_tool_call("order_query", {"client": "Bluewing"})
        turn = _make_turn(role="assistant", tool_calls=[tc])
        assert turn.tool_calls[0]["function"]["name"] == "order_query"

    def test_tool_calls_flat_shape_preserved(self):
        """Flat tool_calls survive the Turn constructor unchanged.

        The Turn type stores tool_calls as-received (both shapes).
        Normalization happens at render/replay time, mirroring the production worker.
        This ensures the trace faithfully records what the worker saw.
        """
        tc = _flat_tool_call("order_query", {"client": "Bluewing"})
        turn = _make_turn(role="assistant", tool_calls=[tc])
        assert turn.tool_calls[0]["name"] == "order_query"
        assert "function" not in turn.tool_calls[0]


# ─── 2. Trace fields ──────────────────────────────────────────────────────────

class TestTraceFields:
    def test_tool_schema_hash_format(self):
        trace = _make_trace()
        assert trace.tool_schema_hash.startswith("sha256:")

    def test_worker_warnings_default_empty(self):
        trace = _make_trace(worker_warnings=[])
        assert trace.worker_warnings == []

    def test_worker_warnings_stored(self):
        warnings = ["apply_chat_template raised ValueError: bad shape"]
        trace = _make_trace(worker_warnings=warnings)
        assert trace.worker_warnings == warnings

    def test_required_identity_fields(self):
        trace = _make_trace()
        assert trace.scenario_id == "s01"
        assert trace.agent_id == "agent-bluewing"
        assert trace.variant_id == "baseline"
        assert trace.model == "test-model"
        assert trace.quant == "q4"
        assert trace.sampler == {"temperature": 0.7, "top_p": 1.0}

    def test_run_id_auto_generated(self):
        """Each Trace gets a unique run_id if not supplied."""
        t1 = _make_trace()
        t2 = _make_trace()
        assert t1.run_id != t2.run_id

    def test_run_id_can_be_supplied(self):
        rid = str(uuid.uuid4())
        trace = _make_trace(run_id=rid)
        assert trace.run_id == rid


# ─── 3. Hash helpers ──────────────────────────────────────────────────────────

class TestHashHelpers:
    def test_compute_hash_returns_sha256_prefix(self):
        h = compute_hash("hello")
        assert h.startswith("sha256:")

    def test_same_content_same_hash(self):
        assert compute_hash("prompt A") == compute_hash("prompt A")

    def test_whitespace_change_different_hash(self):
        assert compute_hash("prompt A") != compute_hash("prompt A ")

    def test_hash_hex_length(self):
        # sha256 = 64 hex chars
        h = compute_hash("x")
        assert len(h) == len("sha256:") + 64

    def test_hash_is_type_alias(self):
        """Hash type alias is just str — no special class needed."""
        h: Hash = compute_hash("test")
        assert isinstance(h, str)


# ─── 4. JSON round-trip ───────────────────────────────────────────────────────

class TestJsonRoundTrip:
    def test_saved_trace_declares_schema_version(self, tmp_path):
        path = tmp_path / "trace.json"
        save_trace(_make_trace(), path)
        assert json.loads(path.read_text(encoding="utf-8"))["windtunnel_trace"] == (
            TRACE_FORMAT_VERSION
        )

    def test_unversioned_v08_trace_still_loads(self, tmp_path):
        payload = _make_trace()._to_dict()
        del payload["windtunnel_trace"]
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert load_trace(path).scenario_id == payload["scenario_id"]

    def test_future_trace_version_is_rejected(self, tmp_path):
        payload = _make_trace()._to_dict()
        payload["windtunnel_trace"] = TRACE_FORMAT_VERSION + 1
        path = tmp_path / "future.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TraceFormatError, match="unsupported"):
            load_trace(path)

    def test_basic_round_trip(self, tmp_path):
        trace = _make_trace()
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.scenario_id == trace.scenario_id
        assert loaded.run_id == trace.run_id
        assert len(loaded.turns) == len(trace.turns)

    def test_timestamps_preserved_exactly(self, tmp_path):
        """Timestamps must survive json round-trip without precision loss."""
        trace = _make_trace(
            started_at=_ts("2026-05-27T18:47:05.593944+00:00"),
            finished_at=_ts("2026-05-27T18:47:25.048257+00:00"),
        )
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.started_at == trace.started_at
        assert loaded.finished_at == trace.finished_at

    def test_unicode_in_content_preserved(self, tmp_path):
        """Unicode in turn content must survive without escaping or loss."""
        content = "日本語テスト — résumé — \U0001f600"
        trace = _make_trace(turns=[_make_turn(content=content)])
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.turns[0].content == content

    def test_tool_call_args_as_string_preserved(self, tmp_path):
        """tool_calls with arguments as JSON-string survive round-trip as string."""
        tc = _openai_tool_call("order_query", '{"client": "Bluewing"}')
        trace = _make_trace(turns=[
            _make_turn(role="assistant", tool_calls=[tc], rendered_prompt="x")
        ])
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        args = loaded.turns[0].tool_calls[0]["function"]["arguments"]
        assert args == '{"client": "Bluewing"}'

    def test_tool_call_args_as_dict_preserved(self, tmp_path):
        """tool_calls with arguments as dict survive round-trip as dict."""
        tc = _openai_tool_call("order_query", {"client": "Bluewing"})
        # force the arguments to be a dict (not json-encoded string)
        tc["function"]["arguments"] = {"client": "Bluewing"}
        trace = _make_trace(turns=[
            _make_turn(role="assistant", tool_calls=[tc], rendered_prompt="x")
        ])
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        args = loaded.turns[0].tool_calls[0]["function"]["arguments"]
        assert args == {"client": "Bluewing"}

    def test_flat_tool_call_round_trip(self, tmp_path):
        """Flat-shape tool_calls survive round-trip without morphing."""
        tc = _flat_tool_call("order_query", {"client": "Bluewing"})
        trace = _make_trace(turns=[
            _make_turn(role="assistant", tool_calls=[tc], rendered_prompt="x")
        ])
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        loaded_tc = loaded.turns[0].tool_calls[0]
        assert loaded_tc["name"] == "order_query"
        assert "function" not in loaded_tc

    def test_hashes_survive_round_trip(self, tmp_path):
        trace = _make_trace()
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.tool_schema_hash == trace.tool_schema_hash
        for orig, loaded_turn in zip(trace.turns, loaded.turns):
            assert orig.rendered_prompt_hash == loaded_turn.rendered_prompt_hash

    def test_worker_warnings_survive_round_trip(self, tmp_path):
        warnings = ["apply_chat_template raised ValueError"]
        trace = _make_trace(worker_warnings=warnings)
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.worker_warnings == warnings

    def test_json_is_valid_utf8_not_ascii_escaped(self, tmp_path):
        """save_trace must write actual Unicode, not \\uXXXX escapes."""
        content = "日本語テスト"
        trace = _make_trace(turns=[_make_turn(content=content)])
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        raw = path.read_text(encoding="utf-8")
        assert "日本語テスト" in raw  # real Unicode, not escaped

    def test_mcp_calls_default_empty(self):
        """mcp_calls defaults to [] when not supplied (in_memory runs)."""
        trace = _make_trace()
        assert trace.mcp_calls == []

    def test_mcp_calls_round_trip(self, tmp_path):
        """Server-witnessed call dicts survive save/load unchanged."""
        calls = [
            {"tool_name": "ops_client_lookup", "args": {"query": "Bluewing"},
             "result": '{"clients": []}', "timestamp_ms": 1000.5},
            {"tool_name": "ops_order_query", "args": {"client": "Bluewing"},
             "result": {"orders": []}, "timestamp_ms": 1001.0},
        ]
        trace = _make_trace(mcp_calls=calls)
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.mcp_calls == calls

    def test_old_trace_json_without_mcp_calls_loads(self, tmp_path):
        """Traces saved before the mcp_calls field existed must still load
        (defaulting to [] → trajectory falls back to transcript evidence)."""
        trace = _make_trace()
        d = trace._to_dict()
        del d["mcp_calls"]  # simulate a pre-mcp_calls trace file
        path = tmp_path / "old_trace.json"
        path.write_text(json.dumps(d), encoding="utf-8")
        loaded = load_trace(path)
        assert loaded.mcp_calls == []
        assert loaded.run_id == trace.run_id

    def test_observations_default_empty(self):
        """observations defaults to {} when no StateProbe was wired."""
        trace = _make_trace()
        assert trace.observations == {}

    def test_observations_round_trip(self, tmp_path):
        """External-state snapshots survive save/load unchanged — the property
        that lets world-state Policies re-score saved traces offline."""
        observations = {
            "github": {
                "branches": ["main", "fix/timeout"],
                "prs": [{"base": "main", "head": "fix/timeout",
                         "files": {"src/app.py": "patched"}}],
            },
        }
        trace = _make_trace(observations=observations)
        path = tmp_path / "trace.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.observations == observations

    def test_old_trace_json_without_observations_loads(self, tmp_path):
        """Traces saved before the observations field existed must still load
        (defaulting to {} → constraint scores on the evidence they do carry)."""
        trace = _make_trace()
        d = trace._to_dict()
        del d["observations"]  # simulate a pre-observations trace file
        path = tmp_path / "old_trace.json"
        path.write_text(json.dumps(d), encoding="utf-8")
        loaded = load_trace(path)
        assert loaded.observations == {}
        assert loaded.run_id == trace.run_id


# ─── 5. Storage path ─────────────────────────────────────────────────────────

class TestStoragePath:
    def test_path_contains_all_key_fields(self, tmp_path):
        trace = _make_trace(
            scenario_id="s01",
            agent_id="agent-bluewing",
            variant_id="baseline",
            model="test-model",
            quant="q4",
            started_at=_ts("2026-05-27T12:00:00+00:00"),
        )
        path = storage_path(trace, base_dir=tmp_path)
        parts = str(path)
        assert "s01" in parts
        assert "agent-bluewing" in parts
        assert "baseline" in parts
        assert "test-model" in parts
        assert "q4" in parts

    def test_path_ends_with_json(self, tmp_path):
        trace = _make_trace()
        path = storage_path(trace, base_dir=tmp_path)
        assert str(path).endswith(".json")

    def test_two_traces_different_timestamps_different_paths(self, tmp_path):
        t1 = _make_trace(started_at=_ts("2026-05-27T12:00:00+00:00"))
        t2 = _make_trace(started_at=_ts("2026-05-27T13:00:00+00:00"))
        p1 = storage_path(t1, base_dir=tmp_path)
        p2 = storage_path(t2, base_dir=tmp_path)
        assert p1 != p2

    def test_dot_dot_identity_component_cannot_escape_base_dir(self, tmp_path):
        """Regression: a plugin-authored scenario id of '..' previously wrote
        the trace outside the configured runs directory."""
        trace = _make_trace(scenario_id="..")

        path = storage_path(trace, base_dir=tmp_path)

        assert path.resolve().is_relative_to(tmp_path.resolve())
        assert ".." not in path.relative_to(tmp_path).parts


# ─── 6. Replay ────────────────────────────────────────────────────────────────

class TestReplay:
    def _stub_generate(self, turns: list[Turn]) -> list[Turn]:
        """Stub that returns a copy of each assistant turn unchanged."""
        return [copy.deepcopy(t) for t in turns]

    def test_replay_produces_trace(self):
        original = _make_trace()
        result = replay(
            original,
            variant_id="variant-b",
            generate=self._stub_generate,
        )
        assert isinstance(result, Trace)

    def test_replay_variant_id_updated(self):
        original = _make_trace(variant_id="baseline")
        result = replay(
            original,
            variant_id="variant-b",
            generate=self._stub_generate,
        )
        assert result.variant_id == "variant-b"

    def test_replay_scenario_and_agent_ids_preserved(self):
        original = _make_trace(scenario_id="s01", agent_id="agent-bluewing")
        result = replay(
            original,
            variant_id="variant-b",
            generate=self._stub_generate,
        )
        assert result.scenario_id == "s01"
        assert result.agent_id == "agent-bluewing"

    def test_replay_new_run_id(self):
        original = _make_trace()
        result = replay(
            original,
            variant_id="variant-b",
            generate=self._stub_generate,
        )
        assert result.run_id != original.run_id

    def test_replay_timestamps_differ(self):
        original = _make_trace()
        result = replay(
            original,
            variant_id="variant-b",
            generate=self._stub_generate,
        )
        # started_at should be a fresh timestamp, not the original
        assert result.started_at != original.started_at

    def test_replay_unchanged_variant_byte_identical_content(self):
        """Unchanged variant → same turn content + hashes (except timestamps + run_id)."""
        original = _make_trace()

        def identity_generate(turns: list[Turn]) -> list[Turn]:
            return [copy.deepcopy(t) for t in turns]

        result = replay(
            original,
            variant_id=original.variant_id,
            generate=identity_generate,
        )
        # Content of turns must match
        for orig_turn, new_turn in zip(original.turns, result.turns):
            assert orig_turn.content == new_turn.content
            assert orig_turn.tool_calls == new_turn.tool_calls
            assert orig_turn.rendered_prompt_hash == new_turn.rendered_prompt_hash

    def test_replay_model_and_quant_preserved(self):
        original = _make_trace(model="test-model", quant="q4")
        result = replay(
            original,
            variant_id="v2",
            generate=self._stub_generate,
        )
        assert result.model == original.model
        assert result.quant == original.quant

    def test_replay_tool_schema_hash_preserved(self):
        original = _make_trace()
        result = replay(
            original,
            variant_id="v2",
            generate=self._stub_generate,
        )
        assert result.tool_schema_hash == original.tool_schema_hash

    def test_replay_model_override(self):
        original = _make_trace(model="test-model")
        result = replay(
            original,
            variant_id="v2",
            generate=self._stub_generate,
            model="llama-3",
        )
        assert result.model == "llama-3"


# ─── 7. Worker-path coverage scaffold ────────────────────────────────────────

class TestWorkerPathScaffold:
    """Scaffold for worker-path coverage.

    The actual workers.llm.worker.parse_job is not importable here
    (it lives in a separate Python project with its own deps). These
    tests verify that the Trace type correctly handles both tool_call
    shapes that parse_job produces, mirroring the two-shape requirement
    of the production worker.

    When the real gateway driver is wired in, these tests should be
    extended to build payloads via the actual parse_job path rather than
    constructing dicts directly.
    """

    def test_openai_shape_tool_call_in_turn(self):
        """OpenAI shape (session.json exports, direct API) roundtrips correctly."""
        tc = _openai_tool_call("mcp_acme_ops_order_query", {"client": "Bluewing", "stage": "Storage"})
        turn = _make_turn(role="assistant", tool_calls=[tc], rendered_prompt="prompt")
        assert turn.tool_calls[0]["function"]["name"] == "mcp_acme_ops_order_query"
        assert turn.rendered_prompt_hash is not None

    def test_flat_shape_tool_call_in_turn(self):
        """Flat shape ({id, name, args}) stored faithfully."""
        tc = _flat_tool_call("mcp_acme_ops_order_query", {"client": "Bluewing", "stage": "Storage"})
        turn = _make_turn(role="assistant", tool_calls=[tc], rendered_prompt="prompt")
        # flat has no 'function' key
        assert "function" not in turn.tool_calls[0]
        assert turn.tool_calls[0]["name"] == "mcp_acme_ops_order_query"

    def test_both_shapes_in_same_trace(self, tmp_path):
        """A trace with mixed shapes (e.g. replay from session.json + live run) roundtrips."""
        tc_openai = _openai_tool_call("tool_a", {"x": 1}, call_id="call_0")
        tc_flat = _flat_tool_call("tool_b", {"y": 2}, call_id="call_1")
        turns = [
            _make_turn(role="assistant", tool_calls=[tc_openai], rendered_prompt="p1"),
            _make_turn(role="assistant", tool_calls=[tc_flat], rendered_prompt="p2"),
        ]
        trace = _make_trace(turns=turns)
        path = tmp_path / "mixed.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.turns[0].tool_calls[0]["function"]["name"] == "tool_a"
        assert "function" not in loaded.turns[1].tool_calls[0]
        assert loaded.turns[1].tool_calls[0]["name"] == "tool_b"

    def test_parse_job_style_payload_produces_valid_trace(self, tmp_path):
        """Simulate what parse_job produces and verify Trace handles it.

        parse_job builds Message(role, content, tool_calls=tuple(raw_list),
        tool_call_id). This test mirrors that construction so the Trace
        type is exercised on the exact shape that would arrive from the
        real worker path (without needing the workers package installed).
        """
        # Simulate parse_job output for an assistant turn with a tool call
        # (tuple of raw dicts, exactly as parse_job produces)
        raw_tool_calls = (
            {"id": "call_0", "type": "function",
             "function": {"name": "order_query", "arguments": '{"client": "Bluewing"}'}},
        )

        turns = [
            Turn(
                role="user",
                content="List storage",
                tool_calls=[],
                tool_results=[],
                latency_ms=0,
                rendered_prompt=None,
            ),
            Turn(
                role="assistant",
                content="",
                tool_calls=list(raw_tool_calls),  # list, as Turn expects
                tool_results=[],
                latency_ms=150.0,
                rendered_prompt="<|im_start|>user\nList storage<|im_end|>",
            ),
        ]
        trace = _make_trace(turns=turns)
        path = tmp_path / "parse_job_style.json"
        save_trace(trace, path)
        loaded = load_trace(path)
        assert loaded.turns[1].tool_calls[0]["function"]["name"] == "order_query"
        # arguments preserved as string (parse_job doesn't normalize)
        assert loaded.turns[1].tool_calls[0]["function"]["arguments"] == '{"client": "Bluewing"}'
