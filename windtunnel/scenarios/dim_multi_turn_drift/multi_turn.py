"""Multi-turn scenario shape for dim_multi_turn_drift.

History: MultiTurnScenario predates Scenario.user_turns — it used to be the
only carrier of the ordered user turns, attached to the inner Scenario via
setattr at pack build. user_turns is now a first-class Scenario field, so
the wrapper is a thin proxy kept as this dim's authored shape (its tests
and build_turn_messages() reference it); the turn list lives on the inner
Scenario and is reachable through __getattr__.

build_turn_messages() accumulates the messages list for each successive
turn (the same interleaving the runner's _build_messages performs).
"""
from dataclasses import dataclass
from typing import Any

from windtunnel.api.scenario import Scenario


@dataclass
class MultiTurnScenario:
    """Wraps a Scenario. __getattr__ proxies Scenario fields (incl. user_turns)."""
    scenario: Scenario

    def __getattr__(self, name: str) -> Any:
        return getattr(self.scenario, name)


def build_turn_messages(user_turns: list[str], responses: list[str]) -> list[dict[str, Any]]:
    if not user_turns:
        return []
    if len(responses) >= len(user_turns):
        raise ValueError(f"Too many responses ({len(responses)}) for {len(user_turns)} turns.")
    msgs: list[dict[str, Any]] = []
    for i, u in enumerate(user_turns):
        msgs.append({"role": "user", "content": u})
        if i < len(responses):
            msgs.append({"role": "assistant", "content": responses[i]})
    return msgs
