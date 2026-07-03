"""Mechanical skeleton generation for Contract A imports.

``wt import`` is intentionally a skeleton generator.  A trace proves what the
agent did; it does not prove what the agent should have done.  This module
therefore fills in only mechanical artifacts:

- user prompts copied from the transcript;
- recorded tool results frozen into a Contract B universe;
- commented-out trajectory suggestions from observed tool names;
- a failing outcome scorer stub with candidate facts copied from the final
  assistant text.

Everything judgmental is left as an explicit TODO in the generated files.
"""
from __future__ import annotations

import json
import pprint
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from windtunnel.api.interchange import (
    InterchangeFormatError,
    InterchangeMessage,
    InterchangeToolDefinition,
    InterchangeTrace,
    ToolCallPart,
    ToolCallResponsePart,
)
from windtunnel.api.universe import UniverseMatching, UniverseTool, freeze_universe, save_universe
from windtunnel.spi.mcp_server import MCPCall

STUB_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


@dataclass(frozen=True)
class ImportResult:
    """Summary of a generated scenario skeleton."""

    out_dir: Path
    scenario_path: Path
    universe_path: Path
    scorer_path: Path
    imported_path: Path
    evidence_source: str
    stubbed_tool_schemas: list[str]
    recordings: int


@dataclass(frozen=True)
class _Evidence:
    """Tool evidence selected for the universe fixture."""

    source: str
    calls: list[MCPCall]
    observed_tool_names: list[str]
    unpaired_tool_call_ids: list[str]
    unpaired_tool_response_ids: list[str]


def write_imported_scenario(
    envelope: InterchangeTrace,
    out_dir: str | Path,
    *,
    scenario_name: str | None = None,
) -> ImportResult:
    """Write the four ``wt import`` artifacts for ``envelope``.

    The caller owns usage-policy checks such as whether a non-empty output
    directory may be overwritten.  This function assumes it can create or
    replace the known generated files under ``out_dir``.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    evidence = _select_evidence(envelope)
    tools, stubbed = _universe_tools(envelope.tool_definitions, evidence.observed_tool_names)
    universe_path = out_path / "fixture.universe.json"
    universe = freeze_universe(
        evidence.calls,
        tools,
        matching=UniverseMatching(on_miss="fail_call"),
    )
    save_universe(universe, universe_path)

    name = scenario_name or _scenario_name(out_path, envelope.source_ref)
    final_assistant_text = _final_assistant_text(envelope.messages)
    user_turns = _user_turns(envelope.messages)
    requires_tool_use = bool(evidence.observed_tool_names or _transcript_tool_names(envelope.messages))

    scenario_path = out_path / "scenario.py"
    scenario_path.write_text(
        _render_scenario_py(
            scenario_name=name,
            user_turns=user_turns,
            observed_tool_names=evidence.observed_tool_names,
            requires_tool_use=requires_tool_use,
            source_ref=envelope.source_ref,
        ),
        encoding="utf-8",
    )

    scorer_path = out_path / "scorer.py"
    scorer_path.write_text(
        _render_scorer_py(final_assistant_text),
        encoding="utf-8",
    )

    imported_path = out_path / "IMPORTED.md"
    imported_path.write_text(
        _render_imported_md(
            envelope=envelope,
            evidence=evidence,
            user_turns=user_turns,
            stubbed_tool_schemas=stubbed,
            final_assistant_text=final_assistant_text,
            requires_tool_use=requires_tool_use,
        ),
        encoding="utf-8",
    )

    return ImportResult(
        out_dir=out_path,
        scenario_path=scenario_path,
        universe_path=universe_path,
        scorer_path=scorer_path,
        imported_path=imported_path,
        evidence_source=evidence.source,
        stubbed_tool_schemas=stubbed,
        recordings=len(evidence.calls),
    )


def _select_evidence(envelope: InterchangeTrace) -> _Evidence:
    """Prefer server-witnessed calls, else reconstruct transcript pairs."""
    if envelope.witnessed_calls is not None:
        calls = [
            MCPCall(
                tool_name=call.tool_name,
                args=call.args,
                result=call.result,
                timestamp_ms=float(index),
            )
            for index, call in enumerate(envelope.witnessed_calls)
        ]
        return _Evidence(
            source="witnessed_calls",
            calls=calls,
            observed_tool_names=_unique(call.tool_name for call in envelope.witnessed_calls),
            unpaired_tool_call_ids=[],
            unpaired_tool_response_ids=[],
        )

    calls, observed, unpaired_calls, unpaired_responses = _reconstruct_calls(envelope.messages)
    return _Evidence(
        source="reconstructed",
        calls=calls,
        observed_tool_names=observed,
        unpaired_tool_call_ids=unpaired_calls,
        unpaired_tool_response_ids=unpaired_responses,
    )


@dataclass(frozen=True)
class _PendingToolCall:
    """Transcript tool call awaiting a matching response part."""

    id: str
    name: str
    args: dict[str, Any]


def _reconstruct_calls(
    messages: list[InterchangeMessage],
) -> tuple[list[MCPCall], list[str], list[str], list[str]]:
    """Pair ``tool_call`` and ``tool_call_response`` parts by id."""
    pending_by_id: dict[str, list[_PendingToolCall]] = {}
    response_by_id: dict[str, list[Any]] = {}
    ordered_calls: list[_PendingToolCall] = []
    observed_tools: list[str] = []

    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                pending = _PendingToolCall(
                    id=part.id,
                    name=part.name,
                    args=_arguments_to_dict(part.arguments, part.id),
                )
                pending_by_id.setdefault(part.id, []).append(pending)
                ordered_calls.append(pending)
                observed_tools.append(part.name)
            elif isinstance(part, ToolCallResponsePart):
                response_by_id.setdefault(part.id, []).append(part.response)

    calls: list[MCPCall] = []
    unpaired_calls: list[str] = []
    for index, pending in enumerate(ordered_calls):
        responses = response_by_id.get(pending.id) or []
        if not responses:
            unpaired_calls.append(pending.id)
            continue
        calls.append(
            MCPCall(
                tool_name=pending.name,
                args=pending.args,
                result=responses.pop(0),
                timestamp_ms=float(index),
            )
        )

    unpaired_responses = [
        call_id
        for call_id, responses in response_by_id.items()
        for _response in responses
        if call_id not in pending_by_id or responses
    ]
    return calls, _unique(observed_tools), _unique(unpaired_calls), _unique(unpaired_responses)


def _arguments_to_dict(value: Any, call_id: str) -> dict[str, Any]:
    """Normalize a transcript ``arguments`` value to MCP's object shape."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InterchangeFormatError(
                f"tool_call {call_id!r} arguments must be a JSON object"
            ) from exc
        if isinstance(decoded, dict):
            return decoded
    raise InterchangeFormatError(f"tool_call {call_id!r} arguments must be an object")


def _universe_tools(
    tool_definitions: list[InterchangeToolDefinition] | None,
    observed_tool_names: list[str],
) -> tuple[list[UniverseTool], list[str]]:
    """Build Contract B tools, stubbing only where schemas are absent."""
    tools: list[UniverseTool] = []
    stubbed: list[str] = []
    defined: set[str] = set()

    for definition in tool_definitions or []:
        schema = definition.input_schema
        if schema is None:
            schema = dict(STUB_INPUT_SCHEMA)
            stubbed.append(definition.name)
        defined.add(definition.name)
        tools.append(
            UniverseTool(
                name=definition.name,
                description=definition.description,
                input_schema=dict(schema),
                result_schema=(
                    dict(definition.result_schema)
                    if definition.result_schema is not None
                    else None
                ),
            )
        )

    for name in observed_tool_names:
        if name in defined:
            continue
        defined.add(name)
        stubbed.append(name)
        tools.append(
            UniverseTool(
                name=name,
                description="TODO: imported trace did not provide a tool definition.",
                input_schema=dict(STUB_INPUT_SCHEMA),
            )
        )

    return tools, _unique(stubbed)


def _render_scenario_py(
    *,
    scenario_name: str,
    user_turns: list[str],
    observed_tool_names: list[str],
    requires_tool_use: bool,
    source_ref: str | None,
) -> str:
    prompt = user_turns[-1] if user_turns else "(no user turns)"
    tags = [f"origin:{source_ref}"] if source_ref else []
    lines = [
        '"""Scenario skeleton generated by `wt import`.',
        "",
        "Review TODOs before adding this scenario to a pack.  Imported traces",
        "show what happened, not what should pass.",
        '"""',
        "from __future__ import annotations",
        "",
        "from windtunnel.api.scenario import Scenario",
        "",
        "",
        "scenario = Scenario(",
        f"    name={_py(scenario_name)},",
        f"    prompt={_py(prompt)},",
    ]
    if len(user_turns) > 1:
        lines.append(f"    user_turns={_py(user_turns)},")
    lines.extend([
        "    # TODO: Replace this placeholder with reviewed outcome facts or",
        "    # wire an outcome_fn from scorer.py after authoring it.",
        "    # The placeholder below can never match, so the imported scenario",
        "    # FAILS until a human authors the gate — an empty target_facts",
        "    # would pass vacuously, and a green unauthored import is a lie.",
        '    target_facts=[["TODO_REPLACE_WITH_REVIEWED_OUTCOME_FACT"]],',
        f"    requires_tool_use={requires_tool_use!r},",
    ])
    if observed_tool_names:
        lines.extend([
            "    # TODO: Uncomment only if this trajectory is part of correctness.",
            "    # must_call=[",
        ])
        for name in observed_tool_names:
            lines.append(f"    #     {_py(name)},")
        lines.append("    # ],")
    lines.extend([
        f"    tags={_py(tags)},",
        ")",
        "",
        "SCENARIOS = [scenario]",
        "",
    ])
    return "\n".join(lines)


def _render_scorer_py(final_assistant_text: str) -> str:
    candidates = [[candidate] for candidate in _candidate_facts(final_assistant_text)]
    return "\n".join([
        '"""Outcome scorer stub generated by `wt import`."""',
        "from __future__ import annotations",
        "",
        "from windtunnel.api.score import LayerResult",
        "from windtunnel.api.trace import Trace",
        "",
        "",
        "# Suggested target_facts candidates copied from the final assistant text.",
        f"SUGGESTED_TARGET_FACTS = {_py(candidates)}",
        "",
        "",
        "def outcome_fn(trace: Trace) -> LayerResult:",
        '    """TODO: Author the imported scenario outcome gate.',
        "",
        "    Useful helpers live in windtunnel.api.scorers:",
        "    all_of / observation / llm_judge / substantiated_by_tools.",
        '    """',
        "    _ = trace",
        "    return LayerResult(",
        "        passed=False,",
        "        detail=(",
        '            "TODO: author outcome_fn using windtunnel.api.scorers "',
        '            "(all_of / observation / llm_judge / substantiated_by_tools)."',
        "        ),",
        "    )",
        "",
    ])


def _render_imported_md(
    *,
    envelope: InterchangeTrace,
    evidence: _Evidence,
    user_turns: list[str],
    stubbed_tool_schemas: list[str],
    final_assistant_text: str,
    requires_tool_use: bool,
) -> str:
    source_json = (
        json.dumps(envelope.source, indent=2, ensure_ascii=False)
        if envelope.source is not None
        else None
    )
    observed_tools = evidence.observed_tool_names or []
    lines = [
        "# Imported Wind Tunnel Scenario",
        "",
        "This directory was generated by `wt import` from a Contract A trace.",
        "The generated artifacts are mechanical; human review is still required.",
        "",
        "## Inferred",
        "",
        f"- Interchange version: `{envelope.windtunnel_interchange}`",
        f"- OTel GenAI mapping: `{envelope.otel_genai_mapping or '(not provided)'}`",
        f"- Session model: `{envelope.model}`",
        f"- User turns: `{len(user_turns)}`",
        f"- Final prompt: {_md_inline(user_turns[-1] if user_turns else '(no user turns)')}",
        f"- Fixture evidence source: `{evidence.source}`",
        f"- Universe recordings emitted: `{len(evidence.calls)}`",
        f"- Observed tools: `{', '.join(observed_tools) if observed_tools else '(none)'}`",
        f"- `requires_tool_use`: `{requires_tool_use}`",
        f"- Origin tag: `{f'origin:{envelope.source_ref}' if envelope.source_ref else '(none)'}`",
        "",
        "## Source",
        "",
    ]
    if source_json is None:
        lines.append("No `source` object was provided.")
    else:
        lines.extend([
            "The full source object is preserved verbatim below.",
            "",
            "```json",
            source_json,
            "```",
        ])

    lines.extend([
        "",
        "## Human TODO",
        "",
        "- Author the outcome gate in `scorer.py`; the stub intentionally fails.",
        "- Review `SUGGESTED_TARGET_FACTS` against the intended behavior.",
        "- Keep `scenario.py` trajectory expectations commented out unless the",
        "  observed tool path is part of correctness.",
    ])

    if stubbed_tool_schemas:
        lines.append(
            "- Replace stubbed `input_schema` values in `fixture.universe.json` for: "
            + ", ".join(f"`{name}`" for name in stubbed_tool_schemas)
            + "."
        )
    elif envelope.tool_definitions is None:
        lines.append("- No tool definitions were provided, but no observed tools needed stubs.")
    else:
        lines.append("- Tool schemas came from `tool_definitions`.")

    if evidence.source == "reconstructed":
        lines.extend([
            "- Tool recordings were reconstructed from the agent transcript by",
            "  pairing `tool_call` and `tool_call_response` parts by `id`.",
        ])
    else:
        lines.append("- Tool recordings came from server-side `witnessed_calls` evidence.")

    if evidence.unpaired_tool_call_ids:
        lines.append(
            "- Unpaired `tool_call` ids were not written to the universe: "
            + ", ".join(f"`{call_id}`" for call_id in evidence.unpaired_tool_call_ids)
            + "."
        )
    if evidence.unpaired_tool_response_ids:
        lines.append(
            "- Unpaired `tool_call_response` ids were ignored: "
            + ", ".join(f"`{call_id}`" for call_id in evidence.unpaired_tool_response_ids)
            + "."
        )

    if final_assistant_text:
        lines.extend([
            "",
            "## Final Assistant Text",
            "",
            "```text",
            final_assistant_text,
            "```",
        ])

    lines.append("")
    return "\n".join(lines)


def _user_turns(messages: list[InterchangeMessage]) -> list[str]:
    return [message.text_content() for message in messages if message.role == "user"]


def _transcript_tool_names(messages: list[InterchangeMessage]) -> list[str]:
    return _unique(
        part.name
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )


def _final_assistant_text(messages: list[InterchangeMessage]) -> str:
    for message in reversed(messages):
        if message.role == "assistant":
            text = message.text_content()
            if text:
                return text
    return ""


def _candidate_facts(final_assistant_text: str) -> list[str]:
    """Return a small suggestion set from final assistant text."""
    lines = [line.strip() for line in final_assistant_text.splitlines() if line.strip()]
    if not lines and final_assistant_text.strip():
        lines = [final_assistant_text.strip()]
    if len(lines) == 1 and len(lines[0]) > 240:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", lines[0]) if s.strip()]
        lines = sentences or lines
    return lines[:5]


def _scenario_name(out_dir: Path, source_ref: str | None) -> str:
    return _slug(out_dir.name) or _slug(source_ref or "") or "imported_trace"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    if slug and slug[0].isdigit():
        slug = f"imported_{slug}"
    return slug


def _unique(values: Any) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _py(value: Any) -> str:
    return pprint.pformat(value, width=88, sort_dicts=False)


def _md_inline(value: str) -> str:
    return "`" + value.replace("`", "\\`").replace("\n", "\\n") + "`"


__all__ = [
    "ImportResult",
    "STUB_INPUT_SCHEMA",
    "write_imported_scenario",
]
