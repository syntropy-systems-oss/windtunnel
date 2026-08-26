"""Read-only loaders behind `wt serve`.

Three data sources, all consumed as they already exist on disk or in the
process — nothing here mutates anything:

  - the append-only sweep ledger (`runs/ledger.ndjsonl`, one JSON object per
    line, written by the `wt run` ledger append — see _cli/storage.py),
  - saved traces plus their `.score.json` sidecars under the runs/ directory
    (the same artifacts `wt report`, `wt triage`, and `wt rescore` consume),
  - discovered ScenarioPacks (built-ins, entry points, and --pack-source),
    summarized into plain JSON-serializable dicts for the scenario browser.

Raw JSON pass-through on purpose: the run drill-down returns the trace and
sidecar dicts exactly as stored rather than round-tripping them through the
dataclasses, so the viewer shows what is actually on disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario
from windtunnel.api.trace import is_trace_json_path

# Run ids are uuid4 strings today, but the trace schema only promises a
# string — accept a conservative filename-safe alphabet and nothing else so
# a request can never smuggle path syntax into the runs/ walk.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ─── ledger ──────────────────────────────────────────────────────────────────


def load_ledger_rows(runs_dir: Path) -> dict[str, Any]:
    """Parse runs/ledger.ndjsonl into rows, newest first.

    Malformed lines are counted and skipped, never fatal — the ledger is
    append-only and a torn final line (a sweep writing right now) is a
    normal condition for a live viewer. A missing ledger is an empty
    dashboard, not an error.
    """
    ledger_path = Path(runs_dir) / "ledger.ndjsonl"
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return {"rows": [], "skipped": 0}

    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        record["_row"] = index
        rows.append(record)

    # Append order is already chronological; sort by ts (falling back to file
    # order) so rows merged from concurrent sweeps still render newest first.
    rows.sort(key=lambda row: (str(row.get("ts") or ""), row["_row"]), reverse=True)
    return {"rows": rows, "skipped": skipped}


# ─── run drill-down ──────────────────────────────────────────────────────────


def resolve_run_path(runs_dir: Path, run_id: str) -> Path | None:
    """Locate the stored trace file for one run_id, or None.

    Storage puts each trace at
    ``<runs>/<scenario_id>/<agent_id>/<variant_id>/<model>/<quant>/<ts>_<run_id[:8]>.json``
    (see api/trace.storage_path), so candidates are narrowed by the filename's
    run_id prefix and confirmed against the run_id stored inside the trace.
    """
    if not _RUN_ID_RE.match(run_id):
        return None
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return None

    suffix = f"_{run_id[:8]}.json"
    for trace_path in sorted(runs_dir.rglob(f"*{suffix}")):
        if not is_trace_json_path(trace_path):
            continue
        try:
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(trace_data, dict) and trace_data.get("run_id") == run_id:
            return trace_path
    return None


def resolve_run(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Return one saved run's raw trace JSON + score sidecar, or None."""
    trace_path = resolve_run_path(runs_dir, run_id)
    if trace_path is None:
        return None
    try:
        trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "trace": trace_data,
        "score": _read_sidecar(trace_path),
        "trace_file": trace_path.relative_to(runs_dir).as_posix(),
    }


def _read_sidecar(trace_path: Path) -> dict[str, Any] | None:
    """Read the `.score.json` sidecar beside a trace, or None when absent."""
    score_path = trace_path.with_suffix(".score.json")
    try:
        data = json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ─── scenario browser ────────────────────────────────────────────────────────


def scenarios_by_id(packs: list[ScenarioPack]) -> dict[str, Scenario]:
    """Index discovered scenarios by name for evidence recomputation.

    Packs flatten in discovery order (matching selection); on a duplicate
    scenario name the first pack's definition wins, mirroring how the
    dashboard reader must pick ONE definition to recompute against.
    """
    index: dict[str, Scenario] = {}
    for pack in packs:
        for scenario in getattr(pack, "scenarios", []) or []:
            index.setdefault(str(getattr(scenario, "name", "")), scenario)
    return index


def pack_summaries(packs: list[ScenarioPack]) -> list[dict[str, Any]]:
    """Summarize discovered packs for the scenario browser.

    Uses the same discovered ScenarioPack objects `wt run` selects from —
    built-in dims plus entry-point packs plus any --pack-source — so the
    browser shows exactly the test cases a sweep would run.
    """
    return [
        {
            "name": str(getattr(pack, "name", "")),
            "owner": getattr(pack, "owner", None),
            "transport_only": bool(getattr(pack, "transport_only", False)),
            "metadata": dict(getattr(pack, "metadata", {}) or {}),
            "tool_surface": _tool_surface(pack),
            "scenarios": [
                scenario_summary(scenario)
                for scenario in (getattr(pack, "scenarios", []) or [])
            ],
        }
        for pack in packs
    ]


def scenario_summary(scenario: Scenario) -> dict[str, Any]:
    """Flatten one Scenario's authored expectations into a plain dict.

    Callable layers (policies, trajectory checks, outcome_fn, perturbations)
    are represented by name/marker — the browser shows what a scenario
    declares, it never executes any of it.
    """
    failure_cost = scenario.failure_cost
    return {
        "name": scenario.name,
        "scored_prompt": scenario.scored_prompt,
        "user_turns": list(scenario.user_turns),
        "tags": list(scenario.tags or []),
        "target_facts": [list(group) for group in scenario.target_facts],
        "target_numbers": [
            {"value": fact.value, "unit": fact.unit} for fact in scenario.target_numbers
        ],
        "forbidden_facts": list(scenario.forbidden_facts),
        "requires_tool_use": bool(scenario.requires_tool_use),
        "has_outcome_fn": scenario.outcome_fn is not None,
        "must_call": [
            list(entry) if isinstance(entry, list) else entry for entry in scenario.must_call
        ],
        "forbidden_calls": list(scenario.forbidden_calls),
        "order_matters": bool(scenario.order_matters),
        "trajectory_checks": [type(check).__name__ for check in scenario.trajectory_checks],
        "policies": [
            {"name": policy.name, "effect_class": policy.effect_class}
            for policy in scenario.policies
        ],
        "gate_layers": list(scenario.resolved_gate_layers()),
        "perturbations": [
            {"type": type(perturbation).__name__, "marker": _perturbation_marker(perturbation)}
            for perturbation in scenario.perturbations
        ],
        "requires_tools": list(scenario.requires_tools),
        "requires_files": list(scenario.requires_files),
        "precondition_count": len(scenario.preconditions),
        "variance_allowed": bool(scenario.variance_allowed),
        "reference_case_count": len(scenario.reference_cases),
        "failure_cost": {
            "severity": failure_cost.severity,
            "customer_visible": failure_cost.customer_visible,
            "reversible": failure_cost.reversible,
            "side_effect_performed": failure_cost.side_effect_performed,
            "risk_weight": failure_cost.risk_weight,
        },
    }


def _perturbation_marker(perturbation: Any) -> str | None:
    """Best-effort marker read — a property that raises is honest absence."""
    try:
        marker = perturbation.marker
    except Exception:  # noqa: BLE001 - authored property, keep the browser up
        return None
    return str(marker) if marker is not None else None


def _tool_surface(pack: ScenarioPack) -> dict[str, Any]:
    """Describe the runtime/tool surface a pack declares, without starting it.

    `wt serve` is read-only and must not spawn mock server processes, so the
    listing is best-effort: a constructed server that exposes served_tools()
    (the ToolIntrospectableMCPHandle shape) is listed; one that only knows
    its tools once started reports honest absence instead.
    """
    factory = getattr(pack, "mcp_factory", None)
    if factory is None:
        return {
            "declared": False,
            "tools": None,
            "detail": "pack declares no mock tool server",
        }

    scenarios = getattr(pack, "scenarios", []) or []
    if not scenarios:
        return {
            "declared": True,
            "tools": None,
            "detail": "mcp_factory declared, but the pack has no scenario to build it for",
        }

    try:
        server = factory(scenarios[0])
    except Exception as exc:  # noqa: BLE001 - introspection must not kill the viewer
        return {
            "declared": True,
            "tools": None,
            "detail": f"mcp_factory raised: {type(exc).__name__}: {exc}",
        }

    served = getattr(server, "served_tools", None)
    if callable(served):
        try:
            return {
                "declared": True,
                "tools": [str(name) for name in served()],
                "detail": "served_tools() reported by the constructed server",
            }
        except Exception as exc:  # noqa: BLE001 - same forgiveness as above
            return {
                "declared": True,
                "tools": None,
                "detail": f"served_tools() raised: {type(exc).__name__}: {exc}",
            }

    return {
        "declared": True,
        "tools": None,
        "detail": "tool listing requires starting the mock server, which wt serve never does",
    }
