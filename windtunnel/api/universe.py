"""Recorded tool-universe fixture format and deterministic matching.

A universe file is the at-rest form of server-witnessed tool calls:
tool definitions plus ``MCPCall``-shaped recordings
(``tool_name``, ``args``, ``result``).  The format is intentionally boring
JSON so production traces, hand-authored fixtures, and record-mode test
runs all converge on the same artifact.

The module is pure format + matching policy.  It does not know how a tool
call arrived (FastMCP, a platform runtime, or a future importer) and it does
not start servers.  Concrete serving lives outside ``api/`` so scenario
authors can depend on the representation without pulling in a particular MCP
transport.

Forward tolerance follows ``Trace._from_dict`` discipline: required v1
fields are validated, optional fields get stable defaults, and additive
fields within v1 are ignored. Unknown format versions are rejected so an
older reader never silently misinterprets a future contract.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from windtunnel.spi.mcp_server import MCPCall

UNIVERSE_VERSION = 1
MISS_POLICIES = frozenset({"fail_call", "empty", "nearest", "synthesize"})
MATCH_MODES = frozenset({"stateless", "sequence"})

SynthesizeHook = Callable[[str, dict[str, Any], "Universe"], Any]


class UniverseFormatError(ValueError):
    """Raised when a universe file is not a valid Contract B fixture."""


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    """Return ``value`` as a plain dict or raise a fixture-focused error."""
    if not isinstance(value, dict):
        raise UniverseFormatError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    """Return ``value`` as a list or raise a fixture-focused error."""
    if not isinstance(value, list):
        raise UniverseFormatError(f"{label} must be a list")
    return value


def _json_key(value: Any) -> str:
    """Canonical JSON key used for deterministic equality.

    Contract B defines exact equality as JSON canonicalization with sorted
    keys.  Tool arguments should already be JSON values; ``default=repr`` is
    only a defensive fallback for a sloppy in-process runtime that passed an
    otherwise non-serializable scalar during a live lookup.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=repr,
    )


def normalize_tool_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize live or recorded tool arguments to Contract B's flat shape.

    ``MCPCall`` explicitly documents a dual-shape reality: some workers emit
    OpenAI tool-call objects while others emit flat ``{"args": ...}`` or just
    the argument dict.  Universe files store only the flat argument dict so
    matching is canonical and stable across runtimes.
    """
    if args is None:
        return {}
    data = dict(args)

    function = data.get("function")
    if isinstance(function, dict) and "arguments" in function:
        return _decode_arguments(function.get("arguments"))

    if "name" in data and isinstance(data.get("args"), dict):
        # Flat worker shape: {"name": "tool", "args": {...}}. Requiring the
        # "name" marker keeps a genuine tool parameter that happens to be
        # called "args" from being unwrapped in hand-authored fixtures.
        return dict(data["args"])

    if "arguments" in data and ("name" in data or "type" in data):
        # Some runtimes flatten OpenAI's function payload one level.
        return _decode_arguments(data.get("arguments"))

    return data


def _decode_arguments(value: Any) -> dict[str, Any]:
    """Decode a tool-call ``arguments`` field into a dict.

    OpenAI wire arguments are commonly a JSON string, but in-process tests
    and some runtimes pass a dict directly.  Non-object JSON normalizes to an
    empty dict because MCP tool arguments are object-shaped.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


@dataclass(frozen=True)
class UniverseTool:
    """One tool definition served by a recorded universe.

    ``input_schema`` is the schema offered to the agent.  ``result_schema`` is
    optional and is only used when a miss policy needs to produce a safe empty
    value.  ``mode`` defaults to stateless replay; sequence mode is an
    explicit per-tool opt-in for genuinely stateful recordings.
    """

    name: str
    input_schema: dict[str, Any]
    description: str = ""
    result_schema: dict[str, Any] | None = None
    mode: str = "stateless"

    @classmethod
    def _from_dict(cls, raw: Mapping[str, Any]) -> UniverseTool:
        d = _require_mapping(raw, "tools[*]")
        name = d.get("name")
        if not isinstance(name, str) or not name:
            raise UniverseFormatError("tools[*].name must be a non-empty string")
        input_schema = d.get("input_schema")
        if not isinstance(input_schema, dict):
            raise UniverseFormatError(f"tools[{name!r}].input_schema must be an object")
        result_schema = d.get("result_schema")
        if result_schema is not None and not isinstance(result_schema, dict):
            raise UniverseFormatError(f"tools[{name!r}].result_schema must be an object")
        description = d.get("description") or ""
        if not isinstance(description, str):
            raise UniverseFormatError(f"tools[{name!r}].description must be a string")
        mode = d.get("mode") or "stateless"
        if mode not in MATCH_MODES:
            raise UniverseFormatError(f"tools[{name!r}].mode must be one of {sorted(MATCH_MODES)}")
        return cls(
            name=name,
            description=description,
            input_schema=dict(input_schema),
            result_schema=dict(result_schema) if result_schema is not None else None,
            mode=mode,
        )

    def _to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.result_schema is not None:
            data["result_schema"] = self.result_schema
        if self.mode != "stateless":
            data["mode"] = self.mode
        return data


@dataclass(frozen=True)
class UniverseRecording:
    """One recorded tool-call/result pair at rest.

    The shape intentionally mirrors ``MCPCall`` minus runtime-only fields:
    no timestamp, no worker-local metadata, and no transport wrapper around
    ``args``.  The constructor path normalizes arguments to the flat fixture
    shape so live retries from different runtimes compare against one key.
    """

    tool_name: str
    args: dict[str, Any]
    result: Any

    @classmethod
    def _from_dict(cls, raw: Mapping[str, Any]) -> UniverseRecording:
        d = _require_mapping(raw, "recordings[*]")
        tool_name = d.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise UniverseFormatError("recordings[*].tool_name must be a non-empty string")
        if "args" not in d:
            raise UniverseFormatError(f"recordings[{tool_name!r}].args is required")
        if "result" not in d:
            raise UniverseFormatError(f"recordings[{tool_name!r}].result is required")
        args = normalize_tool_args(_require_mapping(d.get("args"), f"recordings[{tool_name!r}].args"))
        return cls(tool_name=tool_name, args=args, result=d.get("result"))

    def _to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "result": self.result,
        }


@dataclass(frozen=True)
class UniverseMatching:
    """Matching controls for a universe.

    ``on_miss`` is the universe default.  ``per_tool_on_miss`` overrides it
    for tools whose safety profile differs.  ``arg_keys`` enables the keyed
    match tier by naming the subset of arguments that identify a semantic
    lookup.  ``modes`` is the normalized home for per-tool stateless versus
    sequence behavior; loaders also accept ``tools[*].mode`` and object-valued
    ``arg_keys`` entries to stay tolerant of hand-authored fixtures.
    """

    on_miss: str = "fail_call"
    arg_keys: dict[str, list[str]] = field(default_factory=dict)
    per_tool_on_miss: dict[str, str] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def _from_dict(cls, raw: Mapping[str, Any] | None) -> UniverseMatching:
        if raw is None:
            return cls()
        d = _require_mapping(raw, "matching")
        on_miss = d.get("on_miss") or "fail_call"
        _validate_policy(on_miss, "matching.on_miss")

        arg_keys: dict[str, list[str]] = {}
        modes: dict[str, str] = {}
        raw_arg_keys = d.get("arg_keys") or {}
        raw_arg_keys = _require_mapping(raw_arg_keys, "matching.arg_keys")
        for tool_name, value in raw_arg_keys.items():
            if not isinstance(tool_name, str):
                raise UniverseFormatError("matching.arg_keys keys must be strings")
            if isinstance(value, dict):
                keys_value = value.get("keys", value.get("arg_keys", []))
                mode_value = value.get("mode")
                if mode_value is not None:
                    _validate_mode(mode_value, f"matching.arg_keys[{tool_name!r}].mode")
                    modes[tool_name] = mode_value
            else:
                keys_value = value
            keys = _require_list(keys_value, f"matching.arg_keys[{tool_name!r}]")
            if not all(isinstance(k, str) for k in keys):
                raise UniverseFormatError(f"matching.arg_keys[{tool_name!r}] must contain strings")
            arg_keys[tool_name] = list(keys)

        per_tool_on_miss: dict[str, str] = {}
        raw_per_tool = d.get("per_tool_on_miss") or {}
        raw_per_tool = _require_mapping(raw_per_tool, "matching.per_tool_on_miss")
        for tool_name, policy in raw_per_tool.items():
            if not isinstance(tool_name, str):
                raise UniverseFormatError("matching.per_tool_on_miss keys must be strings")
            _validate_policy(policy, f"matching.per_tool_on_miss[{tool_name!r}]")
            per_tool_on_miss[tool_name] = policy

        raw_modes = d.get("modes") or {}
        raw_modes = _require_mapping(raw_modes, "matching.modes")
        for tool_name, mode in raw_modes.items():
            if not isinstance(tool_name, str):
                raise UniverseFormatError("matching.modes keys must be strings")
            _validate_mode(mode, f"matching.modes[{tool_name!r}]")
            modes[tool_name] = mode

        return cls(
            on_miss=on_miss,
            arg_keys=arg_keys,
            per_tool_on_miss=per_tool_on_miss,
            modes=modes,
        )

    def _to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "on_miss": self.on_miss,
            "arg_keys": self.arg_keys,
            "per_tool_on_miss": self.per_tool_on_miss,
        }
        if self.modes:
            data["modes"] = self.modes
        return data

    def policy_for(self, tool_name: str) -> str:
        """Return the effective divergence policy for ``tool_name``."""
        return self.per_tool_on_miss.get(tool_name, self.on_miss)

    def mode_for(self, tool_name: str, tools: Mapping[str, UniverseTool]) -> str:
        """Return the effective replay mode for ``tool_name``."""
        if tool_name in self.modes:
            return self.modes[tool_name]
        tool = tools.get(tool_name)
        return tool.mode if tool is not None else "stateless"


def _validate_policy(value: Any, label: str) -> None:
    if value not in MISS_POLICIES:
        raise UniverseFormatError(f"{label} must be one of {sorted(MISS_POLICIES)}")


def _validate_mode(value: Any, label: str) -> None:
    if value not in MATCH_MODES:
        raise UniverseFormatError(f"{label} must be one of {sorted(MATCH_MODES)}")


@dataclass(frozen=True)
class Universe:
    """Validated in-memory representation of a ``*.universe.json`` file."""

    windtunnel_universe: int
    tools: list[UniverseTool]
    recordings: list[UniverseRecording]
    matching: UniverseMatching = field(default_factory=UniverseMatching)

    @classmethod
    def _from_dict(cls, raw: Mapping[str, Any]) -> Universe:
        d = _require_mapping(raw, "universe")
        version = d.get("windtunnel_universe")
        if type(version) is not int:
            raise UniverseFormatError("windtunnel_universe must be an integer")
        if version != UNIVERSE_VERSION:
            raise UniverseFormatError(
                f"unsupported windtunnel_universe version {version}; "
                f"expected {UNIVERSE_VERSION}"
            )

        tools = [UniverseTool._from_dict(t) for t in _require_list(d.get("tools"), "tools")]
        tool_names = [t.name for t in tools]
        duplicates = sorted({name for name in tool_names if tool_names.count(name) > 1})
        if duplicates:
            raise UniverseFormatError(f"duplicate tool definitions: {duplicates}")

        recordings = [
            UniverseRecording._from_dict(r)
            for r in _require_list(d.get("recordings"), "recordings")
        ]
        defined = set(tool_names)
        unknown_recordings = sorted({r.tool_name for r in recordings if r.tool_name not in defined})
        if unknown_recordings:
            raise UniverseFormatError(f"recordings reference undefined tools: {unknown_recordings}")

        return cls(
            windtunnel_universe=version,
            tools=tools,
            recordings=recordings,
            matching=UniverseMatching._from_dict(d.get("matching")),
        )

    def _to_dict(self) -> dict[str, Any]:
        return {
            "windtunnel_universe": self.windtunnel_universe,
            "tools": [tool._to_dict() for tool in self.tools],
            "recordings": [recording._to_dict() for recording in self.recordings],
            "matching": self.matching._to_dict(),
        }

    @property
    def tool_map(self) -> dict[str, UniverseTool]:
        """Return tools keyed by canonical tool name."""
        return tools_by_name(self.tools)

    def recording_indices_for(self, tool_name: str) -> list[int]:
        """Return recording indexes for ``tool_name`` in fixture order."""
        return [i for i, rec in enumerate(self.recordings) if rec.tool_name == tool_name]


def tools_by_name(tools: Iterable[UniverseTool]) -> dict[str, UniverseTool]:
    """Return a name-keyed map without changing tool object identity."""
    return {tool.name: tool for tool in tools}


def load_universe(path: str | Path) -> Universe:
    """Load and validate a Contract B universe file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Universe._from_dict(data)


def save_universe(universe: Universe, path: str | Path) -> None:
    """Write a universe file as human-readable JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(universe._to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def freeze_universe(
    call_log: Iterable[MCPCall],
    tools: Iterable[UniverseTool | Mapping[str, Any]],
    *,
    matching: UniverseMatching | Mapping[str, Any] | None = None,
    path: str | Path | None = None,
) -> Universe:
    """Freeze witnessed calls and tool definitions into a universe.

    The recording shape is deliberately ``MCPCall`` minus runtime-only
    fields.  Arguments are normalized on write so the resulting file never
    stores OpenAI wire wrappers, while results are carried through verbatim.
    Pass ``path`` to write the file in the same step.
    """
    tool_defs = [_coerce_tool(t) for t in tools]
    match = _coerce_matching(matching)
    recordings = [
        UniverseRecording(
            tool_name=call.tool_name,
            args=normalize_tool_args(call.args),
            result=call.result,
        )
        for call in call_log
    ]
    universe = Universe(
        windtunnel_universe=UNIVERSE_VERSION,
        tools=tool_defs,
        recordings=recordings,
        matching=match,
    )
    # Re-run validation on the emitted shape so freeze catches undefined tools
    # before a fixture lands on disk.
    universe = Universe._from_dict(universe._to_dict())
    if path is not None:
        save_universe(universe, path)
    return universe


def _coerce_tool(value: UniverseTool | Mapping[str, Any]) -> UniverseTool:
    if isinstance(value, UniverseTool):
        return value
    return UniverseTool._from_dict(value)


def _coerce_matching(value: UniverseMatching | Mapping[str, Any] | None) -> UniverseMatching:
    if value is None:
        return UniverseMatching()
    if isinstance(value, UniverseMatching):
        return value
    return UniverseMatching._from_dict(value)


def find_exact_recording(
    universe: Universe,
    tool_name: str,
    args: dict[str, Any],
    candidate_indices: Iterable[int] | None = None,
) -> int | None:
    """Return the first exact recording index for ``tool_name`` and ``args``."""
    wanted = _json_key(args)
    indices = candidate_indices
    if indices is None:
        indices = universe.recording_indices_for(tool_name)
    for idx in indices:
        rec = universe.recordings[idx]
        if rec.tool_name == tool_name and _json_key(rec.args) == wanted:
            return idx
    return None


def find_keyed_recording(
    universe: Universe,
    tool_name: str,
    args: dict[str, Any],
    candidate_indices: Iterable[int] | None = None,
) -> int | None:
    """Return the first keyed recording index, or ``None`` when inapplicable."""
    if tool_name not in universe.matching.arg_keys:
        return None
    keys = universe.matching.arg_keys[tool_name]
    indices = candidate_indices
    if indices is None:
        indices = universe.recording_indices_for(tool_name)
    for idx in indices:
        rec = universe.recordings[idx]
        if rec.tool_name != tool_name:
            continue
        if all(
            key in args
            and key in rec.args
            and _json_key(args[key]) == _json_key(rec.args[key])
            for key in keys
        ):
            return idx
    return None


def find_nearest_recording(
    universe: Universe,
    tool_name: str,
    args: dict[str, Any],
    candidate_indices: Iterable[int] | None = None,
) -> int | None:
    """Return the nearest recording index for a miss.

    Nearest is intentionally explainable: count exactly matching key/value
    pairs for the same tool, choose the highest count, and break ties by the
    recording order already present in the file.
    """
    indices = list(candidate_indices) if candidate_indices is not None else universe.recording_indices_for(tool_name)
    best_idx: int | None = None
    best_score = -1
    for idx in indices:
        rec = universe.recordings[idx]
        if rec.tool_name != tool_name:
            continue
        score = 0
        for key, value in args.items():
            if key in rec.args and _json_key(value) == _json_key(rec.args[key]):
                score += 1
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx


def empty_result_for(tool: UniverseTool | None) -> Any:
    """Return the schema-shaped empty value for the ``empty`` miss policy."""
    schema = tool.result_schema if tool is not None else None
    schema_type = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(schema_type, list):
        for candidate in ("array", "object", "string"):
            if candidate in schema_type:
                schema_type = candidate
                break
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {}
    if schema_type == "string":
        return ""
    return ""


def fail_call_result(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return Contract B's structured error for an unrecorded call."""
    return {
        "error": "no_recorded_result",
        "tool": tool_name,
        "args": args,
    }
