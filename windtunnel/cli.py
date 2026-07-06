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
    wt triage   [--runs DIR] [--classifier rule_based|llm_judge]
    wt skill    path | install [--dest DIR] [--copy]

Design: argparse (stdlib) — no click dependency. Each subcommand is a
function; main() is the dispatch entry point. Exit code 0 = all pass,
non-zero = any regression or error.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib
import importlib.util
import json
import shutil
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class _SelectedScenario:
    """A Scenario paired with the ScenarioPack that contributed it.

    The run loop mostly cares about the Scenario itself, but CI output and
    pack/owner selection need the pack boundary that was lost when the old
    helper flattened everything into a bare Scenario list.
    """

    pack: object
    scenario: object


@dataclass(frozen=True)
class _SelectionResult:
    """The selected scenarios plus selector values that matched nothing.

    Missing values are reported as usage-adjacent diagnostics without making a
    partially matching selection fail. That preserves the historical
    --scenario behavior: unknown names are printed, but known names still run;
    only an empty final selection exits 2.
    """

    entries: list[_SelectedScenario]
    unmatched_scenarios: list[str]
    unmatched_tags: list[str]
    unmatched_packs: list[str]
    unmatched_owners: list[str]


@dataclass(frozen=True)
class _CompletedAggregate:
    """A completed scenario aggregate with the metadata sweep writers need.

    One collection feeds every end-of-sweep side effect: _ledger_record()
    rows go to the ledger AND to `wt run --format json --out ...` (same
    records by construction), while the JUnit writer reads the aggregate
    plus the transport-only/runner-error context directly.
    """

    pack: object
    scenario: object
    result: object
    transport_only: bool
    had_runner_error: bool

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
    from windtunnel.report import load_runs  # noqa: PLC0415

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
    by_label: dict[str, list[tuple[str, dict]]] = {lbl: [] for lbl in labels}
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

    any_regression = False
    for sid in sorted(all_scenarios):
        row = f"{sid:<40} "
        for lbl in labels:
            cell_map = dict(by_label[lbl])
            cell = cell_map.get(sid)
            if cell is None:
                row += f"{'N/A':<17}"
            else:
                score = cell.get("score") or {}
                outcome = score.get("outcome", {})
                passed = outcome.get("passed", False)
                status = "PASS" if passed else "FAIL"
                if not passed:
                    any_regression = True
                row += f"{status:<17}"
        print(row)

    return 1 if any_regression else 0


# ─── run ─────────────────────────────────────────────────────────────────────

def _write_score_sidecar(trace_path: Path, score, scenario, *, origin: dict | None = None) -> Path:
    """Write the `.score.json` sidecar next to a saved trace.

    The sidecar is the union of BOTH consumer shapes, so one file feeds all
    built-in commands:
      - `wt report` / `wt compare` (report.load_runs → _cell_from_run) read the
        FLAT top-level layer keys: outcome/trajectory/constraint/robustness
        (each {"passed","detail"}) + failure_cost.
      - `wt triage` (_cmd_triage) reads the NESTED keys: "score" (the same four
        layers) + "scenario" (enough Scenario fields to rebuild it for the
        classifier).
    Each consumer ignores the other's keys.
    """
    import json  # noqa: PLC0415

    from windtunnel.api.score import score_to_dict  # noqa: PLC0415

    flat = score_to_dict(score)
    sidecar = {
        **flat,
        "score": flat,
        "scenario": {
            "name": getattr(scenario, "name", ""),
            "prompt": getattr(scenario, "prompt", ""),
            "target_facts": getattr(scenario, "target_facts", []),
            "requires_tool_use": getattr(scenario, "requires_tool_use", False),
            "tags": list(getattr(scenario, "tags", []) or []),
            "must_call": getattr(scenario, "must_call", []),
            "forbidden_calls": getattr(scenario, "forbidden_calls", []),
        },
    }
    if origin is not None:
        sidecar["origin"] = origin
    score_path = trace_path.with_suffix(".score.json")
    score_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return score_path


def _ledger_timestamp() -> str:
    """Return the UTC timestamp format used in append-only ledger rows."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _origin_from_tags(tags: list[str] | None) -> str | None:
    """Extract the first best-effort origin:<ref> tag from a scenario."""
    for tag in tags or []:
        if tag.startswith("origin:") and tag != "origin:":
            return tag.removeprefix("origin:")
    return None


def _git_sha() -> str | None:
    """Best-effort current git SHA for the CLI ledger, or None on any failure."""
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _wt_version() -> str:
    """Best-effort installed package version, with a source-tree fallback.

    Editable and source-tree runs can lack distribution metadata depending on
    how the checkout was invoked. Falling back to pyproject.toml keeps ledger
    rows useful without making package metadata resolution part of the run gate.
    """
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("windtunnel-bench")
    except PackageNotFoundError:
        pass

    try:
        import tomllib  # noqa: PLC0415

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, TypeError, ValueError):
        return "0+unknown"


def _ledger_record(
    *,
    scenario,
    pack,
    result,
    label: str,
    git_sha: str | None,
    wt_version: str,
) -> dict:
    """Build one ledger row for a scenario aggregate.

    The ledger is intentionally mechanism-only: the CLI records aggregate facts
    that downstream tools can query, but it does not attach retention, trend, or
    gating semantics to the row.
    """
    agg = result.aggregate
    first_trace = result.runs[0].trace if result.runs else None

    return {
        "ts": _ledger_timestamp(),
        "scenario_id": scenario.name,
        "pack": getattr(pack, "name", None),
        "owner": getattr(pack, "owner", None),
        "label": label,
        "model": getattr(first_trace, "model", None),
        "quant": getattr(first_trace, "quant", None),
        "verdict": agg.verdict,
        "runs": agg.total,
        "layer_pass_rates": {
            "outcome": agg.outcome_pass_rate,
            "trajectory": agg.trajectory_pass_rate,
            "constraint": agg.constraint_pass_rate,
            "robustness": agg.robustness_pass_rate,
        },
        "run_ids": [run.trace.run_id for run in result.runs],
        "origin": _origin_from_tags(getattr(scenario, "tags", []) or []),
        "git_sha": git_sha,
        "wt_version": wt_version,
    }


def _append_ledger_records(runs_dir: Path, records: list[dict]) -> None:
    """Append scenario-aggregate rows to <runs-dir>/ledger.ndjsonl.

    Ledger writes are a CLI side effect, never a run gate. Any filesystem
    problem degrades to a warning and leaves the sweep's exit-code semantics
    unchanged.
    """
    if not records:
        return

    ledger_path = Path(runs_dir) / "ledger.ndjsonl"
    try:
        with ledger_path.open("a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                f.write("\n")
    except OSError as exc:
        print(f"wt run: warning: could not write ledger {ledger_path}: {exc}", file=sys.stderr)


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

    # Build runtime
    runtime = _build_runtime(runtime_name, label, soul_path=args.soul)

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
        agent_id="wt-cli", variant_id=label,
        system_prompt=system_prompt, persona_doc=persona_doc,
    )

    # Dim-tag → MCPServer factory registry, derived from the packs. Scenarios
    # are runtime-agnostic (import invariant: scenarios/ cannot statically
    # import windtunnel.mcp.*), so the binding from dim to a concrete mock
    # lives in each pack's mcp_factory (deferred import — see
    # windtunnel/scenarios/_mock_factory.py); the CLI just keys factories by
    # the f"dim:{name}" tag. Each factory is called once per scenario to
    # produce a fresh server instance (lifecycle: start-per-batch,
    # stop-per-batch inside run_scenario).
    mcp_registry = {
        f"dim:{pack.name}": pack.mcp_factory
        for pack in packs
        if pack.mcp_factory is not None
    }

    # NOTE: the probe registry is built LAZILY (inside the loop, below) rather
    # than here like mcp_registry — pre_run() hasn't fired yet at this point,
    # and the driver pattern is precisely that pre_run starts the bench fixture
    # and THEN sets state_probe_factory on its pack (see windtunnel.api.pack).
    # Snapshotting the factories pre-pre_run would always read None.

    # Platform-specific bench prep (runtime-pluggable seam): the resolved
    # plugin's OPTIONAL pre_run() hook runs once here — after _build_runtime
    # and scenario loading, before any scenario executes. Platform glue
    # (bench servers, container prep, workspace seeding) lives in the driver
    # package's plugin module. Plugins decide applicability themselves by
    # inspecting scenario tags, so the CLI never special-cases a platform.
    plugin = _resolve_runtime_plugin(runtime_name)
    pre_run = getattr(plugin, "pre_run", None)
    if pre_run is not None:
        pre_run(runtime, scenarios, runtime_name)

    # Dims whose MODEL verdict is counterfactual on the live path — the pack
    # declares it via ScenarioPack.transport_only (see windtunnel/api/pack.py
    # for the full semantics, and dim_memory_conflict/__init__.py for why that
    # built-in pack is currently the only one setting it). This set replaces
    # the old hardcoded _HISTORY_SHAPING_DIMS.
    transport_only_dims = {f"dim:{pack.name}" for pack in packs if pack.transport_only}
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
        if output_format is None or output_path is None:
            return rc
        try:
            _write_run_output(output_format, Path(output_path), completed, records)
        except OSError as exc:
            print(f"wt run: could not write {output_format} output to {output_path}: {exc}", file=sys.stderr)
            return 1
        return rc

    for selected_entry in selected:
        scenario = selected_entry.scenario
        # Wire the MCPServer for this scenario based on its dim tag — but ONLY
        # for plugin runtimes, which provision the mock into the real platform
        # MCP (e.g. acme via its runs API, acme_gateway via the gateway
        # path). The built-in in_memory runtime is scripted — it IGNORES mcps,
        # and starting an unused FastMCP subprocess can fail on a local dep/
        # port conflict before the run even reaches its real target. So don't
        # build the mock for it.
        scenario_mcps = None
        scenario_probe = None
        if runtime_name != "in_memory":
            for tag in getattr(scenario, "tags", []):
                if tag in mcp_registry:
                    factory = mcp_registry[tag]
                    # Pass the scenario so scenario-aware factories (silent_failure,
                    # which injects MOCK_MCP_FAILURE_MODE per scenario) can specialize.
                    scenario_mcps = [factory(scenario)]
                    break
            # External-state probe, same dim-tag dispatch as the mock. Factories
            # are read per scenario (not snapshotted into a registry above)
            # because pre_run() may have set them after pack discovery. The
            # factory itself may return None for scenarios it doesn't observe.
            for pack in packs:
                if pack.state_probe_factory is None:
                    continue
                if f"dim:{pack.name}" in getattr(scenario, "tags", []):
                    scenario_probe = pack.state_probe_factory(scenario)
                    break

        transport_only = any(
            t in transport_only_dims for t in getattr(scenario, "tags", [])
        )

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
                scenario, runtime, mcps=scenario_mcps, config=config,
                runs_per_scenario=n_runs, state_probe=scenario_probe,
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

        # Save traces + score sidecars (so `wt report/compare/triage` can
        # consume the run output directly, without a re-scoring pass).
        for run_result in result.runs:
            path = storage_path(run_result.trace, base_dir=runs_dir)
            save_trace(run_result.trace, path)
            _write_score_sidecar(path, run_result.score, scenario)

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
        completed.append(_CompletedAggregate(
            pack=selected_entry.pack,
            scenario=scenario,
            result=result,
            transport_only=transport_only,
            had_runner_error=had_runner_error,
        ))

        status = "PASS" if agg.verdict == "PASS" else "FAIL"
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


def _discover_scenario_packs(extra_sources: list[str] | None = None) -> list:
    """Return all ScenarioPacks: built-ins, entry points, then local sources.

    The scenario-side mirror of _resolve_runtime_plugin:
      1. Built-ins — windtunnel.scenarios.builtin_packs(), an explicit ordered
         list (order pins the sweep's scenario iteration order).
      2. Entry points in group "windtunnel.scenario_packs" — each entry-point
         value resolves to a ScenarioPack INSTANCE or a ZERO-ARG CALLABLE
         returning one (the callable form lets a pack defer scenario
         construction to load time, the same instance-or-class latitude the
         runtimes group gives plugins).
      3. Explicit local sources from --pack-source. Each source is either
         "module:attr" or "path/to/file.py:attr" and resolves under the same
         ScenarioPack-or-zero-arg-callable rule. This is the authoring escape
         hatch for examples before they are packaged as entry points.

    One deliberate asymmetry: runtimes resolve ONE plugin by --runtime NAME,
    but every installed pack is loaded — packs ADD scenarios to the selection
    pool and --scenario is the filter. So a broken pack can't be routed
    around by naming a different one; it fails the run loudly (exit 2, naming
    the offending entry point) exactly like an unloadable runtime plugin.
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    from windtunnel.scenarios import builtin_packs  # noqa: PLC0415

    packs: list = list(builtin_packs())
    for ep in entry_points(group="windtunnel.scenario_packs"):
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 — any load failure gets the same exit
            print(
                f"wt run: could not load scenario pack {ep.name!r} ({ep.value}): {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        packs.append(_coerce_scenario_pack(obj, f"entry point {ep.name!r}", ep.value))
    for source in extra_sources or []:
        packs.append(_load_scenario_pack_source(source))
    return packs


def _coerce_scenario_pack(obj: object, label: str, value: str) -> object:
    from windtunnel.api.pack import ScenarioPack  # noqa: PLC0415

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


def _load_scenario_pack_source(source: str) -> object:
    module_or_path, sep, attr = source.partition(":")
    if not sep or not module_or_path or not attr:
        print(
            "wt run: --pack-source must be module:attr or path/to/file.py:attr, "
            f"got {source!r}.",
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


class _InMemoryPlugin:
    """Built-in RuntimePlugin for the zero-infrastructure scripted runtime.

    One of the small runtimes that ship inside the framework itself —
    platform driver packages arrive via the "windtunnel.runtimes"
    entry-point group and stay OUT of cli.py.
    No pre_run hook: a scripted runtime has no bench infrastructure to prep.
    """

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> object:
        from windtunnel.runtimes.in_memory import InMemoryRuntime  # noqa: PLC0415
        return InMemoryRuntime(scripted_responses=["ok"])


class _HttpInjectPlugin:
    """Built-in RuntimePlugin for Contract C HTTP inject endpoints."""

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> object:
        from windtunnel.runtimes.http_inject import HttpInjectRuntime  # noqa: PLC0415
        return HttpInjectRuntime()


class _TerminusPlugin:
    """Built-in RuntimePlugin for Harbor Terminus-2 terminal agents."""

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> object:
        from windtunnel.runtimes.terminus import TerminusRuntime  # noqa: PLC0415
        return TerminusRuntime()


def _resolve_runtime_plugin(runtime_name: str) -> object:
    """Resolve a --runtime name to a RuntimePlugin instance.

    Resolution order (see windtunnel.spi.runtime_plugin for the contract):
      1. Built-ins — "in_memory", "http_inject", "terminus".
      2. Entry points in group "windtunnel.runtimes", matched by NAME.
         The entry-point value is a RuntimePlugin instance or class
         (a class is instantiated with no args).
      3. "module:attr" dotted path — same instance-or-class rule. This is
         the escape hatch for drivers not (yet) packaged with an entry point.
      4. Error (exit 2) listing the built-in + discovered names.
    """
    builtin = {
        "http_inject": _HttpInjectPlugin,
        "in_memory": _InMemoryPlugin,
        "terminus": _TerminusPlugin,
    }
    if runtime_name in builtin:
        return builtin[runtime_name]()

    from importlib.metadata import entry_points  # noqa: PLC0415
    eps = entry_points(group="windtunnel.runtimes")
    for ep in eps:
        if ep.name == runtime_name:
            return _as_plugin_instance(ep.load())

    if ":" in runtime_name:
        import importlib  # noqa: PLC0415
        module_name, _, attr = runtime_name.partition(":")
        try:
            obj = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            print(
                f"wt run: could not load runtime plugin {runtime_name!r}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        return _as_plugin_instance(obj)

    available = sorted({*builtin, *(ep.name for ep in eps)})
    print(
        f"wt run: unknown runtime {runtime_name!r}. Available: "
        f"{', '.join(available)}. (Or pass a 'module:attr' dotted path to a "
        f"RuntimePlugin.)",
        file=sys.stderr,
    )
    sys.exit(2)


def _as_plugin_instance(obj: object) -> object:
    """Normalize an entry-point/dotted-path target to a plugin INSTANCE.

    The contract (spi/runtime_plugin.py) is deliberately simple: the target is
    either a RuntimePlugin instance (used as-is) or a RuntimePlugin class
    (instantiated with no arguments). Anything fancier — factories with
    config args — belongs inside the plugin's own build().
    """
    if isinstance(obj, type):
        return obj()
    return obj


def _build_runtime(runtime_name: str, label: str, soul_path: str | None) -> object:
    """Instantiate the requested runtime via its resolved plugin."""
    plugin = _resolve_runtime_plugin(runtime_name)
    return plugin.build(runtime_name, label, soul_path)


def _select_scenarios(
    *,
    scenario_patterns: list[str],
    tag_filters: list[str],
    pack_filters: list[str],
    owner_filters: list[str],
    packs: list,
) -> _SelectionResult:
    """Select scenarios from packs with OR-within-flag, AND-across-flags.

    Selection predicates:
      - scenario: Scenario.name matched with fnmatch semantics, so exact
        names remain exact and shell-style globs such as ``lookup_*`` work.
      - tag: exact membership in Scenario.tags (e.g. ``dim:recovery``).
      - pack: exact ScenarioPack.name.
      - owner: exact defensive ``getattr(pack, "owner", None)``.

    Repeated values within one flag are alternatives. Distinct flag families
    compose as intersections, so ``--pack recovery --tag dim:recovery`` means
    scenarios in that pack that also carry that tag.
    """
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
            fnmatch.fnmatchcase(scenario_name(entry), pattern)
            for pattern in scenario_patterns
        )

    def tag_selected(entry: _SelectedScenario) -> bool:
        tags = scenario_tags(entry)
        return not tag_filters or any(tag in tags for tag in tag_filters)

    def pack_selected(entry: _SelectedScenario) -> bool:
        return not pack_filters or any(pack_name(entry) == name for name in pack_filters)

    def owner_selected(entry: _SelectedScenario) -> bool:
        return not owner_filters or any(pack_owner(entry) == owner for owner in owner_filters)

    entries = [
        entry for entry in all_entries
        if (
            scenario_selected(entry)
            and tag_selected(entry)
            and pack_selected(entry)
            and owner_selected(entry)
        )
    ]

    unmatched_scenarios = [
        pattern for pattern in scenario_patterns
        if not any(fnmatch.fnmatchcase(scenario_name(entry), pattern) for entry in all_entries)
    ]
    unmatched_tags = [
        tag for tag in tag_filters
        if not any(tag in scenario_tags(entry) for entry in all_entries)
    ]
    unmatched_packs = [
        name for name in pack_filters
        if not any(str(getattr(pack, "name", "")) == name for pack in packs)
    ]
    unmatched_owners = [
        owner for owner in owner_filters
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


def _load_scenarios(names: list[str], packs: list) -> list:
    """Flatten the packs' scenarios (pack order preserved) and filter by name.

    Every dimension arrives as a ScenarioPack — built-ins from
    windtunnel.scenarios.builtin_packs() (which keeps the pre-pack flattening
    order), externals from the "windtunnel.scenario_packs" entry-point group.
    Pack-specific shaping (e.g. multi_turn_drift exporting its wrappers'
    inner Scenarios, whose user_turns field drives the multi-turn runner
    path) happens where the pack is built, not here.
    """
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


def _counts_as_gate_failure(completed: _CompletedAggregate) -> bool:
    """Return the same per-scenario gate decision the run loop uses.

    The outcome aggregate is the gate, except transport-only packs do not flip
    CI on model-quality verdicts. A runner_error is an execution failure and
    still gates even for transport-only dims.
    """
    agg = completed.result.aggregate
    return agg.verdict != "PASS" and (not completed.transport_only or completed.had_runner_error)


def _write_run_output(
    output_format: str,
    out_path: Path,
    completed: list[_CompletedAggregate],
    records: list[dict],
) -> None:
    """Write the requested machine-readable `wt run` output file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        _write_run_json(out_path, records)
        return
    if output_format == "junit":
        _write_run_junit(out_path, completed)
        return
    raise ValueError(f"unknown run output format: {output_format!r}")


def _write_run_json(out_path: Path, records: list[dict]) -> None:
    """Write the sweep document: the very records the ledger just received.

    Sharing _ledger_record() output (rather than assembling a parallel
    record) is what keeps `--format json` and ledger.ndjsonl
    shape-identical by construction.
    """
    out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_run_junit(out_path: Path, completed: list[_CompletedAggregate]) -> None:
    """Write JUnit XML: one testsuite per pack, one testcase per aggregate."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    root = ET.Element("testsuites")
    total_tests = len(completed)
    total_failures = sum(1 for result in completed if _counts_as_gate_failure(result))
    total_time = sum(_aggregate_time_seconds(result) for result in completed)
    root.set("tests", str(total_tests))
    root.set("failures", str(total_failures))
    root.set("errors", "0")
    root.set("time", _format_seconds(total_time))

    by_pack: dict[str, list[_CompletedAggregate]] = {}
    for result in completed:
        by_pack.setdefault(str(getattr(result.pack, "name", "")), []).append(result)

    for pack_name, pack_results in by_pack.items():
        suite_failures = sum(1 for result in pack_results if _counts_as_gate_failure(result))
        suite_time = sum(_aggregate_time_seconds(result) for result in pack_results)
        suite = ET.SubElement(root, "testsuite", {
            "name": pack_name,
            "tests": str(len(pack_results)),
            "failures": str(suite_failures),
            "errors": "0",
            "time": _format_seconds(suite_time),
        })
        for result in pack_results:
            testcase = ET.SubElement(suite, "testcase", {
                "classname": pack_name,
                "name": str(getattr(result.scenario, "name", "")),
                "time": _format_seconds(_aggregate_time_seconds(result)),
            })
            if _counts_as_gate_failure(result):
                categories = _triage_categories(result)
                failure_attrs = {
                    "message": _junit_failure_message(result, categories),
                    "type": f"windtunnel.{result.result.aggregate.verdict}",
                }
                if categories:
                    failure_attrs["triage_category"] = ", ".join(categories)
                failure = ET.SubElement(testcase, "failure", failure_attrs)
                failure.text = _junit_failure_text(result, categories)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)


def _junit_failure_message(completed: _CompletedAggregate, categories: list[str]) -> str:
    """Return a compact failure summary for the JUnit failure attribute."""
    agg = completed.result.aggregate
    category = f" triage={', '.join(categories)}" if categories else ""
    return (
        f"{agg.verdict}: {agg.passed}/{agg.total} outcome pass"
        f"{category}"
    )


def _junit_failure_text(completed: _CompletedAggregate, categories: list[str]) -> str:
    """Return the escaped-by-ElementTree multi-line JUnit failure payload."""
    agg = completed.result.aggregate
    lines = [
        f"scenario_id: {getattr(completed.scenario, 'name', '')}",
        f"pack: {getattr(completed.pack, 'name', '')}",
        f"verdict: {agg.verdict}",
        f"outcome_pass_rate: {agg.outcome_pass_rate}",
        f"trajectory_pass_rate: {agg.trajectory_pass_rate}",
        f"constraint_pass_rate: {agg.constraint_pass_rate}",
        f"robustness_pass_rate: {agg.robustness_pass_rate}",
    ]
    if categories:
        lines.append(f"triage_category: {', '.join(categories)}")

    for idx, run_result in enumerate(completed.result.runs, start=1):
        run_id = getattr(run_result.trace, "run_id", "")
        lines.append(f"run {idx}: {run_id}")
        for layer_name in ("outcome", "trajectory", "constraint", "robustness"):
            layer = getattr(run_result.score, layer_name)
            status = "PASS" if layer.passed else "FAIL"
            lines.append(f"  {layer_name}: {status} - {layer.detail}")
    return "\n".join(lines)


def _triage_categories(completed: _CompletedAggregate) -> list[str]:
    """Return rule-based triage categories for failed runs when available."""
    attached = getattr(completed.result, "triage_category", None)
    if attached is None:
        attached = getattr(completed.result.aggregate, "triage_category", None)
    if attached:
        if isinstance(attached, str):
            return [attached]
        return [str(category) for category in attached]

    try:
        from windtunnel.triage.rule_based import RuleBasedClassifier  # noqa: PLC0415
    except Exception:
        return []

    classifier = RuleBasedClassifier()
    categories: list[str] = []
    for run_result in completed.result.runs:
        if run_result.score.outcome.passed:
            continue
        try:
            classification = classifier.classify(
                completed.scenario,
                run_result.trace,
                run_result.score,
            )
        except Exception:
            continue
        category = getattr(classification, "category", None)
        if category and category not in categories:
            categories.append(str(category))
    return categories


def _aggregate_time_seconds(completed: _CompletedAggregate) -> float:
    """Return total elapsed run time for one scenario aggregate, in seconds."""
    total = 0.0
    for run_result in completed.result.runs:
        trace = run_result.trace
        started_at = getattr(trace, "started_at", None)
        finished_at = getattr(trace, "finished_at", None)
        if started_at is None or finished_at is None:
            continue
        total += max(0.0, (finished_at - started_at).total_seconds())
    return total


def _format_seconds(value: float) -> str:
    """Format JUnit time attributes as seconds."""
    return f"{value:.6f}"


# ─── rescore ─────────────────────────────────────────────────────────────────

_SCORE_LAYERS = ("outcome", "trajectory", "constraint", "robustness")


def _cmd_rescore(args: argparse.Namespace) -> int:
    """Handle the `wt rescore` subcommand.

    Recomputes score layers from saved traces and current Scenario definitions.
    It never provisions a runtime and never modifies trace files.  Exit codes
    mirror `wt run`: 0 when all newly-scored outcomes pass, 1 when any newly
    scored outcome fails, and 2 for usage/configuration errors such as missing
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
            fnmatch.fnmatchcase(trace.scenario_id, pattern)
            for pattern in args.scenario
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
        if not new_score.outcome.passed:
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

        print(
            f"{trace_path}: scenario={trace.scenario_id} "
            + " | ".join(layer_parts)
            + write_note
        )

    total = len(trace_paths)
    print(
        "summary: "
        f"traces={total} changed={changed} new_outcome_failures={new_fail} "
        f"unresolved={unresolved} errors={errors} written={written} skipped={skipped}"
    )

    if unresolved or errors:
        return 2
    return 1 if new_fail else 0


def _rescore_trace_paths(args: argparse.Namespace) -> list[Path] | None:
    """Resolve --runs/--trace into trace JSON paths, excluding sidecars."""
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
    trace_paths = sorted(
        path for path in runs_dir.rglob("*.json")
        if not path.name.endswith(".score.json")
    )
    if not trace_paths:
        print(f"wt rescore: no trace files found under {runs_dir}", file=sys.stderr)
        return None
    return trace_paths


def _rescore_scenario_map(entries: list[_SelectedScenario]) -> dict[str, object] | None:
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


def _score_saved_trace(trace, scenario) -> object:
    """Re-run all score layers derivable from a saved trace."""
    from windtunnel.api.evaluators import (  # noqa: PLC0415
        evaluate_constraint,
        evaluate_outcome,
        evaluate_robustness,
        evaluate_trajectory,
    )
    from windtunnel.api.score import Score  # noqa: PLC0415

    return Score(
        outcome=evaluate_outcome(trace, scenario),
        trajectory=evaluate_trajectory(trace, scenario),
        constraint=evaluate_constraint(trace, scenario),
        robustness=evaluate_robustness(trace, scenario),
        failure_cost=scenario.failure_cost,
    )


def _read_score_sidecar(trace_path: Path) -> dict | None:
    score_path = trace_path.with_suffix(".score.json")
    if not score_path.is_file():
        return None
    try:
        return json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _old_layer_verdict(score_data: dict | None, layer_name: str) -> str:
    if score_data is None:
        return "UNKNOWN"
    layer = score_data.get(layer_name)
    if not isinstance(layer, dict):
        nested = score_data.get("score")
        if isinstance(nested, dict):
            layer = nested.get(layer_name)
    if not isinstance(layer, dict) or "passed" not in layer:
        return "UNKNOWN"
    return "PASS" if bool(layer["passed"]) else "FAIL"


def _score_layer_verdict(score, layer_name: str) -> str:
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
              "robustness": {"passed": ..., "detail": ...}
          }
        }
    Traces without a sibling score.json are skipped.

    Exit code: 0 (always — triage is informational, not a gate).
    """
    import json

    from windtunnel.api.scenario import Scenario  # noqa: PLC0415
    from windtunnel.api.score import LayerResult, Score  # noqa: PLC0415
    from windtunnel.api.trace import load_trace  # noqa: PLC0415

    runs_dir = Path(args.runs)
    classifier_name: str = args.classifier

    if not runs_dir.exists():
        print(f"wt triage: runs directory not found: {runs_dir}", file=sys.stderr)
        return 0

    # Build classifier
    if classifier_name == "rule_based":
        from windtunnel.triage.rule_based import RuleBasedClassifier  # noqa: PLC0415
        clf = RuleBasedClassifier()
    elif classifier_name == "llm_judge":
        from windtunnel.triage.llm_judge import LLMJudgeClassifier  # noqa: PLC0415
        clf = LLMJudgeClassifier()
    else:
        print(f"wt triage: unknown classifier {classifier_name!r}", file=sys.stderr)
        return 2

    # Walk runs/ for trace JSON files
    trace_files = sorted(runs_dir.rglob("*.json"))
    # Exclude sibling score files (end in .score.json)
    trace_files = [f for f in trace_files if not f.name.endswith(".score.json")]

    if not trace_files:
        print("# Wind Tunnel Triage Report\n\nNo runs found.")
        return 0

    # Classify each failed run
    # Groups: category → list of (scenario_id, trace, classification)
    by_category: dict[str, list[tuple[str, str, object]]] = {}
    skipped = 0
    passed = 0

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
            sd = score_data["score"]
            score = Score(
                outcome=LayerResult(**sd["outcome"]),
                trajectory=LayerResult(**sd["trajectory"]),
                constraint=LayerResult(**sd["constraint"]),
                robustness=LayerResult(**sd["robustness"]),
            )
        except (KeyError, TypeError):
            skipped += 1
            continue

        # Skip passing runs
        if score.outcome.passed:
            passed += 1
            continue

        # Build Scenario from stored data
        try:
            sc_data = score_data["scenario"]
            scenario = Scenario(
                name=sc_data["name"],
                prompt=sc_data.get("prompt", ""),
                target_facts=sc_data.get("target_facts", []),
                requires_tool_use=sc_data.get("requires_tool_use", False),
                tags=sc_data.get("tags", []),
                must_call=sc_data.get("must_call", []),
                forbidden_calls=sc_data.get("forbidden_calls", []),
            )
        except (KeyError, TypeError):
            skipped += 1
            continue

        classification = clf.classify(scenario, trace, score)
        cat = classification.category
        by_category.setdefault(cat, []).append(
            (scenario.name, trace.run_id[:8], classification)
        )

    # Emit markdown report
    total_failed = sum(len(v) for v in by_category.values())
    print("# Wind Tunnel Triage Report\n")
    print(f"**Failed runs:** {total_failed}  "
          f"**Passed:** {passed}  "
          f"**Skipped (no score):** {skipped}  "
          f"**Classifier:** `{classifier_name}`\n")

    if not by_category:
        print("No failures to triage.")
        return 0

    # Sort categories: unknown last, others by count descending
    def _sort_key(item: tuple[str, list]) -> tuple[int, int]:
        cat, entries = item
        return (1 if cat == "unknown" else 0, -len(entries))

    for category, entries in sorted(by_category.items(), key=_sort_key):
        print(f"## `{category}` ({len(entries)} failure{'s' if len(entries) != 1 else ''})\n")

        # Emit fix suggestion from first entry with one
        fix_shown = False
        for _name, _run_id, clf_result in entries:
            if not fix_shown and clf_result.suggested_fix is not None:  # type: ignore[union-attr]
                fix = clf_result.suggested_fix  # type: ignore[union-attr]
                print(f"**Suggested fix vector:** `{fix.fix_vector}`")
                print(f"**Rationale:** {fix.rationale}\n")
                fix_shown = True
                break
        # Table of failures
        print("| Scenario | Run ID | Confidence | Evidence |")
        print("|----------|--------|-----------|---------|")
        for name, run_id, clf_result in entries:
            conf = f"{clf_result.confidence:.0%}"  # type: ignore[union-attr]
            ev = "; ".join(clf_result.evidence[:2]) if clf_result.evidence else ""  # type: ignore[union-attr]
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
        agent_id="wt-doctor", variant_id=label, system_prompt=system_prompt,
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
    report_p.add_argument("--runs", default="runs", metavar="DIR",
                          help="Path to the runs/ directory (default: ./runs)")
    report_p.add_argument("--out", default=None, metavar="FILE",
                          help="Output path for file formats (HTML default: report.html).")
    report_p.add_argument("--format", default="html", choices=["html", "markdown", "json"],
                          help="Output format: html (default), markdown, or json.")

    # ── compare ──────────────────────────────────────────────────────────────
    compare_p = sub.add_parser("compare", help="Compare results across variant labels.")
    compare_p.add_argument("--labels", nargs="+", metavar="LABEL", default=[],
                           help="Variant labels to compare (space-separated).")
    compare_p.add_argument("--runs", default="runs", metavar="DIR", dest="runs",
                           help="Path to the runs/ directory (default: ./runs)")

    # ── run ──────────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run scenarios against a runtime.")
    run_p.add_argument("--scenario", action="append", metavar="S", default=None,
                       help="Scenario name(s) to run. Repeat for multiple. "
                            "Omit to run all registered scenarios (the built-in "
                            "dims plus any pack installed under the "
                            "'windtunnel.scenario_packs' entry-point group). "
                            "Shell-style globs such as 'lookup_*' are supported.")
    run_p.add_argument("--tag", action="append", metavar="TAG", default=None,
                       help="Run scenarios carrying TAG. Repeat for OR matching "
                            "within tags; composes with other selectors by AND.")
    run_p.add_argument("--pack", action="append", metavar="PACK", default=None,
                       help="Run scenarios from pack PACK. Repeat for OR matching "
                            "within packs; composes with other selectors by AND.")
    run_p.add_argument("--pack-source", action="append", metavar="SOURCE", default=None,
                       help="Load an additional local scenario pack from module:attr "
                            "or path/to/file.py:attr. Repeat for multiple sources; "
                            "use --pack to select it by name.")
    run_p.add_argument("--owner", action="append", metavar="OWNER", default=None,
                       help="Run scenarios from packs whose owner matches OWNER. "
                            "Repeat for OR matching within owners; composes with "
                            "other selectors by AND.")
    run_p.add_argument("--soul", default=None, metavar="PATH",
                       help="Path to SOUL.md / persona doc to inject.")
    run_p.add_argument("--agents", default=None, metavar="PATH",
                       help="Path to an AGENTS.md operating-notes doc to inject "
                            "(routed to set-docs --agents; does not touch agent code).")
    run_p.add_argument("--runtime", default="in_memory", metavar="RUNTIME",
                       help="Runtime to use (default: in_memory). Either the "
                            "built-in 'in_memory' (zero-infrastructure scripted "
                            "runtime — no network; useful for learning the "
                            "scoring model and testing scenario definitions in "
                            "CI), the name of an installed runtime plugin "
                            "(discovered via the 'windtunnel.runtimes' entry-"
                            "point group — e.g. 'acme' from a platform driver "
                            "package), or a 'module:attr' "
                            "dotted path to a RuntimePlugin instance or class.")
    run_p.add_argument("--label", default=None, metavar="LABEL",
                       help="Variant label for this run (recorded in traces).")
    run_p.add_argument("--runs", dest="n_runs", type=int, default=1, metavar="N",
                       help="Number of runs per scenario (default: 1).")
    run_p.add_argument("--runs-dir", default="runs", metavar="DIR",
                       help="Directory to write trace files (default: ./runs).")
    run_p.add_argument("--format", choices=["junit", "json"], default=None,
                       help="Machine-readable run output format. Must be paired with --out.")
    run_p.add_argument("--out", default=None, metavar="FILE",
                       help="Path for --format junit/json output. Must be paired with --format.")

    # ── rescore ──────────────────────────────────────────────────────────────
    rescore_p = sub.add_parser(
        "rescore",
        help="Re-score saved traces against current scenario definitions.",
    )
    rescore_input = rescore_p.add_mutually_exclusive_group(required=True)
    rescore_input.add_argument(
        "--runs", default=None, metavar="DIR",
        help="Walk a runs/ directory and re-score every saved trace.",
    )
    rescore_input.add_argument(
        "--trace", nargs="+", metavar="PATH", default=None,
        help="Explicit trace JSON path(s) to re-score.",
    )
    rescore_p.add_argument(
        "--write", action="store_true",
        help="Update .score.json sidecars. Trace files are never modified.",
    )
    rescore_p.add_argument(
        "--scenario", action="append", metavar="S", default=None,
        help="Only re-score traces whose scenario_id matches S. Repeat for multiple; "
             "shell-style globs such as 'lookup_*' are supported.",
    )
    rescore_p.add_argument("--tag", action="append", metavar="TAG", default=None,
                           help="Restrict scenario definitions to packs/scenarios carrying TAG.")
    rescore_p.add_argument("--pack", action="append", metavar="PACK", default=None,
                           help="Restrict scenario definitions to pack PACK.")
    rescore_p.add_argument("--pack-source", action="append", metavar="SOURCE", default=None,
                           help="Load an additional local scenario pack from module:attr "
                                "or path/to/file.py:attr before resolving traces.")
    rescore_p.add_argument("--owner", action="append", metavar="OWNER", default=None,
                           help="Restrict scenario definitions to packs whose owner matches OWNER.")

    # ── replay ───────────────────────────────────────────────────────────────
    replay_p = sub.add_parser("replay", help="Replay a captured trace against a runtime.")
    replay_p.add_argument("--trace", required=True, metavar="PATH",
                          help="Path to the trace JSON file to replay.")
    replay_p.add_argument("--runtime", default="in_memory", metavar="RUNTIME",
                          help="Runtime to replay against: built-in 'in_memory', "
                               "an installed plugin name (entry-point group "
                               "'windtunnel.runtimes'), or a 'module:attr' "
                               "dotted path to a RuntimePlugin.")
    replay_p.add_argument("--runs-dir", default="runs", metavar="DIR",
                          help="Directory to write replayed traces (default: ./runs).")

    # ── doctor ───────────────────────────────────────────────────────────────
    doctor_p = sub.add_parser(
        "doctor",
        help="Bring-up check: run the reset-isolation canary against a live runtime.",
    )
    doctor_p.add_argument("--runtime", default="in_memory", metavar="RUNTIME",
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
                               "state_probe=...) directly from pytest instead.")
    doctor_p.add_argument("--soul", default=None, metavar="PATH",
                          help="Path to SOUL.md / persona doc to inject "
                               "(mirrors `wt run --soul`).")
    doctor_p.add_argument("--label", default=None, metavar="LABEL",
                          help="Variant label recorded for this check "
                               "(default: wt_doctor).")

    # ── import ───────────────────────────────────────────────────────────────
    import_p = sub.add_parser(
        "import",
        help="Generate a scenario skeleton from a Contract A *.wtin.json trace.",
    )
    import_p.add_argument("--trace", required=True, metavar="PATH",
                          help="Path to the Contract A *.wtin.json trace envelope.")
    import_p.add_argument("--out", required=True, metavar="DIR",
                          help="Directory to write scenario.py, scorer.py, "
                               "fixture.universe.json, and IMPORTED.md.")
    import_p.add_argument("--force", action="store_true",
                          help="Allow writing into an existing non-empty directory.")

    # ── validate ─────────────────────────────────────────────────────────────
    validate_p = sub.add_parser(
        "validate",
        help="Validate Contract A *.wtin.json interchange envelope(s).",
    )
    validate_p.add_argument("paths", nargs="+", metavar="PATH",
                            help="Path(s) to *.wtin.json envelope file(s) to validate.")
    validate_p.add_argument("--strict", action="store_true",
                            help="Exit 1 if any file produces a lint warning (e.g. "
                                 "truncated/redacted values, unpaired tool_call_response "
                                 "ids), not only on schema errors.")

    # ── triage ───────────────────────────────────────────────────────────────
    triage_p = sub.add_parser(
        "triage",
        help="Classify failed runs and emit a markdown report grouped by failure category.",
    )
    triage_p.add_argument(
        "--runs", default="runs", metavar="DIR",
        help="Path to the runs/ directory (default: ./runs). "
             "Each trace must have a sibling .score.json file.",
    )
    triage_p.add_argument(
        "--classifier", default="rule_based",
        choices=["rule_based", "llm_judge"],
        help="Classifier to use: rule_based (default, deterministic) or "
             "llm_judge (stub — raises NotImplementedError until implemented).",
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
