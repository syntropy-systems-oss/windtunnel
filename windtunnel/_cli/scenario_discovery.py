"""Scenario-pack discovery, loading, and selection for the CLI."""

from __future__ import annotations

import fnmatch
import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

from windtunnel._cli.models import _SelectedScenario, _SelectionResult
from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario


def _discover_scenario_packs(
    extra_sources: list[str] | None = None,
) -> list[ScenarioPack]:
    """Return built-in, entry-point, and explicitly sourced scenario packs."""
    from importlib.metadata import entry_points

    from windtunnel.scenarios import builtin_packs

    packs: list[ScenarioPack] = list(builtin_packs())
    for ep in entry_points(group="windtunnel.scenario_packs"):
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 - normalize third-party load failures
            print(
                f"wt run: could not load scenario pack {ep.name!r} ({ep.value}): {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        packs.append(_coerce_scenario_pack(obj, f"entry point {ep.name!r}", ep.value))
    for source in extra_sources or []:
        packs.append(_load_scenario_pack_source(source))
    _validate_scenario_packs(packs)
    return packs


def _validate_scenario_packs(packs: list[ScenarioPack]) -> None:
    """Fail loudly when dimension tags drift from registered pack names.

    ``dim:`` tags remain useful selection metadata after operational wiring
    moves to the owning ``_SelectedScenario.pack``. Validate that metadata at
    discovery so a rename cannot silently exclude scenarios from a tagged
    sweep. Tagless third-party packs remain valid for backwards compatibility.
    """
    registered_names = {pack.name for pack in packs}
    failures: list[str] = []

    for pack in packs:
        for scenario in pack.scenarios:
            dimensions = {
                tag.removeprefix("dim:")
                for tag in scenario.tags
                if tag.startswith("dim:")
            }
            if not dimensions:
                continue

            unknown = sorted(dimensions - registered_names)
            if unknown:
                failures.append(
                    f"scenario {scenario.name!r} in pack {pack.name!r} references "
                    f"unknown dimension tag(s): {', '.join(f'dim:{name}' for name in unknown)}"
                )
            if pack.name not in dimensions:
                failures.append(
                    f"scenario {scenario.name!r} in pack {pack.name!r} is missing its "
                    f"owning tag 'dim:{pack.name}'"
                )

    if failures:
        print("wt run: invalid scenario pack dimension tags:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        sys.exit(2)


def _coerce_scenario_pack(obj: object, label: str, value: str) -> ScenarioPack:
    if not isinstance(obj, ScenarioPack) and callable(obj):
        obj = obj()
    if not isinstance(obj, ScenarioPack):
        print(
            f"wt run: scenario pack {label} ({value}) must resolve to a "
            "ScenarioPack instance or a zero-arg callable returning one, "
            f"got {type(obj).__name__}.",
            file=sys.stderr,
        )
        sys.exit(2)
    return obj


def _load_scenario_pack_source(source: str) -> ScenarioPack:
    module_or_path, sep, attr = source.partition(":")
    if not sep or not module_or_path or not attr:
        print(
            f"wt run: --pack-source must be module:attr or path/to/file.py:attr, got {source!r}.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        if module_or_path.endswith(".py") or "/" in module_or_path or "\\" in module_or_path:
            path = Path(module_or_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
            module_name = f"_windtunnel_pack_{digest}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load module spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        else:
            module = importlib.import_module(module_or_path)
        obj = getattr(module, attr)
    except Exception as exc:  # noqa: BLE001 - source load failures are usage errors
        print(f"wt run: could not load scenario pack source {source!r}: {exc}", file=sys.stderr)
        sys.exit(2)

    return _coerce_scenario_pack(obj, f"source {source!r}", source)


def _select_scenarios(
    *,
    scenario_patterns: list[str],
    tag_filters: list[str],
    pack_filters: list[str],
    owner_filters: list[str],
    packs: list[ScenarioPack],
) -> _SelectionResult:
    """Select scenarios with OR-within-flag and AND-across-flags semantics."""
    all_entries: list[_SelectedScenario] = []
    for pack in packs:
        for scenario in getattr(pack, "scenarios", []) or []:
            all_entries.append(_SelectedScenario(pack=pack, scenario=scenario))

    def scenario_name(entry: _SelectedScenario) -> str:
        return str(getattr(entry.scenario, "name", ""))

    def scenario_tags(entry: _SelectedScenario) -> list[str]:
        return list(getattr(entry.scenario, "tags", []) or [])

    def pack_name(entry: _SelectedScenario) -> str:
        return str(getattr(entry.pack, "name", ""))

    def pack_owner(entry: _SelectedScenario) -> str | None:
        owner = getattr(entry.pack, "owner", None)
        return str(owner) if owner is not None else None

    def scenario_selected(entry: _SelectedScenario) -> bool:
        return not scenario_patterns or any(
            fnmatch.fnmatchcase(scenario_name(entry), pattern) for pattern in scenario_patterns
        )

    def tag_selected(entry: _SelectedScenario) -> bool:
        tags = scenario_tags(entry)
        return not tag_filters or any(tag in tags for tag in tag_filters)

    def pack_selected(entry: _SelectedScenario) -> bool:
        return not pack_filters or any(pack_name(entry) == name for name in pack_filters)

    def owner_selected(entry: _SelectedScenario) -> bool:
        return not owner_filters or any(pack_owner(entry) == owner for owner in owner_filters)

    entries = [
        entry
        for entry in all_entries
        if (
            scenario_selected(entry)
            and tag_selected(entry)
            and pack_selected(entry)
            and owner_selected(entry)
        )
    ]

    unmatched_scenarios = [
        pattern
        for pattern in scenario_patterns
        if not any(fnmatch.fnmatchcase(scenario_name(entry), pattern) for entry in all_entries)
    ]
    unmatched_tags = [
        tag for tag in tag_filters if not any(tag in scenario_tags(entry) for entry in all_entries)
    ]
    unmatched_packs = [
        name
        for name in pack_filters
        if not any(str(getattr(pack, "name", "")) == name for pack in packs)
    ]
    unmatched_owners = [
        owner
        for owner in owner_filters
        if not any(str(getattr(pack, "owner", "")) == owner for pack in packs)
    ]

    return _SelectionResult(
        entries=entries,
        unmatched_scenarios=unmatched_scenarios,
        unmatched_tags=unmatched_tags,
        unmatched_packs=unmatched_packs,
        unmatched_owners=unmatched_owners,
    )


def _print_selection_warnings(selection: _SelectionResult, *, command: str = "wt run") -> None:
    """Emit non-fatal diagnostics for selector values that matched nothing."""
    if selection.unmatched_scenarios:
        print(
            f"{command}: unknown scenario(s): {', '.join(sorted(selection.unmatched_scenarios))}",
            file=sys.stderr,
        )
    if selection.unmatched_tags:
        print(
            f"{command}: unknown tag(s): {', '.join(sorted(selection.unmatched_tags))}",
            file=sys.stderr,
        )
    if selection.unmatched_packs:
        print(
            f"{command}: unknown pack(s): {', '.join(sorted(selection.unmatched_packs))}",
            file=sys.stderr,
        )
    if selection.unmatched_owners:
        print(
            f"{command}: unknown owner(s): {', '.join(sorted(selection.unmatched_owners))}",
            file=sys.stderr,
        )


def _load_scenarios(names: list[str], packs: list[ScenarioPack]) -> list[Scenario]:
    """Flatten packs in order and optionally filter scenarios by name."""
    selection = _select_scenarios(
        scenario_patterns=names,
        tag_filters=[],
        pack_filters=[],
        owner_filters=[],
        packs=packs,
    )
    if selection.unmatched_scenarios:
        print(
            f"wt run: unknown scenario(s): {', '.join(sorted(selection.unmatched_scenarios))}",
            file=sys.stderr,
        )
    return [entry.scenario for entry in selection.entries]
