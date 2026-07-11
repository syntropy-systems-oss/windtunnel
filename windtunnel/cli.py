"""CLI entry point for Wind Tunnel — the `wt` command.

Subcommands:
    wt run      [--scenario S]... [--tag TAG]... [--pack PACK]...
                [--owner OWNER]... [--soul PATH] [--runtime RUNTIME]
                [--label LABEL] [--runs N] [--format junit|json --out FILE]
    wt rescore  (--runs DIR | --trace PATH...) [--write]
    wt report   [--runs DIR] [--out FILE] [--format html|markdown|json]
    wt compare  --labels L1 L2 ...
    wt replay   --trace PATH --runtime RUNTIME
    wt doctor   --runtime RUNTIME [--soul PATH] [--label LABEL]
    wt import   --trace PATH --out DIR [--force]
    wt validate [--strict] PATH [PATH ...]
    wt triage   [--runs DIR] [--classifier rule_based]
    wt skill    path | install [--dest DIR] [--copy]

Design: argparse (stdlib) — no click dependency. Each subcommand is a
function; main() is the dispatch entry point. Exit code 0 = all pass,
non-zero = any regression or error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import sys
import traceback
from importlib import resources
from pathlib import Path
from typing import Any

from windtunnel._cli.hooks import (
    _as_hook_instance as _as_hook_instance_impl,
)
from windtunnel._cli.hooks import (
    _dispatch_pack_end_hooks as _dispatch_pack_end_hooks_impl,
)
from windtunnel._cli.hooks import (
    _resolve_hooks as _resolve_hooks_impl,
)
from windtunnel._cli.models import (
    _CompletedAggregate as _CompletedAggregateModel,
)
from windtunnel._cli.models import (
    _SelectedScenario as _SelectedScenarioModel,
)
from windtunnel._cli.models import (
    _SelectionResult as _SelectionResultModel,
)
from windtunnel._cli.output import (
    _aggregate_time_seconds as _aggregate_time_seconds_impl,
)
from windtunnel._cli.output import (
    _counts_as_gate_failure as _counts_as_gate_failure_impl,
)
from windtunnel._cli.output import (
    _format_seconds as _format_seconds_impl,
)
from windtunnel._cli.output import (
    _junit_failure_message as _junit_failure_message_impl,
)
from windtunnel._cli.output import (
    _junit_failure_text as _junit_failure_text_impl,
)
from windtunnel._cli.output import (
    _triage_categories as _triage_categories_impl,
)
from windtunnel._cli.output import (
    _write_run_json as _write_run_json_impl,
)
from windtunnel._cli.output import (
    _write_run_junit as _write_run_junit_impl,
)
from windtunnel._cli.output import (
    _write_run_output as _write_run_output_impl,
)
from windtunnel._cli.runtime_discovery import (
    _as_plugin_instance as _as_plugin_instance_impl,
)
from windtunnel._cli.runtime_discovery import (
    _build_runtime as _build_runtime_impl,
)
from windtunnel._cli.runtime_discovery import (
    _HttpInjectPlugin as _HttpInjectPluginImpl,
)
from windtunnel._cli.runtime_discovery import (
    _InMemoryPlugin as _InMemoryPluginImpl,
)
from windtunnel._cli.runtime_discovery import (
    _resolve_runtime_plugin as _resolve_runtime_plugin_impl,
)
from windtunnel._cli.runtime_discovery import (
    _TerminusPlugin as _TerminusPluginImpl,
)
from windtunnel._cli.scenario_discovery import (
    _coerce_scenario_pack as _coerce_scenario_pack_impl,
)
from windtunnel._cli.scenario_discovery import (
    _discover_scenario_packs as _discover_scenario_packs_impl,
)
from windtunnel._cli.scenario_discovery import (
    _load_scenario_pack_source as _load_scenario_pack_source_impl,
)
from windtunnel._cli.scenario_discovery import (
    _load_scenarios as _load_scenarios_impl,
)
from windtunnel._cli.scenario_discovery import (
    _print_selection_warnings as _print_selection_warnings_impl,
)
from windtunnel._cli.scenario_discovery import (
    _select_scenarios as _select_scenarios_impl,
)
from windtunnel._cli.storage import (
    _append_ledger_records as _append_ledger_records_impl,
)
from windtunnel._cli.storage import (
    _artifact_component as _artifact_component_impl,
)
from windtunnel._cli.storage import (
    _collision_safe_artifact_path as _collision_safe_artifact_path_impl,
)
from windtunnel._cli.storage import (
    _git_sha as _git_sha_impl,
)
from windtunnel._cli.storage import (
    _ledger_record as _ledger_record_impl,
)
from windtunnel._cli.storage import (
    _ledger_timestamp as _ledger_timestamp_impl,
)
from windtunnel._cli.storage import (
    _origin_from_tags as _origin_from_tags_impl,
)
from windtunnel._cli.storage import (
    _sweep_artifact_timestamp as _sweep_artifact_timestamp_impl,
)
from windtunnel._cli.storage import (
    _write_hook_artifact_sidecar as _write_hook_artifact_sidecar_impl,
)
from windtunnel._cli.storage import (
    _write_pack_hook_artifact as _write_pack_hook_artifact_impl,
)
from windtunnel._cli.storage import (
    _write_scenario_hook_artifact as _write_scenario_hook_artifact_impl,
)
from windtunnel._cli.storage import (
    _write_score_sidecar as _write_score_sidecar_impl,
)
from windtunnel._cli.storage import (
    _write_sweep_hook_artifact as _write_sweep_hook_artifact_impl,
)
from windtunnel._cli.storage import (
    _wt_version as _wt_version_impl,
)
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import Score
from windtunnel.api.trace import Trace
from windtunnel.triage.classifier import FailureClassification, FailureClassifier

# Compatibility facade: command orchestration and historical test seams remain
# available from ``windtunnel.cli`` while their implementations live in focused
# private service modules.
_as_hook_instance = _as_hook_instance_impl
_dispatch_pack_end_hooks = _dispatch_pack_end_hooks_impl
_resolve_hooks = _resolve_hooks_impl
_CompletedAggregate = _CompletedAggregateModel
_SelectedScenario = _SelectedScenarioModel
_SelectionResult = _SelectionResultModel
_as_plugin_instance = _as_plugin_instance_impl
_build_runtime = _build_runtime_impl
_HttpInjectPlugin = _HttpInjectPluginImpl
_InMemoryPlugin = _InMemoryPluginImpl
_resolve_runtime_plugin = _resolve_runtime_plugin_impl
_TerminusPlugin = _TerminusPluginImpl
_coerce_scenario_pack = _coerce_scenario_pack_impl
_discover_scenario_packs = _discover_scenario_packs_impl
_load_scenario_pack_source = _load_scenario_pack_source_impl
_load_scenarios = _load_scenarios_impl
_print_selection_warnings = _print_selection_warnings_impl
_select_scenarios = _select_scenarios_impl
_counts_as_gate_failure = _counts_as_gate_failure_impl
_write_run_output = _write_run_output_impl
_write_run_json = _write_run_json_impl
_write_run_junit = _write_run_junit_impl
_junit_failure_message = _junit_failure_message_impl
_junit_failure_text = _junit_failure_text_impl
_triage_categories = _triage_categories_impl
_aggregate_time_seconds = _aggregate_time_seconds_impl
_format_seconds = _format_seconds_impl
_write_score_sidecar = _write_score_sidecar_impl
_write_hook_artifact_sidecar = _write_hook_artifact_sidecar_impl
_write_pack_hook_artifact = _write_pack_hook_artifact_impl
_write_scenario_hook_artifact = _write_scenario_hook_artifact_impl
_write_sweep_hook_artifact = _write_sweep_hook_artifact_impl
_collision_safe_artifact_path = _collision_safe_artifact_path_impl
_artifact_component = _artifact_component_impl
_sweep_artifact_timestamp = _sweep_artifact_timestamp_impl
_ledger_timestamp = _ledger_timestamp_impl
_origin_from_tags = _origin_from_tags_impl
_git_sha = _git_sha_impl
_wt_version = _wt_version_impl
_ledger_record = _ledger_record_impl
_append_ledger_records = _append_ledger_records_impl

# ─── report ──────────────────────────────────────────────────────────────────


def _cmd_report(args: argparse.Namespace) -> int:
    """Handle the `wt report` subcommand."""
    from windtunnel.report import generate_html, generate_json, generate_markdown  # noqa: PLC0415

    runs_dir = Path(args.runs)
    fmt = args.format.lower()

    if fmt == "markdown":
        generate_markdown(runs_dir=runs_dir, out=sys.stdout)
        return 0

    if fmt == "json":
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as out:
                generate_json(runs_dir=runs_dir, out=out)
            print(f"wrote: {out_path}", file=sys.stderr)
        else:
            generate_json(runs_dir=runs_dir, out=sys.stdout)
        return 0

    # HTML (default)
    out_path = Path(args.out) if args.out else Path("report.html")
    generate_html(runs_dir=runs_dir, out_path=out_path)
    print(f"wrote: {out_path}", file=sys.stderr)
    return 0


# ─── compare ─────────────────────────────────────────────────────────────────


def _cmd_compare(args: argparse.Namespace) -> int:
    """Handle the `wt compare` subcommand.

    Loads traces for each label from the runs/ dir and prints a diff table.
    Labels correspond to variant_id values in stored traces.
    """
    from windtunnel.report import _cell_from_run, compute_diff, load_runs  # noqa: PLC0415

    runs_dir = Path(args.runs)
    labels: list[str] = args.labels

    if len(labels) < 2:
        print("wt compare: provide at least 2 --labels", file=sys.stderr)
        return 2

    all_runs = load_runs(runs_dir)
    if not all_runs:
        print(f"No runs found in {runs_dir}", file=sys.stderr)
        return 1

    # Group by label (variant_id)
    by_label: dict[str, list[tuple[str, dict[str, Any]]]] = {lbl: [] for lbl in labels}
    for (scenario_id, variant_id), cell in all_runs.items():
        if variant_id in by_label:
            by_label[variant_id].append((scenario_id, cell))

    # Print comparison table
    print(f"{'Scenario':<40} " + "  ".join(f"{lbl:<15}" for lbl in labels))
    print("-" * (40 + 18 * len(labels)))

    # Collect all scenario_ids across labels
    all_scenarios: set[str] = set()
    for cells in by_label.values():
        for sid, _ in cells:
            all_scenarios.add(sid)

    for sid in sorted(all_scenarios):
        row = f"{sid:<40} "
        for lbl in labels:
            cell_map = dict(by_label[lbl])
            selected_cell = cell_map.get(sid)
            if selected_cell is None:
                row += f"{'N/A':<17}"
            else:
                report_cell = _cell_from_run(
                    selected_cell.get("trace") or {}, selected_cell.get("score") or {}
                )
                status = str(report_cell["verdict"])
                row += f"{status:<17}"
        print(row)

    # The first label is the baseline; a pre-existing baseline failure is not
    # itself a regression. Fail only when a later label moves to a worse
    # verdict, preserving PASS_WITH_VARIANCE ordering through compute_diff().
    baseline = labels[0]
    changes = [
        (candidate, item)
        for candidate in labels[1:]
        for item in compute_diff(runs_dir, baseline, candidate)
    ]
    any_regression = any(item["direction"] == "regression" for _, item in changes)
    if changes:
        print("\nRisk-ranked changes:")
        for candidate, item in sorted(
            changes,
            key=lambda pair: (
                0 if pair[1]["direction"] == "regression" else 1,
                -float(pair[1]["risk_delta"]),
                str(pair[1]["scenario_id"]),
            ),
        ):
            print(
                f"  {str(item['direction']).upper():<11} {item['scenario_id']} "
                f"({baseline}={item['verdict_a']} -> {candidate}={item['verdict_b']}, "
                f"risk {float(item['risk_a']):.2f} -> {float(item['risk_b']):.2f})"
            )
    return 1 if any_regression else 0


# ─── run ─────────────────────────────────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    """Handle the `wt run` subcommand.

    Drives selected scenarios against the specified runtime, writes traces
    to the runs/ directory, and exits non-zero if any scenario fails.

    Supports built-in runtimes for smoke testing and Contract C endpoints,
    plus any runtime plugin installed under the "windtunnel.runtimes"
    entry-point group (e.g. acme / acme_gateway from a platform driver
    package) or a 'module:attr' dotted path — see
    windtunnel.spi.runtime_plugin.

    Scenarios arrive as ScenarioPacks: the built-in dims plus any pack
    installed under the "windtunnel.scenario_packs" entry-point group —
    see windtunnel.api.pack and _discover_scenario_packs.
    """
    from windtunnel.api.preconditions import WorldMismatchError  # noqa: PLC0415
    from windtunnel.api.runner import run_scenario  # noqa: PLC0415
    from windtunnel.api.trace import save_trace, storage_path  # noqa: PLC0415

    output_format = getattr(args, "format", None)
    output_path = getattr(args, "out", None)
    if bool(output_format) != bool(output_path):
        print("wt run: --format and --out must be provided together.", file=sys.stderr)
        return 2
    if output_format is not None:
        output_format = output_format.lower()

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    n_runs: int = args.n_runs
    runtime_name: str = args.runtime
    label: str = args.label or "cli_run"
    scenario_patterns: list[str] = args.scenario or []
    tag_filters: list[str] = args.tag or []
    pack_filters: list[str] = args.pack or []
    owner_filters: list[str] = args.owner or []
    hooks = _resolve_hooks(getattr(args, "hook", None))
    sweep_timestamp = _sweep_artifact_timestamp()

    # Resolve once for the entire invocation: stateful plugins must receive
    # build() and pre_run() on the same object.
    plugin = _resolve_runtime_plugin(runtime_name)
    runtime = _build_runtime(runtime_name, label, soul_path=args.soul, _plugin=plugin)

    # Discover scenario packs (built-ins + the "windtunnel.scenario_packs"
    # entry-point group). The pack is the unit that carries a dim's scenarios,
    # its mock-MCP factory, and the transport-only flag — see windtunnel.api.pack.
    pack_sources = args.pack_source or []
    packs = _discover_scenario_packs(pack_sources) if pack_sources else _discover_scenario_packs()

    # Load scenarios
    selection = _select_scenarios(
        scenario_patterns=scenario_patterns,
        tag_filters=tag_filters,
        pack_filters=pack_filters,
        owner_filters=owner_filters,
        packs=packs,
    )
    _print_selection_warnings(selection)
    selected = selection.entries
    scenarios = [entry.scenario for entry in selected]
    if not selected:
        print("wt run: no scenarios found. Use --scenario <name> to specify.", file=sys.stderr)
        return 2

    from windtunnel.spi.agent_runtime import AgentConfig  # noqa: PLC0415

    # Thread --soul (a PATH) into AgentConfig.system_prompt so the platform
    # runtime's provision() writes it to the SOUL doc via `set-docs --soul`. The
    # flag is named --soul and the natural target is the SOUL document, so the
    # file content lands in system_prompt (NOT persona_doc, which feeds AGENTS).
    system_prompt: str | None = None
    if args.soul:
        soul_path = Path(args.soul)
        if not soul_path.is_file():
            print(f"wt run: --soul file not found: {args.soul}", file=sys.stderr)
            return 2
        system_prompt = soul_path.read_text()
    # --agents (a PATH) → AgentConfig.persona_doc, which provision() writes to the
    # AGENTS.md operating-notes doc via `set-docs --agents`. Operator-authored
    # steering that does NOT touch agent code — the agent reads AGENTS.md from
    # the context_files snapshot after the provision restart.
    persona_doc: Path | None = None
    if args.agents:
        agents_path = Path(args.agents)
        if not agents_path.is_file():
            print(f"wt run: --agents file not found: {args.agents}", file=sys.stderr)
            return 2
        persona_doc = agents_path
    config = AgentConfig(
        agent_id="wt-cli",
        variant_id=label,
        system_prompt=system_prompt,
        persona_doc=persona_doc,
    )

    # Platform-specific bench prep (runtime-pluggable seam): the resolved
    # plugin's OPTIONAL pre_run() hook runs once here — after _build_runtime
    # and scenario loading, before any scenario executes. Platform glue
    # (bench servers, container prep, workspace seeding) lives in the driver
    # package's plugin module. Plugins decide applicability themselves by
    # inspecting scenario tags, so the CLI never special-cases a platform.
    pre_run = getattr(plugin, "pre_run", None)
    if pre_run is not None:
        pre_run(runtime, scenarios, runtime_name)

    _ERROR_CIRCUIT_LIMIT = 3

    any_fail = False
    consecutive_errors = 0
    first_error_logged = False
    completed: list[_CompletedAggregate] = []

    def _finish(rc: int) -> int:
        """Flush end-of-sweep side effects, on normal exit AND circuit-breaker abort.

        The ledger append happens unconditionally; the same records then feed
        `--format json` so the sweep document and the ledger cannot drift.
        """
        git_sha = _git_sha()
        wt_version = _wt_version()
        records = [
            _ledger_record(
                scenario=c.scenario,
                pack=c.pack,
                result=c.result,
                label=label,
                git_sha=git_sha,
                wt_version=wt_version,
            )
            for c in completed
        ]
        _append_ledger_records(runs_dir, records)
        for artifact in _dispatch_pack_end_hooks(hooks, config=config, completed=completed):
            _write_pack_hook_artifact(runs_dir, sweep_timestamp, artifact)
        if output_format is None or output_path is None:
            return rc
        try:
            _write_run_output(output_format, Path(output_path), completed, records)
        except OSError as exc:
            print(
                f"wt run: could not write {output_format} output to {output_path}: {exc}",
                file=sys.stderr,
            )
            return 1
        return rc

    for selected_entry in selected:
        scenario = selected_entry.scenario
        scenario_pack = selected_entry.pack
        # Wire the MCPServer only when the runtime can mount runner-managed
        # handles. Contract C, in_memory, and terminal-only runtimes own their
        # tool surfaces; an unused mock would create misleading empty evidence.
        scenario_mcps = None
        scenario_probe = None
        if (
            getattr(runtime, "accepts_runner_managed_mcps", True)
            and scenario_pack.mcp_factory is not None
        ):
            # Pass the scenario so scenario-aware factories (silent_failure,
            # which injects MOCK_MCP_FAILURE_MODE per scenario) can specialize.
            scenario_mcps = [scenario_pack.mcp_factory(scenario)]

        # External-state probes are independent of MCP mounting. Factories are
        # read from the owning pack after pre_run() because the runtime plugin
        # may have wired its live fixture-backed factory during that hook.
        if scenario_pack.state_probe_factory is not None:
            scenario_probe = scenario_pack.state_probe_factory(scenario)

        transport_only = scenario_pack.transport_only

        # Resilience: a single scenario's failure (e.g. a provision-time agent
        # readiness timeout, or a mock that won't start) MUST NOT abort a long
        # multi-dim sweep. run_scenario tears down in its own finally, so the next
        # scenario re-provisions from a clean slate. BUT a SYSTEMIC outage (a dead
        # queue or inference worker) would otherwise burn the full readiness timeout per
        # scenario across the whole sweep — so a circuit breaker aborts after N
        # consecutive errors, and the FIRST error logs a full traceback (the
        # per-scenario lines clip the message to 120 chars).
        try:
            result = run_scenario(
                scenario,
                runtime,
                mcps=scenario_mcps,
                config=config,
                runs_per_scenario=n_runs,
                state_probe=scenario_probe,
                hooks=hooks,
            )
        except WorldMismatchError as exc:
            any_fail = True
            consecutive_errors = 0
            print(f"  WORLD   {scenario.name:<40}  preconditions failed")
            print(f"wt run: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 — sweep-level isolation, detail printed
            any_fail = True
            consecutive_errors += 1
            print(f"  ERROR   {scenario.name:<40}  ({type(exc).__name__}: {str(exc)[:120]})")
            if not first_error_logged:
                first_error_logged = True
                traceback.print_exc()
            if consecutive_errors >= _ERROR_CIRCUIT_LIMIT:
                print(
                    f"wt run: aborting after {consecutive_errors} consecutive scenario "
                    f"errors — likely a systemic outage (e.g. the bench inference worker "
                    f"is down), not per-scenario flakiness. Fix the root cause and re-run "
                    f"rather than churning the remaining scenarios.",
                    file=sys.stderr,
                )
                return _finish(1)
            continue

        consecutive_errors = 0  # a successful scenario resets the breaker
        agg = result.aggregate
        for warning in getattr(result, "worker_warnings", []) or []:
            print(f"wt run: warning: {warning}", file=sys.stderr)

        # Save traces + score sidecars (so `wt report/compare/triage` can
        # consume the run output directly, without a re-scoring pass).
        for run_result in result.runs:
            path = storage_path(run_result.trace, base_dir=runs_dir)
            save_trace(run_result.trace, path)
            _write_score_sidecar(path, run_result.score, scenario)
            for artifact in getattr(run_result, "hook_artifacts", []) or []:
                _write_hook_artifact_sidecar(path, artifact)
        for artifact in getattr(result, "hook_artifacts", []) or []:
            _write_scenario_hook_artifact(
                runs_dir,
                sweep_timestamp,
                artifact,
                scenario.name,
            )

        # Note: run_scenario catches a send-time/runtime error INTERNALLY and
        # returns a failed aggregate carrying a `runner_error: …` worker_warning
        # (it does NOT raise). The transport-only exemption must cover only the
        # counterfactual MODEL VERDICT — a real EXECUTION error means no valid
        # model turn ran, so it must still fail the sweep even for these dims.
        had_runner_error = any(
            str(w).startswith("runner_error:")
            for run_result in result.runs
            for w in (getattr(run_result.trace, "worker_warnings", None) or [])
        )
        completed.append(
            _CompletedAggregate(
                pack=selected_entry.pack,
                scenario=scenario,
                result=result,
                transport_only=transport_only,
                had_runner_error=had_runner_error,
            )
        )

        status = agg.verdict
        if had_runner_error:
            note = "  ✗ EXECUTION ERROR (runner_error in trace — counts as a real failure)"
        elif transport_only:
            note = "  ⚠ transport-only (history-shaping perturbation post-hoc; not model signal)"
        else:
            note = ""
        print(
            f"  {status:<6}  {scenario.name:<40}  "
            f"({agg.passed}/{agg.total} pass, rate={agg.pass_rate:.0%}){note}"
        )
        # transport-only dims run faithfully but their MODEL verdict is not a
        # model-quality signal, so it doesn't flip the exit code — UNLESS the run
        # hit a real execution error (no valid model turn happened).
        if _counts_as_gate_failure(completed[-1]):
            any_fail = True

    return _finish(1 if any_fail else 0)


# ─── rescore ─────────────────────────────────────────────────────────────────

_SCORE_LAYERS = ("outcome", "trajectory", "constraint", "integrity")


def _cmd_rescore(args: argparse.Namespace) -> int:
    """Handle the `wt rescore` subcommand.

    Recomputes score layers from saved traces and current Scenario definitions.
    It never provisions a runtime and never modifies trace files.  Exit codes
    mirror `wt run`: 0 when all newly-scored gates pass, 1 when any newly
    scored gate fails or is invalid, and 2 for usage/configuration errors such as missing
    traces or unresolved scenario definitions.
    """
    from windtunnel.api.trace import load_trace  # noqa: PLC0415

    trace_paths = _rescore_trace_paths(args)
    if trace_paths is None:
        return 2

    pack_sources = args.pack_source or []
    packs = _discover_scenario_packs(pack_sources) if pack_sources else _discover_scenario_packs()
    selection = _select_scenarios(
        scenario_patterns=args.scenario or [],
        tag_filters=args.tag or [],
        pack_filters=args.pack or [],
        owner_filters=args.owner or [],
        packs=packs,
    )
    _print_selection_warnings(selection, command="wt rescore")
    scenarios_by_id = _rescore_scenario_map(selection.entries)
    if scenarios_by_id is None:
        return 2
    if not scenarios_by_id:
        print("wt rescore: no scenario definitions found.", file=sys.stderr)
        return 2

    changed = 0
    new_fail = 0
    invalid = 0
    unresolved = 0
    errors = 0
    written = 0
    skipped = 0

    for trace_path in trace_paths:
        try:
            trace = load_trace(trace_path)
        except Exception as exc:  # noqa: BLE001 - keep walking the corpus
            errors += 1
            print(f"{trace_path}: ERROR could not load trace ({exc})")
            continue

        if args.scenario and not any(
            fnmatch.fnmatchcase(trace.scenario_id, pattern) for pattern in args.scenario
        ):
            skipped += 1
            continue

        scenario = scenarios_by_id.get(trace.scenario_id)
        if scenario is None:
            unresolved += 1
            print(
                f"{trace_path}: ERROR no current scenario definition for "
                f"scenario_id={trace.scenario_id!r}"
            )
            continue

        old_score = _read_score_sidecar(trace_path)
        new_score = _score_saved_trace(trace, scenario)
        layer_parts: list[str] = []
        trace_changed = False
        for layer_name in _SCORE_LAYERS:
            old_verdict = _old_layer_verdict(old_score, layer_name)
            new_verdict = _score_layer_verdict(new_score, layer_name)
            if old_verdict != "UNKNOWN" and old_verdict != new_verdict:
                trace_changed = True
            layer_parts.append(f"{layer_name} {old_verdict} -> {new_verdict}")

        if trace_changed:
            changed += 1
        if not new_score.integrity.passed:
            invalid += 1
        elif not new_score.gate_passed(scenario.resolved_gate_layers()):
            new_fail += 1

        write_note = ""
        if args.write:
            _write_score_sidecar(
                trace_path,
                new_score,
                scenario,
                origin={
                    "kind": "rescore",
                    "rescored_at": _ledger_timestamp(),
                    "source_trace": str(trace_path),
                    "trace_unchanged": True,
                },
            )
            written += 1
            write_note = " [sidecar written]"

        print(f"{trace_path}: scenario={trace.scenario_id} " + " | ".join(layer_parts) + write_note)

    total = len(trace_paths)
    print(
        "summary: "
        f"traces={total} changed={changed} new_gate_failures={new_fail} invalid={invalid} "
        f"unresolved={unresolved} errors={errors} written={written} skipped={skipped}"
    )

    if unresolved or errors:
        return 2
    return 1 if new_fail or invalid else 0


def _rescore_trace_paths(args: argparse.Namespace) -> list[Path] | None:
    """Resolve --runs/--trace into trace JSON paths, excluding sidecars."""
    from windtunnel.api.trace import is_trace_json_path  # noqa: PLC0415

    explicit = [Path(p) for p in (args.trace or [])]
    if explicit:
        missing = [path for path in explicit if not path.is_file()]
        if missing:
            for path in missing:
                print(f"wt rescore: trace file not found: {path}", file=sys.stderr)
            return None
        return explicit

    runs_dir = Path(args.runs)
    if not runs_dir.is_dir():
        print(f"wt rescore: runs directory not found: {runs_dir}", file=sys.stderr)
        return None
    trace_paths = sorted(path for path in runs_dir.rglob("*.json") if is_trace_json_path(path))
    if not trace_paths:
        print(f"wt rescore: no trace files found under {runs_dir}", file=sys.stderr)
        return None
    return trace_paths


def _rescore_scenario_map(entries: list[_SelectedScenario]) -> dict[str, Scenario] | None:
    """Return scenario_id -> Scenario, or print ambiguity errors."""
    by_id: dict[str, list[_SelectedScenario]] = {}
    for entry in entries:
        name = str(getattr(entry.scenario, "name", ""))
        by_id.setdefault(name, []).append(entry)

    ambiguous = {name: values for name, values in by_id.items() if len(values) > 1}
    if ambiguous:
        for name, values in sorted(ambiguous.items()):
            packs = ", ".join(str(getattr(entry.pack, "name", "")) for entry in values)
            print(
                f"wt rescore: scenario_id {name!r} is ambiguous across packs: {packs}",
                file=sys.stderr,
            )
        print(
            "wt rescore: narrow definitions with --pack, --owner, or --tag.",
            file=sys.stderr,
        )
        return None

    return {name: values[0].scenario for name, values in by_id.items()}


def _score_saved_trace(trace: Trace, scenario: Scenario) -> Score:
    """Re-run all score layers derivable from a saved trace."""
    from windtunnel.api.evaluators import (  # noqa: PLC0415
        evaluate_constraint,
        evaluate_integrity,
        evaluate_outcome,
        evaluate_trajectory,
    )
    from windtunnel.api.score import Score  # noqa: PLC0415

    return Score(
        outcome=evaluate_outcome(trace, scenario),
        trajectory=evaluate_trajectory(trace, scenario),
        constraint=evaluate_constraint(trace, scenario),
        integrity=evaluate_integrity(trace, scenario),
        failure_cost=scenario.failure_cost,
    )


def _read_score_sidecar(trace_path: Path) -> dict[str, Any] | None:
    score_path = trace_path.with_suffix(".score.json")
    if not score_path.is_file():
        return None
    try:
        data = json.loads(score_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _old_layer_verdict(score_data: dict[str, Any] | None, layer_name: str) -> str:
    if score_data is None:
        return "UNKNOWN"
    layer = score_data.get(layer_name)
    if layer_name == "integrity" and not isinstance(layer, dict):
        layer = score_data.get("robustness")
    if not isinstance(layer, dict):
        nested = score_data.get("score")
        if isinstance(nested, dict):
            layer = nested.get(layer_name)
            if layer_name == "integrity" and not isinstance(layer, dict):
                layer = nested.get("robustness")
    if not isinstance(layer, dict) or "passed" not in layer:
        return "UNKNOWN"
    return "PASS" if bool(layer["passed"]) else "FAIL"


def _score_layer_verdict(score: Score, layer_name: str) -> str:
    layer = getattr(score, layer_name)
    return "PASS" if layer.passed else "FAIL"


# ─── triage ──────────────────────────────────────────────────────────────────


def _cmd_triage(args: argparse.Namespace) -> int:
    """Handle the `wt triage` subcommand.

    Walks a runs/ directory, classifies every failed run using a score.json
    sibling file, and emits a markdown report grouped by failure category with
    suggested fix vectors. Suitable as a weekly bench digest or Slack alert.

    Each trace must have a sibling <trace>.score.json file containing:
        {
          "scenario": { "name": ..., "prompt": ..., "target_facts": ...,
                        "requires_tool_use": ..., "tags": ...,
                        "must_call": ..., "forbidden_calls": ... },
          "score": {
              "outcome": {"passed": ..., "detail": ...},
              "trajectory": {"passed": ..., "detail": ...},
              "constraint": {"passed": ..., "detail": ...},
              "integrity": {"passed": ..., "detail": ...}
          }
        }
    Traces without a sibling score.json are skipped.

    Exit code: 0 (always — triage is informational, not a gate).
    """
    import json

    from windtunnel.api.scenario import Scenario  # noqa: PLC0415
    from windtunnel.api.score import ScoreFormatError, score_from_dict  # noqa: PLC0415
    from windtunnel.api.trace import is_trace_json_path, load_trace  # noqa: PLC0415

    runs_dir = Path(args.runs)
    classifier_name: str = args.classifier

    if not runs_dir.exists():
        print(f"wt triage: runs directory not found: {runs_dir}", file=sys.stderr)
        return 0

    # Build classifier
    if classifier_name == "rule_based":
        from windtunnel.triage.rule_based import RuleBasedClassifier  # noqa: PLC0415

        clf: FailureClassifier = RuleBasedClassifier()
    else:
        print(f"wt triage: unknown classifier {classifier_name!r}", file=sys.stderr)
        return 2

    # Walk runs/ for trace JSON files
    trace_files = sorted(f for f in runs_dir.rglob("*.json") if is_trace_json_path(f))

    if not trace_files:
        print("# Wind Tunnel Triage Report\n\nNo runs found.")
        return 0

    # Classify each failed run
    # Groups: category → list of (scenario_id, trace, classification)
    by_category: dict[str, list[tuple[str, str, FailureClassification]]] = {}
    skipped = 0
    passed = 0
    invalid = 0

    for trace_path in trace_files:
        score_path = trace_path.with_suffix(".score.json")
        if not score_path.exists():
            skipped += 1
            continue

        try:
            trace = load_trace(trace_path)
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue

        # Build Score
        try:
            score = score_from_dict(score_data)
        except ScoreFormatError:
            skipped += 1
            continue

        # Build Scenario from stored data
        try:
            sc_data = score_data["scenario"]
            scenario = Scenario(
                name=sc_data["name"],
                prompt=sc_data.get("prompt", ""),
                user_turns=sc_data.get("user_turns", []),
                target_facts=sc_data.get("target_facts", []),
                requires_tool_use=sc_data.get("requires_tool_use", False),
                tags=sc_data.get("tags", []),
                must_call=sc_data.get("must_call", []),
                forbidden_calls=sc_data.get("forbidden_calls", []),
                gate_layers=sc_data.get("gate_layers"),
            )
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        if not score.integrity.passed:
            invalid += 1
            continue
        if score.gate_passed(scenario.resolved_gate_layers()):
            passed += 1
            continue

        classification = clf.classify(scenario, trace, score)
        cat = classification.category
        by_category.setdefault(cat, []).append((scenario.name, trace.run_id[:8], classification))

    # Emit markdown report
    total_failed = sum(len(v) for v in by_category.values())
    print("# Wind Tunnel Triage Report\n")
    print(
        f"**Failed runs:** {total_failed}  "
        f"**Passed:** {passed}  "
        f"**Invalid:** {invalid}  "
        f"**Skipped (no score):** {skipped}  "
        f"**Classifier:** `{classifier_name}`\n"
    )

    if not by_category:
        print("No failures to triage.")
        return 0

    # Sort categories: unknown last, others by count descending
    def _sort_key(
        item: tuple[str, list[tuple[str, str, FailureClassification]]],
    ) -> tuple[int, int]:
        cat, entries = item
        return (1 if cat == "unknown" else 0, -len(entries))

    for category, entries in sorted(by_category.items(), key=_sort_key):
        print(f"## `{category}` ({len(entries)} failure{'s' if len(entries) != 1 else ''})\n")

        # Emit fix suggestion from first entry with one
        fix_shown = False
        for _name, _run_id, clf_result in entries:
            if not fix_shown and clf_result.suggested_fix is not None:
                fix = clf_result.suggested_fix
                print(f"**Suggested fix vector:** `{fix.fix_vector}`")
                print(f"**Rationale:** {fix.rationale}\n")
                fix_shown = True
                break
        # Table of failures
        print("| Scenario | Run ID | Confidence | Evidence |")
        print("|----------|--------|-----------|---------|")
        for name, run_id, clf_result in entries:
            conf = f"{clf_result.confidence:.0%}"
            ev = "; ".join(clf_result.evidence[:2]) if clf_result.evidence else ""
            ev = ev[:80] + "..." if len(ev) > 80 else ev
            print(f"| `{name}` | `{run_id}` | {conf} | {ev} |")
        print()

    return 0


# ─── replay ──────────────────────────────────────────────────────────────────


def _cmd_replay(args: argparse.Namespace) -> int:
    """Handle the `wt replay` subcommand.

    Loads a captured trace and replays it against the specified runtime.
    Writes the new trace to runs/ and prints the score comparison.
    """
    from windtunnel.api.trace import load_trace, save_trace, storage_path  # noqa: PLC0415

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"wt replay: trace file not found: {trace_path}", file=sys.stderr)
        return 2

    original = load_trace(trace_path)
    runtime_name: str = args.runtime
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    print(f"Replaying trace: {trace_path.name}")
    print(f"  scenario_id:  {original.scenario_id}")
    print(f"  agent_id:     {original.agent_id}")
    print(f"  variant_id:   {original.variant_id}")
    print(f"  turns:        {len(original.turns)}")
    print(f"  runtime:      {runtime_name}")

    runtime = _build_runtime(runtime_name, f"replay_{runtime_name}", soul_path=None)

    # Build a minimal echo scenario from the trace
    from windtunnel.api.runner import run_scenario  # noqa: PLC0415
    from windtunnel.api.scenario import Scenario  # noqa: PLC0415
    from windtunnel.spi.agent_runtime import AgentConfig  # noqa: PLC0415

    # Extract the last user turn as the replay prompt
    user_turns = [t.content for t in original.turns if t.role == "user"]
    prompt = user_turns[-1] if user_turns else "(no user turns)"

    replay_scenario = Scenario(
        name=original.scenario_id,
        prompt=prompt,
        target_facts=[],  # No scoring on replay — just record the trace
    )
    config = AgentConfig(
        agent_id=original.agent_id,
        variant_id=f"replay_{runtime_name}",
    )

    result = run_scenario(replay_scenario, runtime, config=config)
    new_trace = result.runs[0].trace
    out_path = storage_path(new_trace, base_dir=runs_dir)
    save_trace(new_trace, out_path)
    _write_score_sidecar(out_path, result.runs[0].score, replay_scenario)
    print(f"  → replayed trace saved: {out_path}")
    return 0


# ─── doctor ──────────────────────────────────────────────────────────────────


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Handle the `wt doctor` subcommand.

    `wt doctor` is the one-command "is this endpoint conformant" check an
    operator runs after standing up a stack: resolve --runtime exactly the
    way `wt run` does, then run the reset-isolation canary
    (windtunnel.api.canary.run_reset_canary) against it in RECALL mode —
    doctor is a bring-up tool for a box with a live model, and there is no
    portable way for the CLI to conjure a StateProbe for an arbitrary
    runtime. Hermetic (recall-free) canary runs stay library/pytest-only:
    importing run_reset_canary(..., probe_recall=False, state_probe=...)
    directly is what belongs in a driver repo's CI, where no live model is
    available. See docs/writing-a-runtime.md for both.
    """
    from windtunnel.api.canary import run_reset_canary  # noqa: PLC0415
    from windtunnel.spi.agent_runtime import AgentConfig  # noqa: PLC0415

    runtime_name: str = args.runtime
    label: str = args.label or "wt_doctor"

    # Mirrors wt run's --soul handling: the flag is a PATH, the file content
    # is threaded into AgentConfig.system_prompt (not passed to build()).
    system_prompt: str | None = None
    if args.soul:
        soul_path = Path(args.soul)
        if not soul_path.is_file():
            print(f"wt doctor: --soul file not found: {args.soul}", file=sys.stderr)
            return 2
        system_prompt = soul_path.read_text()

    # _build_runtime resolves the plugin via _resolve_runtime_plugin exactly
    # like `wt run` (same built-in/entry-point/dotted-path order, same exit-2
    # behavior on an unresolvable name — see _resolve_runtime_plugin).
    runtime = _build_runtime(runtime_name, label, soul_path=args.soul)
    config = AgentConfig(
        agent_id="wt-doctor",
        variant_id=label,
        system_prompt=system_prompt,
    )

    print(
        f"wt doctor: probing runtime {runtime_name!r} for reset-isolation "
        "leaks (recall mode — requires a live model)..."
    )
    try:
        result = run_reset_canary(runtime, config)
    except RuntimeError as exc:
        print(f"wt doctor: {exc}", file=sys.stderr)
        return 1

    print(f"  nonce:  {result.nonce}")
    print(f"  {result.detail}")
    if result.leaked:
        print(f"  evidence: {len(result.evidence)} response(s) contained the nonce")
        return 1
    return 0


# ─── surface ─────────────────────────────────────────────────────────────────


def _probe_runtime_surface(args: argparse.Namespace) -> dict[str, Any] | None:
    """Provision the runtime, probe its surface with the run-time timing
    (reset first, then probe), tear down. Returns the surface block, or
    None when the handle has no surface introspection at all."""
    from windtunnel.api.runner import _capture_surface  # noqa: PLC0415
    from windtunnel.spi.agent_runtime import AgentConfig  # noqa: PLC0415

    label: str = args.label or "wt_surface"
    system_prompt: str | None = None
    if args.soul:
        soul_path = Path(args.soul)
        if not soul_path.is_file():
            print(f"wt surface: --soul file not found: {args.soul}", file=sys.stderr)
            sys.exit(2)
        system_prompt = soul_path.read_text()

    runtime = _build_runtime(args.runtime, label, soul_path=args.soul)
    config = AgentConfig(
        agent_id="wt-surface",
        variant_id=label,
        system_prompt=system_prompt,
    )
    handle = runtime.provision(config)
    try:
        handle.reset_state()
        block, _warnings = _capture_surface(handle)
    finally:
        try:
            handle.teardown()
        except Exception:
            pass
    return block


def _describe_absent_surface(block: dict[str, Any] | None) -> str:
    if block is None:
        return "runtime has no surface introspection (describe_surface not implemented)"
    if block.get("status") == "unavailable":
        return "endpoint reports no surface (status: unavailable)"
    detail = block.get("detail") or "unspecified"
    return f"surface INVALID: {detail}"


def _cmd_surface(args: argparse.Namespace) -> int:
    """Handle the `wt surface` subcommand.

    Two intents, two invocations — strictness is never a config knob:
    `diff` informs and always exits 0 on a successful comparison; `check`
    is the CI gate and exits 1 on ANY change (or an invalid/absent surface
    where the golden promises one). The golden stores per-segment hashes
    only, unless the operator opts into the text sidecar with
    --store-text. The hash is a tripwire, never a skip-token: a surface
    change forces a bench run; an unchanged surface proves nothing.
    """
    from windtunnel.api.surface import (  # noqa: PLC0415
        SurfaceGoldenError,
        build_surface_golden,
        diff_surface_goldens,
        parse_surface_golden,
    )

    action = args.surface_action
    if action is None:
        print("wt surface: an action is required: record | diff | check", file=sys.stderr)
        return 2

    golden_path = Path(args.golden)
    block = _probe_runtime_surface(args)

    if action == "record":
        try:
            golden = build_surface_golden(block or {}, store_text=args.store_text)
        except SurfaceGoldenError:
            print(f"wt surface record: {_describe_absent_surface(block)}", file=sys.stderr)
            return 1
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(
            json.dumps(golden, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        mode = "hashes + full text (SENSITIVE)" if args.store_text else "hashes only"
        print(f"wt surface: golden recorded ({mode}): {golden_path}")
        print(
            f"  tools: {len(golden['tool_order'])} "
            f"· extra segments: {len(golden['extra_segments'])}"
        )
        return 0

    # diff / check share everything except the exit code.
    if not golden_path.is_file():
        print(
            f"wt surface {action}: golden not found: {golden_path} (run `wt surface record` first)",
            file=sys.stderr,
        )
        return 2
    try:
        golden = parse_surface_golden(json.loads(golden_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, SurfaceGoldenError) as exc:
        print(f"wt surface {action}: unusable golden {golden_path}: {exc}", file=sys.stderr)
        return 2

    failing = action == "check"
    if block is None or block.get("status") not in ("reported", "rendered"):
        print(f"wt surface {action}: {_describe_absent_surface(block)}")
        print("  golden promises a surface — treat as a change requiring attention.")
        return 1 if failing else 0

    changes = diff_surface_goldens(golden, build_surface_golden(block))
    if not changes:
        print(f"wt surface {action}: surface matches golden ({golden_path})")
        return 0
    print(f"wt surface {action}: {len(changes)} change(s) vs {golden_path}:")
    for change in changes:
        print(f"  - {change}")
    print("  surface diff ⇒ bench run before merge (then re-record the golden).")
    return 1 if failing else 0


# ─── import ──────────────────────────────────────────────────────────────────


def _cmd_import(args: argparse.Namespace) -> int:
    """Handle the `wt import` subcommand.

    Reads a Contract A ``*.wtin.json`` envelope and emits a self-contained
    scenario skeleton.  The command is intentionally usage-strict: missing
    inputs, invalid envelopes, and unsafe output-directory reuse all exit 2.
    """
    from windtunnel.api.importer import write_imported_scenario  # noqa: PLC0415
    from windtunnel.api.interchange import InterchangeFormatError, load_interchange  # noqa: PLC0415
    from windtunnel.api.universe import UniverseFormatError  # noqa: PLC0415

    trace_path = Path(args.trace)
    if not trace_path.is_file():
        print(f"wt import: trace file not found: {trace_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    if out_dir.exists() and not out_dir.is_dir():
        print(f"wt import: --out must be a directory: {out_dir}", file=sys.stderr)
        return 2
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        print(
            f"wt import: --out directory is not empty: {out_dir} "
            "(pass --force to overwrite generated files)",
            file=sys.stderr,
        )
        return 2

    try:
        envelope = load_interchange(trace_path)
        result = write_imported_scenario(envelope, out_dir)
    except (OSError, InterchangeFormatError, UniverseFormatError) as exc:
        print(f"wt import: {exc}", file=sys.stderr)
        return 2

    print(f"wrote: {result.out_dir}", file=sys.stderr)
    return 0


# ─── validate ────────────────────────────────────────────────────────────────


def _cmd_validate(args: argparse.Namespace) -> int:
    """Handle the `wt validate` subcommand.

    A thin wrapper over `parse_interchange` and `lint_interchange` — it does
    not duplicate any parsing, validation, or lint logic. For each path,
    prints one line: `OK <path>` or `INVALID <path>: <error>`, followed by
    one `WARN <path>: <message>` line per lint hit on envelopes that parsed.
    A missing file is a usage error (exit 2), matching `wt import`'s
    conventions; a well-formed-but-invalid envelope is a validation failure
    (exit 1), not a usage error. Warnings do not affect the exit code unless
    `--strict` is given, in which case any warning also exits 1.
    """
    from windtunnel.api.interchange import (  # noqa: PLC0415
        InterchangeFormatError,
        lint_interchange,
        parse_interchange,
    )

    paths = [Path(p) for p in args.paths]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        for p in missing:
            print(f"wt validate: file not found: {p}", file=sys.stderr)
        return 2

    all_valid = True
    any_warnings = False
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"INVALID {path}: invalid JSON: {exc}")
            all_valid = False
            continue
        try:
            parse_interchange(raw)
        except InterchangeFormatError as exc:
            print(f"INVALID {path}: {exc}")
            all_valid = False
            continue
        print(f"OK {path}")
        for warning in lint_interchange(raw):
            print(f"WARN {path}: {warning}")
            any_warnings = True

    if not all_valid:
        return 1
    if args.strict and any_warnings:
        return 1
    return 0


def _installed_skill_dir() -> Path:
    """Return the installed Wind Tunnel skill directory as a filesystem path."""
    skill = resources.files("windtunnel").joinpath("skill")
    if not skill.is_dir():
        raise FileNotFoundError("windtunnel skill resources are not installed")
    return Path(str(skill)).resolve()


def _cmd_skill(args: argparse.Namespace) -> int:
    """Handle the `wt skill` subcommand."""
    action = getattr(args, "skill_action", None)
    if action == "path":
        try:
            print(_installed_skill_dir())
        except OSError as exc:
            print(f"wt skill path: {exc}", file=sys.stderr)
            return 1
        return 0

    if action == "install":
        try:
            source = _installed_skill_dir()
        except OSError as exc:
            print(f"wt skill install: {exc}", file=sys.stderr)
            return 1

        dest_dir = Path(args.dest)
        target = dest_dir / "windtunnel"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if args.copy:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
                print(target.resolve())
                return 0

            if target.is_symlink():
                target.unlink()
            elif target.exists():
                print(
                    "wt skill install: refusing to overwrite existing non-symlink "
                    f"destination: {target}",
                    file=sys.stderr,
                )
                print(
                    "Use --copy to replace it with a standalone copy that may go stale.",
                    file=sys.stderr,
                )
                return 1

            target.symlink_to(source, target_is_directory=True)
            print(target.resolve())
            return 0
        except OSError as exc:
            print(f"wt skill install: {exc}", file=sys.stderr)
            return 1

    print("wt skill: expected one of: path, install", file=sys.stderr)
    return 2


# ─── main ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the wt argparse tree used by both the CLI and generated docs."""
    parser = argparse.ArgumentParser(
        prog="wt",
        description="Wind Tunnel — unittest for agents.",
    )
    sub = parser.add_subparsers(dest="command")

    # ── report ───────────────────────────────────────────────────────────────
    report_p = sub.add_parser("report", help="Generate a report from a runs/ directory.")
    report_p.add_argument(
        "--runs",
        default="runs",
        metavar="DIR",
        help="Path to the runs/ directory (default: ./runs)",
    )
    report_p.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="Output path for file formats (HTML default: report.html).",
    )
    report_p.add_argument(
        "--format",
        default="html",
        choices=["html", "markdown", "json"],
        help="Output format: html (default), markdown, or json.",
    )

    # ── compare ──────────────────────────────────────────────────────────────
    compare_p = sub.add_parser("compare", help="Compare results across variant labels.")
    compare_p.add_argument(
        "--labels",
        nargs="+",
        metavar="LABEL",
        default=[],
        help="Variant labels to compare (space-separated); the first label is the baseline.",
    )
    compare_p.add_argument(
        "--runs",
        default="runs",
        metavar="DIR",
        dest="runs",
        help="Path to the runs/ directory (default: ./runs)",
    )

    # ── run ──────────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run scenarios against a runtime.")
    run_p.add_argument(
        "--scenario",
        action="append",
        metavar="S",
        default=None,
        help="Scenario name(s) to run. Repeat for multiple. "
        "Omit to run all registered scenarios (the built-in "
        "dims plus any pack installed under the "
        "'windtunnel.scenario_packs' entry-point group). "
        "Shell-style globs such as 'lookup_*' are supported.",
    )
    run_p.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        default=None,
        help="Run scenarios carrying TAG. Repeat for OR matching "
        "within tags; composes with other selectors by AND.",
    )
    run_p.add_argument(
        "--pack",
        action="append",
        metavar="PACK",
        default=None,
        help="Run scenarios from pack PACK. Repeat for OR matching "
        "within packs; composes with other selectors by AND.",
    )
    run_p.add_argument(
        "--pack-source",
        action="append",
        metavar="SOURCE",
        default=None,
        help="Load an additional local scenario pack from module:attr "
        "or path/to/file.py:attr. Repeat for multiple sources; "
        "use --pack to select it by name.",
    )
    run_p.add_argument(
        "--owner",
        action="append",
        metavar="OWNER",
        default=None,
        help="Run scenarios from packs whose owner matches OWNER. "
        "Repeat for OR matching within owners; composes with "
        "other selectors by AND.",
    )
    run_p.add_argument(
        "--soul", default=None, metavar="PATH", help="Path to SOUL.md / persona doc to inject."
    )
    run_p.add_argument(
        "--agents",
        default=None,
        metavar="PATH",
        help="Path to an AGENTS.md operating-notes doc to inject "
        "(routed to set-docs --agents; does not touch agent code).",
    )
    run_p.add_argument(
        "--runtime",
        default="in_memory",
        metavar="RUNTIME",
        help="Runtime to use (default: in_memory). Either the "
        "built-in 'in_memory' (zero-infrastructure scripted "
        "runtime — no network; useful for learning the "
        "scoring model and testing scenario definitions in "
        "CI), the name of an installed runtime plugin "
        "(discovered via the 'windtunnel.runtimes' entry-"
        "point group — e.g. 'acme' from a platform driver "
        "package), or a 'module:attr' "
        "dotted path to a RuntimePlugin instance or class.",
    )
    run_p.add_argument(
        "--hook",
        action="append",
        metavar="HOOK",
        default=None,
        help="Lifecycle hook to activate for this run. Repeat for "
        "multiple hooks; built-ins include 'debrief'.",
    )
    run_p.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help="Variant label for this run (recorded in traces).",
    )
    run_p.add_argument(
        "--runs",
        dest="n_runs",
        type=_positive_int,
        default=1,
        metavar="N",
        help="Number of runs per scenario (default: 1).",
    )
    run_p.add_argument(
        "--runs-dir",
        default="runs",
        metavar="DIR",
        help="Directory to write trace files (default: ./runs).",
    )
    run_p.add_argument(
        "--format",
        choices=["junit", "json"],
        default=None,
        help="Machine-readable run output format. Must be paired with --out.",
    )
    run_p.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="Path for --format junit/json output. Must be paired with --format.",
    )

    # ── rescore ──────────────────────────────────────────────────────────────
    rescore_p = sub.add_parser(
        "rescore",
        help="Re-score saved traces against current scenario definitions.",
    )
    rescore_input = rescore_p.add_mutually_exclusive_group(required=True)
    rescore_input.add_argument(
        "--runs",
        default=None,
        metavar="DIR",
        help="Walk a runs/ directory and re-score every saved trace.",
    )
    rescore_input.add_argument(
        "--trace",
        nargs="+",
        metavar="PATH",
        default=None,
        help="Explicit trace JSON path(s) to re-score.",
    )
    rescore_p.add_argument(
        "--write",
        action="store_true",
        help="Update .score.json sidecars. Trace files are never modified.",
    )
    rescore_p.add_argument(
        "--scenario",
        action="append",
        metavar="S",
        default=None,
        help="Only re-score traces whose scenario_id matches S. Repeat for multiple; "
        "shell-style globs such as 'lookup_*' are supported.",
    )
    rescore_p.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        default=None,
        help="Restrict scenario definitions to packs/scenarios carrying TAG.",
    )
    rescore_p.add_argument(
        "--pack",
        action="append",
        metavar="PACK",
        default=None,
        help="Restrict scenario definitions to pack PACK.",
    )
    rescore_p.add_argument(
        "--pack-source",
        action="append",
        metavar="SOURCE",
        default=None,
        help="Load an additional local scenario pack from module:attr "
        "or path/to/file.py:attr before resolving traces.",
    )
    rescore_p.add_argument(
        "--owner",
        action="append",
        metavar="OWNER",
        default=None,
        help="Restrict scenario definitions to packs whose owner matches OWNER.",
    )

    # ── replay ───────────────────────────────────────────────────────────────
    replay_p = sub.add_parser("replay", help="Replay a captured trace against a runtime.")
    replay_p.add_argument(
        "--trace", required=True, metavar="PATH", help="Path to the trace JSON file to replay."
    )
    replay_p.add_argument(
        "--runtime",
        default="in_memory",
        metavar="RUNTIME",
        help="Runtime to replay against: built-in 'in_memory', "
        "an installed plugin name (entry-point group "
        "'windtunnel.runtimes'), or a 'module:attr' "
        "dotted path to a RuntimePlugin.",
    )
    replay_p.add_argument(
        "--runs-dir",
        default="runs",
        metavar="DIR",
        help="Directory to write replayed traces (default: ./runs).",
    )

    # ── doctor ───────────────────────────────────────────────────────────────
    doctor_p = sub.add_parser(
        "doctor",
        help="Bring-up check: run the reset-isolation canary against a live runtime.",
    )
    doctor_p.add_argument(
        "--runtime",
        default="in_memory",
        metavar="RUNTIME",
        help="Runtime to check (default: in_memory). Resolved "
        "exactly like `wt run --runtime`: built-in "
        "'in_memory', an installed plugin name (entry-"
        "point group 'windtunnel.runtimes'), or a "
        "'module:attr' dotted path to a RuntimePlugin. "
        "Runs the canary in RECALL mode, which requires "
        "a live model behind the runtime — doctor is a "
        "bring-up tool, not a CI check. For CI runners "
        "without a live model, call "
        "run_reset_canary(..., probe_recall=False, "
        "state_probe=...) directly from pytest instead.",
    )
    doctor_p.add_argument(
        "--soul",
        default=None,
        metavar="PATH",
        help="Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`).",
    )
    doctor_p.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help="Variant label recorded for this check (default: wt_doctor).",
    )

    # ── surface ──────────────────────────────────────────────────────────────
    surface_p = sub.add_parser(
        "surface",
        help="Record or compare the agent's prompt-surface golden "
        "(surface diff ⇒ bench run before merge).",
    )
    surface_sub = surface_p.add_subparsers(dest="surface_action")

    def _add_surface_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--runtime",
            default="in_memory",
            metavar="RUNTIME",
            help="Runtime to probe (default: in_memory). Resolved "
            "exactly like `wt run --runtime`. The probe "
            "provisions, resets, asks describe_surface(), and "
            "tears down — no scenarios run, no model calls.",
        )
        p.add_argument(
            "--soul",
            default=None,
            metavar="PATH",
            help="Path to SOUL.md / persona doc to inject (mirrors `wt run --soul`).",
        )
        p.add_argument(
            "--label",
            default=None,
            metavar="LABEL",
            help="Variant label for the probe (default: wt_surface).",
        )
        p.add_argument(
            "--golden",
            default="surface.golden.json",
            metavar="PATH",
            help="Golden file path (default: surface.golden.json).",
        )

    surface_record_p = surface_sub.add_parser(
        "record",
        help="Probe the runtime's surface and write the golden "
        "(per-segment hashes; no prompt text unless --store-text).",
    )
    _add_surface_args(surface_record_p)
    surface_record_p.add_argument(
        "--store-text",
        action="store_true",
        help="ALSO store the full segment text in the golden. The text is a "
        "human-facing sidecar — comparison only ever reads hashes — and "
        "it embeds the complete prompt surface: treat the file as "
        "sensitively as the system prompt itself.",
    )

    surface_diff_p = surface_sub.add_parser(
        "diff",
        help="Show per-segment changes vs the golden. Informative: exits 0 "
        "even when the surface changed.",
    )
    _add_surface_args(surface_diff_p)

    surface_check_p = surface_sub.add_parser(
        "check",
        help="CI gate: exit 1 on ANY surface change (or an invalid/absent "
        "surface where the golden promises one). A change means: bench "
        "before merge. An unchanged surface proves nothing — never use "
        "a passing check to skip runs.",
    )
    _add_surface_args(surface_check_p)

    # ── import ───────────────────────────────────────────────────────────────
    import_p = sub.add_parser(
        "import",
        help="Generate a scenario skeleton from a Contract A *.wtin.json trace.",
    )
    import_p.add_argument(
        "--trace",
        required=True,
        metavar="PATH",
        help="Path to the Contract A *.wtin.json trace envelope.",
    )
    import_p.add_argument(
        "--out",
        required=True,
        metavar="DIR",
        help="Directory to write scenario.py, scorer.py, fixture.universe.json, and IMPORTED.md.",
    )
    import_p.add_argument(
        "--force", action="store_true", help="Allow writing into an existing non-empty directory."
    )

    # ── validate ─────────────────────────────────────────────────────────────
    validate_p = sub.add_parser(
        "validate",
        help="Validate Contract A *.wtin.json interchange envelope(s).",
    )
    validate_p.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Path(s) to *.wtin.json envelope file(s) to validate.",
    )
    validate_p.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any file produces a lint warning (e.g. "
        "truncated/redacted values, unpaired tool_call_response "
        "ids), not only on schema errors.",
    )

    # ── triage ───────────────────────────────────────────────────────────────
    triage_p = sub.add_parser(
        "triage",
        help="Classify failed runs and emit a markdown report grouped by failure category.",
    )
    triage_p.add_argument(
        "--runs",
        default="runs",
        metavar="DIR",
        help="Path to the runs/ directory (default: ./runs). "
        "Each trace must have a sibling .score.json file.",
    )
    triage_p.add_argument(
        "--classifier",
        default="rule_based",
        choices=["rule_based"],
        help="Classifier to use (default: rule_based, deterministic).",
    )

    # ── skill ────────────────────────────────────────────────────────────────
    skill_p = sub.add_parser(
        "skill",
        help="Print or install the packaged Wind Tunnel agent skill.",
    )
    skill_sub = skill_p.add_subparsers(dest="skill_action")
    skill_sub.add_parser(
        "path",
        help="Print the absolute path of the installed Wind Tunnel skill directory.",
    )
    skill_install_p = skill_sub.add_parser(
        "install",
        help="Install the Wind Tunnel skill into an agent skills directory.",
    )
    skill_install_p.add_argument(
        "--dest",
        default=".agents/skills",
        metavar="DIR",
        help="Directory that will receive a windtunnel skill entry (default: .agents/skills).",
    )
    skill_install_p.add_argument(
        "--copy",
        action="store_true",
        help="Copy instead of symlinking. The copy survives package uninstall but may go stale.",
    )

    return parser


def _positive_int(raw: str) -> int:
    """Parse an integer CLI argument that must be greater than zero."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0 = all pass, non-zero = regression/error)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "report":
        return _cmd_report(args)
    if args.command == "compare":
        return _cmd_compare(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "rescore":
        return _cmd_rescore(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "surface":
        return _cmd_surface(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "triage":
        return _cmd_triage(args)
    if args.command == "skill":
        return _cmd_skill(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
