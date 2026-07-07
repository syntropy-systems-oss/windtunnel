"""Prompt-surface goldens — the prevention half of the surface work.

Per-segment hashes are the semantic core: comparison ONLY ever reads
hashes, so a hash-only golden (the default) contains zero prompt text and
is safe to commit anywhere. Full text is an opt-in sidecar for humans
(``store_text=True``) — teams with public prompts get textual PR diffs —
and it never participates in comparison.

Diffs are per-segment by construction: "tool 'client_lookup' changed",
never "bytes differ". That is the point — a surface diff is a reviewable
steering artifact, and the house rule it enables is mechanical: surface
diff ⇒ bench run before merge. The hash is a tripwire, never a
skip-token: an unchanged surface proves nothing about behavior (harness,
model, and tool implementations move behavior with identical text).

Pure stdlib, same as trace.py.
"""
from __future__ import annotations

import json
from typing import Any

from windtunnel.api.trace import Hash, compute_hash

GOLDEN_VERSION = 1

SENSITIVITY_WARNING = (
    "This golden embeds the agent's full prompt surface. A captured surface "
    "IS the system prompt — treat this file as sensitively as the prompt "
    "itself, and keep it out of public repositories unless the prompt is "
    "public. Hash-only goldens (the default, without --store-text) carry no "
    "prompt text and are safe to commit anywhere."
)

_SEGMENT_KEYS = ("system_instructions", "tool_definitions", "extra_segments")


class SurfaceGoldenError(ValueError):
    """A surface block or golden file that cannot be used as requested."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def build_surface_golden(
    block: dict[str, Any], *, store_text: bool = False
) -> dict[str, Any]:
    """Build a golden dict from a captured surface block.

    The block is a Trace.surface value with status "reported" or
    "rendered" (see spi/agent_runtime.py describe_surface). Any other
    status has no surface to golden — raising here keeps "we recorded a
    golden" meaning "there was a surface", never a hash of absence.

    Duplicate tool or extra-segment names raise: per-name diffing depends
    on names being stable AND unique addresses.
    """
    status = block.get("status")
    if status not in ("reported", "rendered"):
        raise SurfaceGoldenError(
            f"cannot build a golden from a surface with status {status!r} — "
            "only 'reported' or 'rendered' surfaces carry segments"
        )

    system_instructions = block.get("system_instructions") or []
    tool_definitions = block.get("tool_definitions") or []
    extra_segments = block.get("extra_segments") or []

    tool_hashes: dict[str, Hash] = {}
    tool_order: list[str] = []
    for definition in tool_definitions:
        name = definition.get("name")
        if not isinstance(name, str) or not name:
            raise SurfaceGoldenError("tool_definitions entries must have a non-empty name")
        if name in tool_hashes:
            raise SurfaceGoldenError(
                f"duplicate tool definition name {name!r} — per-name diffing "
                "requires unique names"
            )
        tool_order.append(name)
        tool_hashes[name] = compute_hash(_canonical(definition))

    segment_hashes: dict[str, Hash] = {}
    for segment in extra_segments:
        name = segment.get("name")
        if not isinstance(name, str) or not name:
            raise SurfaceGoldenError("extra_segments entries must have a non-empty name")
        if name in segment_hashes:
            raise SurfaceGoldenError(
                f"duplicate extra segment name {name!r} — per-name diffing "
                "requires unique names"
            )
        segment_hashes[name] = compute_hash(str(segment.get("content", "")))

    golden: dict[str, Any] = {
        "windtunnel_surface_golden": GOLDEN_VERSION,
        "status": status,
        "system_instructions": compute_hash(_canonical(system_instructions)),
        "tool_order": tool_order,
        "tool_definitions": tool_hashes,
        "extra_segments": segment_hashes,
    }
    if store_text:
        golden["sensitivity_warning"] = SENSITIVITY_WARNING
        golden["text"] = {
            "system_instructions": system_instructions,
            "tool_definitions": tool_definitions,
            "extra_segments": extra_segments,
        }
    return golden


def parse_surface_golden(raw: Any) -> dict[str, Any]:
    """Validate a loaded golden dict; raise SurfaceGoldenError when unusable."""
    if not isinstance(raw, dict):
        raise SurfaceGoldenError("golden must be a JSON object")
    version = raw.get("windtunnel_surface_golden")
    if version != GOLDEN_VERSION:
        raise SurfaceGoldenError(
            f"windtunnel_surface_golden must be {GOLDEN_VERSION}, got {version!r}"
        )
    if not isinstance(raw.get("system_instructions"), str):
        raise SurfaceGoldenError("golden missing 'system_instructions' hash")
    if not isinstance(raw.get("tool_order"), list):
        raise SurfaceGoldenError("golden missing 'tool_order' list")
    for key in ("tool_definitions", "extra_segments"):
        if not isinstance(raw.get(key), dict):
            raise SurfaceGoldenError(f"golden missing {key!r} hash map")
    return raw


def diff_surface_goldens(
    golden: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    """Per-segment differences between a golden and a fresh candidate.

    Both arguments are golden-shaped dicts (build_surface_golden output /
    parse_surface_golden result); text sidecars are ignored — comparison
    only ever reads hashes. Returns human-readable change lines, empty
    when the surfaces match.
    """
    changes: list[str] = []

    if golden.get("status") != candidate.get("status"):
        changes.append(
            f"surface status changed: {golden.get('status')!r} → "
            f"{candidate.get('status')!r}"
        )

    if golden["system_instructions"] != candidate["system_instructions"]:
        changes.append("system instructions changed")

    old_tools: dict[str, str] = golden["tool_definitions"]
    new_tools: dict[str, str] = candidate["tool_definitions"]
    for name in sorted(new_tools.keys() - old_tools.keys()):
        changes.append(f"tool added: {name!r}")
    for name in sorted(old_tools.keys() - new_tools.keys()):
        changes.append(f"tool removed: {name!r}")
    for name in sorted(old_tools.keys() & new_tools.keys()):
        if old_tools[name] != new_tools[name]:
            changes.append(f"tool changed: {name!r}")
    if (
        old_tools.keys() == new_tools.keys()
        and golden["tool_order"] != candidate["tool_order"]
    ):
        # Only worth reporting when membership is identical; adds/removes
        # above already explain an order shift otherwise.
        changes.append("tool manifest order changed")

    old_segments: dict[str, str] = golden["extra_segments"]
    new_segments: dict[str, str] = candidate["extra_segments"]
    for name in sorted(new_segments.keys() - old_segments.keys()):
        changes.append(f"extra segment added: {name!r}")
    for name in sorted(old_segments.keys() - new_segments.keys()):
        changes.append(f"extra segment removed: {name!r}")
    for name in sorted(old_segments.keys() & new_segments.keys()):
        if old_segments[name] != new_segments[name]:
            changes.append(f"extra segment changed: {name!r}")

    return changes
