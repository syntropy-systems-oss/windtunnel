"""Trace + Turn data types, serialization, hashing, and storage paths.

Design decisions:
- Pure stdlib (json, dataclasses, hashlib, pathlib, datetime, uuid).
  No external deps so this package stays open-sourceable without
  requiring a specific LLM framework.
- tool_calls stored as-received (both the OpenAI wire shape AND the flat
  {id, name, args} shape some workers emit). The Trace faithfully records
  what the worker saw. Normalization happens at render/replay time, in
  the consumer's own prompt builder.
- Hash format: sha256:<hex> — prefix makes algorithm-migration detectable
  in stored traces without breaking string comparisons.
- rendered_prompt_hash is computed automatically from rendered_prompt
  if provided; None otherwise (non-assistant turns, or turns where the
  rendered prompt was not captured).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Type alias — just str, no special class. Hash values always start with
# the algorithm prefix so a future migration from sha256 is detectable.
Hash = str


def compute_hash(content: str) -> Hash:
    """Compute a sha256 hash of a UTF-8 string, prefixed with 'sha256:'."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass
class Turn:
    """A single turn in an agent conversation trace.

    tool_calls: stored in whatever shape was received — either OpenAI
        wire shape ({id, type, function: {name, arguments}}) or
        flat shape ({id, name, args}). Both shapes are preserved
        faithfully. Callers that need a normalized form must do their
        own normalization.

    rendered_prompt: the exact string the chat template produced for
        this assistant turn. Only meaningful for role=="assistant" turns
        where the worker had a tokenizer available.

    rendered_prompt_hash: auto-computed from rendered_prompt on init.
        None when rendered_prompt is None.
    """
    role: str
    content: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    latency_ms: float
    rendered_prompt: str | None = None

    # Computed on post_init — not passed by callers.
    rendered_prompt_hash: Hash | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.rendered_prompt is not None:
            object.__setattr__(
                self,
                "rendered_prompt_hash",
                compute_hash(self.rendered_prompt),
            )

    def _to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "latency_ms": self.latency_ms,
            "rendered_prompt": self.rendered_prompt,
            "rendered_prompt_hash": self.rendered_prompt_hash,
        }

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> Turn:
        turn = cls(
            role=d["role"],
            content=d["content"],
            tool_calls=d.get("tool_calls") or [],
            tool_results=d.get("tool_results") or [],
            latency_ms=d.get("latency_ms", 0.0),
            rendered_prompt=d.get("rendered_prompt"),
        )
        # rendered_prompt_hash is computed by __post_init__ from rendered_prompt.
        # If the stored hash differs (shouldn't happen), we trust the stored value
        # so that historical traces remain intact even if the hash algorithm changes.
        stored_hash = d.get("rendered_prompt_hash")
        if stored_hash is not None and stored_hash != turn.rendered_prompt_hash:
            object.__setattr__(turn, "rendered_prompt_hash", stored_hash)
        return turn


@dataclass
class Trace:
    """Full trace for one scenario run.

    Identity tuple: (scenario_id, agent_id, variant_id, model, quant, started_at)
    The storage_path() function uses this tuple to produce a versioned
    path under windtunnel/runs/ so runs are diff-able across deploys.

    mcp_calls: the SERVER-witnessed tool calls for this run — what the mock
        MCP server itself recorded (MCPHandle.call_log()), normalized to
        plain dicts ({"tool_name", "args", "result", "timestamp_ms", optional
        "extra"}) so the trace stays pure-stdlib-serializable. This is independent evidence:
        turns[*].tool_calls is the agent's own account of what it did;
        mcp_calls is what actually reached the tool server. Empty when no
        logging MCP server was in play (e.g. the in_memory runtime).

    tool_schema_hash: identity of the tool surface the run's MCP servers
        offered the agent. Computed from served_tool_definitions() where a
        handle provides it (full name/description/schema entries), falling
        back to served_tools() names; order-sensitive by design — a
        reordered manifest is a changed surface. compute_hash("[]") means
        "no tool servers, truthfully"; None means the manifest was UNKNOWN
        (some handle exposed no tool metadata) — honest absence, never a
        fabricated identity.

    observations: end-of-run snapshots of EXTERNAL (non-MCP) world state,
        captured by a StateProbe (spi/state_probe.py) after the final turn
        and before scoring. Keyed by evidence source, e.g.
        {"github": {"branches": [...], "prs": [...]}}. This completes the
        evidence triad: turns[*].tool_calls = the agent's account,
        mcp_calls = what reached the tool server, observations = the world
        the agent left behind. Policy predicates read it like any other
        trace field, so verdicts that depend on world state survive
        offline re-scoring. Empty when no probe was wired.

    surface: the agent's prompt surface as captured once per run — after
        reset, before the first send — from a handle implementing
        describe_surface() (spi/agent_runtime.py). A dict with "status":
        "reported" (endpoint's account of its configured surface — a
        driver cannot verify what the model saw through an inject
        boundary, so never treat as ground truth), "rendered" (worker-side
        truth), "unavailable" (honest absence), or "invalid" (failed
        validation; the malformed payload is never stored). None when the
        handle has no surface introspection at all — absent capability,
        distinct from a probed "unavailable". A captured surface IS the
        system prompt: treat trace files embedding one as sensitively as
        the prompt itself.
    """
    scenario_id: str
    agent_id: str
    variant_id: str
    model: str
    quant: str
    sampler: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    turns: list[Turn]
    tool_schema_hash: Hash | None
    worker_warnings: list[str] = field(default_factory=list)
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    surface: dict[str, Any] | None = None
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def _to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "agent_id": self.agent_id,
            "variant_id": self.variant_id,
            "model": self.model,
            "quant": self.quant,
            "sampler": self.sampler,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "turns": [t._to_dict() for t in self.turns],
            "tool_schema_hash": self.tool_schema_hash,
            "worker_warnings": self.worker_warnings,
            "mcp_calls": self.mcp_calls,
            "observations": self.observations,
            "surface": self.surface,
        }

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> Trace:
        return cls(
            run_id=d["run_id"],
            scenario_id=d["scenario_id"],
            agent_id=d["agent_id"],
            variant_id=d["variant_id"],
            model=d["model"],
            quant=d["quant"],
            sampler=d.get("sampler") or {},
            started_at=datetime.fromisoformat(d["started_at"]),
            finished_at=datetime.fromisoformat(d["finished_at"]),
            turns=[Turn._from_dict(t) for t in d.get("turns", [])],
            tool_schema_hash=d.get("tool_schema_hash"),
            worker_warnings=d.get("worker_warnings") or [],
            # Fields added post-1st-schema: old traces don't carry them.
            # Default to empty so historical runs still load (and score via
            # the evidence they do carry).
            mcp_calls=d.get("mcp_calls") or [],
            observations=d.get("observations") or {},
            surface=d.get("surface"),
        )


def save_trace(trace: Trace, path: Path) -> None:
    """Write a Trace to a JSON file.

    Writes actual Unicode (ensure_ascii=False) rather than \\uXXXX escapes
    so diffs are human-readable and grep works on content.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trace._to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_trace(path: Path) -> Trace:
    """Load a Trace from a JSON file produced by save_trace()."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Trace._from_dict(data)


def is_trace_json_path(path: Path) -> bool:
    """Return True when a JSON path is a trace, not a sidecar.

    Sidecar rule: any ``*.json`` whose stem contains a dot is never a trace
    (for example ``run.score.json`` or ``run.debrief.json``).
    """
    path = Path(path)
    return path.suffix == ".json" and "." not in path.stem


def storage_path(trace: Trace, base_dir: Path | None = None) -> Path:
    """Compute a versioned storage path for a trace.

    Path schema:
        <base_dir>/<scenario_id>/<agent_id>/<variant_id>/<model>/<quant>/<timestamp>_<run_id[:8]>.json

    The timestamp comes from started_at (UTC) formatted as YYYYMMDDTHHMMSSuuuuuuZ
    so lexicographic sort = chronological sort. run_id prefix guards against
    sub-second collisions.

    base_dir defaults to the runs/ directory adjacent to this file's package.
    """
    if base_dir is None:
        base_dir = Path(__file__).parent.parent / "runs"
    base_dir = Path(base_dir)

    # Sanitize fields for use in filesystem paths. In particular, ``.`` and
    # ``..`` must never survive as components: scenario packs are extensible,
    # and their identity strings must not be able to escape base_dir.
    def _safe(s: str) -> str:
        component = "".join(
            char if char.isalnum() or char in "-._" else "_"
            for char in str(s)
        )
        if component in {"", ".", ".."}:
            return "_" * max(1, len(component))
        return component

    ts = trace.started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    short_id = trace.run_id[:8]
    filename = f"{ts}_{short_id}.json"

    candidate = (
        base_dir
        / _safe(trace.scenario_id)
        / _safe(trace.agent_id)
        / _safe(trace.variant_id)
        / _safe(trace.model)
        / _safe(trace.quant)
        / filename
    )
    resolved_base = base_dir.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_base):
        raise ValueError(f"trace storage path escaped base directory: {candidate}")
    return candidate
