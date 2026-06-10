"""CLI entry point for Wind Tunnel — the `wt` command.

Subcommands:
    wt run      [--scenario S]... [--soul PATH] [--runtime RUNTIME]
                [--label LABEL] [--runs N]
    wt report   [--runs DIR] [--out FILE] [--format html|markdown]
    wt compare  --labels L1 L2 ...
    wt replay   --trace PATH --runtime RUNTIME

Design: argparse (stdlib) — no click dependency. Each subcommand is a
function; main() is the dispatch entry point. Exit code 0 = all pass,
non-zero = any regression or error.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# ─── report ──────────────────────────────────────────────────────────────────

def _cmd_report(args: argparse.Namespace) -> int:
    """Handle the `wt report` subcommand."""
    from windtunnel.report import generate_html, generate_markdown  # noqa: PLC0415

    runs_dir = Path(args.runs)
    fmt = args.format.lower()

    if fmt == "markdown":
        generate_markdown(runs_dir=runs_dir, out=sys.stdout)
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

def _write_score_sidecar(trace_path: Path, score, scenario) -> Path:
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
    score_path = trace_path.with_suffix(".score.json")
    score_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return score_path


def _cmd_run(args: argparse.Namespace) -> int:
    """Handle the `wt run` subcommand.

    Drives selected scenarios against the specified runtime, writes traces
    to the runs/ directory, and exits non-zero if any scenario fails.

    Supports --runtime in_memory (built-in) for smoke testing, plus any
    runtime plugin installed under the "windtunnel.runtimes" entry-point
    group (e.g. acme / acme_gateway from a platform driver package)
    or a 'module:attr' dotted path — see windtunnel.spi.runtime_plugin.

    Scenarios arrive as ScenarioPacks: the built-in dims plus any pack
    installed under the "windtunnel.scenario_packs" entry-point group —
    see windtunnel.api.pack and _discover_scenario_packs.
    """
    from windtunnel.api.runner import run_scenario  # noqa: PLC0415
    from windtunnel.api.trace import save_trace, storage_path  # noqa: PLC0415

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    n_runs: int = args.n_runs
    runtime_name: str = args.runtime
    label: str = args.label or "cli_run"
    scenario_names: list[str] = args.scenario or []

    # Build runtime
    runtime = _build_runtime(runtime_name, label, soul_path=args.soul)

    # Discover scenario packs (built-ins + the "windtunnel.scenario_packs"
    # entry-point group). The pack is the unit that carries a dim's scenarios,
    # its mock-MCP factory, and the transport-only flag — see windtunnel.api.pack.
    packs = _discover_scenario_packs()

    # Load scenarios
    scenarios = _load_scenarios(scenario_names, packs)
    if not scenarios:
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
    for scenario in scenarios:
        # Wire the MCPServer for this scenario based on its dim tag — but ONLY
        # for plugin runtimes, which provision the mock into the real platform
        # MCP (e.g. acme via its runs API, acme_gateway via the gateway
        # path). The built-in in_memory runtime is scripted — it IGNORES mcps,
        # and starting an unused FastMCP subprocess can fail on a local dep/
        # port conflict before the run even reaches its real target. So don't
        # build the mock for it.
        scenario_mcps = None
        if runtime_name != "in_memory":
            for tag in getattr(scenario, "tags", []):
                if tag in mcp_registry:
                    factory = mcp_registry[tag]
                    # Pass the scenario so scenario-aware factories (silent_failure,
                    # which injects MOCK_MCP_FAILURE_MODE per scenario) can specialize.
                    scenario_mcps = [factory(scenario)]
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
                runs_per_scenario=n_runs,
            )
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
                return 1
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
        if agg.verdict != "PASS" and (not transport_only or had_runner_error):
            any_fail = True

    return 1 if any_fail else 0


def _discover_scenario_packs() -> list:
    """Return all ScenarioPacks: built-ins first, then entry-point packs.

    The scenario-side mirror of _resolve_runtime_plugin:
      1. Built-ins — windtunnel.scenarios.builtin_packs(), an explicit ordered
         list (order pins the sweep's scenario iteration order).
      2. Entry points in group "windtunnel.scenario_packs" — each entry-point
         value resolves to a ScenarioPack INSTANCE or a ZERO-ARG CALLABLE
         returning one (the callable form lets a pack defer scenario
         construction to load time, the same instance-or-class latitude the
         runtimes group gives plugins).

    One deliberate asymmetry: runtimes resolve ONE plugin by --runtime NAME,
    but every installed pack is loaded — packs ADD scenarios to the selection
    pool and --scenario is the filter. So a broken pack can't be routed
    around by naming a different one; it fails the run loudly (exit 2, naming
    the offending entry point) exactly like an unloadable runtime plugin.
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    from windtunnel.api.pack import ScenarioPack  # noqa: PLC0415
    from windtunnel.scenarios import builtin_packs  # noqa: PLC0415

    packs: list = list(builtin_packs())
    for ep in entry_points(group="windtunnel.scenario_packs"):
        try:
            obj = ep.load()
            if not isinstance(obj, ScenarioPack) and callable(obj):
                obj = obj()
        except Exception as exc:  # noqa: BLE001 — any load failure gets the same exit
            print(
                f"wt run: could not load scenario pack {ep.name!r} ({ep.value}): {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        if not isinstance(obj, ScenarioPack):
            print(
                f"wt run: scenario pack entry point {ep.name!r} ({ep.value}) must "
                f"resolve to a ScenarioPack instance or a zero-arg callable "
                f"returning one, got {type(obj).__name__}.",
                file=sys.stderr,
            )
            sys.exit(2)
        packs.append(obj)
    return packs


class _InMemoryPlugin:
    """Built-in RuntimePlugin for the zero-infrastructure scripted runtime.

    The only runtime that ships inside the framework itself — everything else
    (platform driver packages) arrives via the
    "windtunnel.runtimes" entry-point group and stays OUT of cli.py.
    No pre_run hook: a scripted runtime has no bench infrastructure to prep.
    """

    def build(self, runtime_name: str, label: str, soul_path: str | None) -> object:
        from windtunnel.runtimes.in_memory import InMemoryRuntime  # noqa: PLC0415
        return InMemoryRuntime(scripted_responses=["ok"])


def _resolve_runtime_plugin(runtime_name: str) -> object:
    """Resolve a --runtime name to a RuntimePlugin instance.

    Resolution order (see windtunnel.spi.runtime_plugin for the contract):
      1. Built-ins — "in_memory".
      2. Entry points in group "windtunnel.runtimes", matched by NAME.
         The entry-point value is a RuntimePlugin instance or class
         (a class is instantiated with no args).
      3. "module:attr" dotted path — same instance-or-class rule. This is
         the escape hatch for drivers not (yet) packaged with an entry point.
      4. Error (exit 2) listing the built-in + discovered names.
    """
    builtin = {"in_memory": _InMemoryPlugin}
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


def _load_scenarios(names: list[str], packs: list) -> list:
    """Flatten the packs' scenarios (pack order preserved) and filter by name.

    Every dimension arrives as a ScenarioPack — built-ins from
    windtunnel.scenarios.builtin_packs() (which keeps the pre-pack flattening
    order), externals from the "windtunnel.scenario_packs" entry-point group.
    Pack-specific shaping (e.g. multi_turn_drift exporting its wrappers'
    inner Scenarios, whose user_turns field drives the multi-turn runner
    path) happens where the pack is built, not here.
    """
    all_scenarios: list = []
    for pack in packs:
        all_scenarios.extend(pack.scenarios)

    if not names:
        return all_scenarios

    name_set = set(names)
    matched = [s for s in all_scenarios if s.name in name_set]
    missing = name_set - {s.name for s in matched}
    if missing:
        print(f"wt run: unknown scenario(s): {', '.join(sorted(missing))}", file=sys.stderr)
    return matched


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


# ─── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0 = all pass, non-zero = regression/error)."""
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
                          help="Output path for HTML report (default: report.html).")
    report_p.add_argument("--format", default="html", choices=["html", "markdown"],
                          help="Output format: html (default) or markdown.")

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
                            "'windtunnel.scenario_packs' entry-point group).")
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

    args = parser.parse_args(argv)

    if args.command == "report":
        return _cmd_report(args)
    if args.command == "compare":
        return _cmd_compare(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "triage":
        return _cmd_triage(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
