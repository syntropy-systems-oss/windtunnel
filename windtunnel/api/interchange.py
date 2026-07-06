"""Contract A trace interchange envelope.

Wind Tunnel's native ``Trace`` JSON is an internal storage format.  The
``*.wtin.json`` interchange envelope is the public import boundary: producers
copy their OTel GenAI-shaped messages into it, and ``wt import`` turns that
neutral record into a reviewable scenario skeleton.

This module is deliberately only parsing and validation.  It does not interpret
the pinned OTel mapping string, it does not decide scenario correctness, and it
does not import any runtime or MCP transport code.  Forward tolerance follows
``Trace._from_dict`` discipline: required v1 fields are validated, optional
fields get stable defaults, and unknown future fields are ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INTERCHANGE_VERSION = 1


class InterchangeFormatError(ValueError):
    """Raised when a ``*.wtin.json`` file is not a valid Contract A envelope."""


@dataclass(frozen=True)
class TextPart:
    """An OTel GenAI ``text`` message part."""

    content: str


@dataclass(frozen=True)
class ToolCallPart:
    """An OTel GenAI ``tool_call`` message part."""

    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ToolCallResponsePart:
    """An OTel GenAI ``tool_call_response`` message part."""

    id: str
    response: Any


InterchangePart = TextPart | ToolCallPart | ToolCallResponsePart


@dataclass(frozen=True)
class InterchangeMessage:
    """One OTel GenAI message with typed parts."""

    role: str
    parts: list[InterchangePart]

    def text_content(self) -> str:
        """Return text parts joined in source order."""
        return "\n".join(part.content for part in self.parts if isinstance(part, TextPart))


@dataclass(frozen=True)
class InterchangeToolDefinition:
    """Tool schema carried by a producer, if available."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    result_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class InterchangeWitnessedCall:
    """Server-side tool evidence carried by ``witnessed_calls``."""

    tool_name: str
    args: dict[str, Any]
    result: Any


@dataclass(frozen=True)
class InterchangeTrace:
    """Validated in-memory representation of a Contract A envelope."""

    windtunnel_interchange: int
    otel_genai_mapping: str
    session: dict[str, Any]
    messages: list[InterchangeMessage]
    source: dict[str, Any] | None = None
    tool_definitions: list[InterchangeToolDefinition] | None = None
    witnessed_calls: list[InterchangeWitnessedCall] | None = None

    @property
    def model(self) -> str:
        """Return the required ``session.model`` value."""
        return str(self.session["model"])

    @property
    def source_ref(self) -> str | None:
        """Return the optional opaque origin ref, when it is a string."""
        if self.source is None:
            return None
        ref = self.source.get("ref")
        return ref if isinstance(ref, str) and ref else None


def load_interchange(path: str | Path) -> InterchangeTrace:
    """Load and validate a Contract A ``*.wtin.json`` envelope."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InterchangeFormatError(f"invalid JSON: {exc}") from exc
    return parse_interchange(raw)


def parse_interchange(raw: Any) -> InterchangeTrace:
    """Validate ``raw`` and return an ``InterchangeTrace``.

    Unknown fields are ignored.  The version is accepted for any positive
    integer so older importers can read newer envelopes whose required v1
    surface is still present.
    """
    d = _require_mapping(raw, "interchange")

    version = d.get("windtunnel_interchange")
    if type(version) is not int or version < 1:
        raise InterchangeFormatError("windtunnel_interchange must be a positive integer")

    mapping = d.get("otel_genai_mapping", "")
    if not isinstance(mapping, str):
        raise InterchangeFormatError("otel_genai_mapping must be a string")

    session = _require_mapping(d.get("session"), "session")
    model = session.get("model")
    if not isinstance(model, str) or not model:
        raise InterchangeFormatError("session.model must be a non-empty string")

    source = d.get("source")
    if source is not None:
        source = _require_mapping(source, "source")

    return InterchangeTrace(
        windtunnel_interchange=version,
        otel_genai_mapping=mapping,
        source=dict(source) if source is not None else None,
        session=dict(session),
        messages=[
            _parse_message(message, i)
            for i, message in enumerate(_require_list(d.get("messages"), "messages"))
        ],
        tool_definitions=_parse_tool_definitions(d),
        witnessed_calls=_parse_witnessed_calls(d),
    )


def _parse_message(raw: Any, index: int) -> InterchangeMessage:
    d = _require_mapping(raw, f"messages[{index}]")
    role = d.get("role")
    if not isinstance(role, str) or not role:
        raise InterchangeFormatError(f"messages[{index}].role must be a non-empty string")
    parts = [
        _parse_part(part, index, part_index)
        for part_index, part in enumerate(_require_list(d.get("parts"), f"messages[{index}].parts"))
    ]
    return InterchangeMessage(role=role, parts=parts)


def _parse_part(raw: Any, message_index: int, part_index: int) -> InterchangePart:
    label = f"messages[{message_index}].parts[{part_index}]"
    d = _require_mapping(raw, label)
    part_type = d.get("type")
    if part_type == "text":
        content = d.get("content")
        if not isinstance(content, str):
            raise InterchangeFormatError(f"{label}.content must be a string")
        return TextPart(content=content)

    if part_type == "tool_call":
        call_id = d.get("id")
        name = d.get("name")
        if not isinstance(call_id, str) or not call_id:
            raise InterchangeFormatError(f"{label}.id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise InterchangeFormatError(f"{label}.name must be a non-empty string")
        if "arguments" not in d:
            raise InterchangeFormatError(f"{label}.arguments is required")
        return ToolCallPart(id=call_id, name=name, arguments=d.get("arguments"))

    if part_type == "tool_call_response":
        call_id = d.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise InterchangeFormatError(f"{label}.id must be a non-empty string")
        if "response" not in d:
            raise InterchangeFormatError(f"{label}.response is required")
        return ToolCallResponsePart(id=call_id, response=d.get("response"))

    raise InterchangeFormatError(
        f"{label}.type must be one of ['text', 'tool_call', 'tool_call_response']"
    )


def _parse_tool_definitions(d: dict[str, Any]) -> list[InterchangeToolDefinition] | None:
    if "tool_definitions" not in d or d.get("tool_definitions") is None:
        return None

    definitions: list[InterchangeToolDefinition] = []
    for index, raw in enumerate(_require_list(d.get("tool_definitions"), "tool_definitions")):
        label = f"tool_definitions[{index}]"
        td = _require_mapping(raw, label)
        name = td.get("name")
        if not isinstance(name, str) or not name:
            raise InterchangeFormatError(f"{label}.name must be a non-empty string")

        description = td.get("description") or ""
        if not isinstance(description, str):
            raise InterchangeFormatError(f"{label}.description must be a string")

        input_schema = td.get("input_schema")
        if input_schema is not None and not isinstance(input_schema, dict):
            raise InterchangeFormatError(f"{label}.input_schema must be an object")

        result_schema = td.get("result_schema")
        if result_schema is not None and not isinstance(result_schema, dict):
            raise InterchangeFormatError(f"{label}.result_schema must be an object")

        definitions.append(
            InterchangeToolDefinition(
                name=name,
                description=description,
                input_schema=dict(input_schema) if input_schema is not None else None,
                result_schema=dict(result_schema) if result_schema is not None else None,
            )
        )
    return definitions


def _parse_witnessed_calls(d: dict[str, Any]) -> list[InterchangeWitnessedCall] | None:
    if "witnessed_calls" not in d or d.get("witnessed_calls") is None:
        return None

    calls: list[InterchangeWitnessedCall] = []
    for index, raw in enumerate(_require_list(d.get("witnessed_calls"), "witnessed_calls")):
        label = f"witnessed_calls[{index}]"
        call = _require_mapping(raw, label)
        tool_name = call.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise InterchangeFormatError(f"{label}.tool_name must be a non-empty string")
        args = _require_mapping(call.get("args"), f"{label}.args")
        if "result" not in call:
            raise InterchangeFormatError(f"{label}.result is required")
        calls.append(
            InterchangeWitnessedCall(
                tool_name=tool_name,
                args=dict(args),
                result=call.get("result"),
            )
        )
    return calls


def build_envelope(
    *,
    model: str,
    messages: list[dict[str, Any]],
    provider: str | None = None,
    otel_genai_mapping: str = "",
    source: dict[str, Any] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    witnessed_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reference emitter for a version-1 Contract A ``*.wtin.json`` envelope.

    This is documentation in code form, not a general-purpose builder. Its
    job is to show, in the host language producers are most likely to also
    be reading (Python), exactly which keys a valid envelope needs and where
    they go. A producer writing an emitter in another language should treat
    this function's body as the spec and the golden fixtures under
    ``tests/fixtures/interchange/`` plus ``wt validate`` as the conformance
    corpus to check their own output against — not this function itself,
    which they cannot call from Go, TypeScript, etc.

    ``messages`` is a list of already-shaped OTel GenAI message dicts, e.g.::

        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "..."},
                {"type": "tool_call", "id": "call_1", "name": "client_lookup",
                 "arguments": {"query": "Bluewing"}},
                {"type": "tool_call_response", "id": "call_1",
                 "response": {"email": "ops@bluewing.example"}},
            ],
        }

    ``tool_definitions`` and ``witnessed_calls``, when given, are lists of
    already-shaped dicts matching the corresponding sections of the Contract
    A schema (see the module docstring and ``parse_interchange``). This
    function does not validate its inputs — call ``parse_interchange`` on
    the result if you want that, which is exactly what ``wt validate`` does.

    Deliberately minimal: no builder class, no options explosion. Producers
    with more complex needs should construct the envelope dict directly and
    validate it with ``wt validate`` or ``parse_interchange``.
    """
    session: dict[str, Any] = {"model": model}
    if provider is not None:
        session["provider"] = provider

    envelope: dict[str, Any] = {
        "windtunnel_interchange": INTERCHANGE_VERSION,
        "otel_genai_mapping": otel_genai_mapping,
        "session": session,
        "messages": messages,
    }
    if source is not None:
        envelope["source"] = source
    if tool_definitions is not None:
        envelope["tool_definitions"] = tool_definitions
    if witnessed_calls is not None:
        envelope["witnessed_calls"] = witnessed_calls
    return envelope


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    """Return ``value`` as a plain dict or raise an envelope-focused error."""
    if not isinstance(value, dict):
        raise InterchangeFormatError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    """Return ``value`` as a list or raise an envelope-focused error."""
    if not isinstance(value, list):
        raise InterchangeFormatError(f"{label} must be a list")
    return value


__all__ = [
    "INTERCHANGE_VERSION",
    "InterchangeFormatError",
    "InterchangeMessage",
    "InterchangePart",
    "InterchangeToolDefinition",
    "InterchangeTrace",
    "InterchangeWitnessedCall",
    "TextPart",
    "ToolCallPart",
    "ToolCallResponsePart",
    "build_envelope",
    "load_interchange",
    "parse_interchange",
]
