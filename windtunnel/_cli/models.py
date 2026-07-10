"""Internal data carried between CLI orchestration services."""

from __future__ import annotations

from dataclasses import dataclass

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.runner import ScenarioResult
from windtunnel.api.scenario import Scenario


@dataclass(frozen=True)
class _SelectedScenario:
    """A scenario paired with the pack that contributed it."""

    pack: ScenarioPack
    scenario: Scenario


@dataclass(frozen=True)
class _SelectionResult:
    """Selected scenarios and selector values that matched nothing."""

    entries: list[_SelectedScenario]
    unmatched_scenarios: list[str]
    unmatched_tags: list[str]
    unmatched_packs: list[str]
    unmatched_owners: list[str]


@dataclass(frozen=True)
class _CompletedAggregate:
    """A completed aggregate and the metadata sweep writers consume."""

    pack: ScenarioPack
    scenario: Scenario
    result: ScenarioResult
    transport_only: bool
    had_runner_error: bool
