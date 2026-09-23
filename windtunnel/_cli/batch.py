"""`wt batch`: run a file of `wt run` specs back to back with one command.

Each non-blank line of the file is one run spec written exactly as the
options of `wt run` (shell quoting, ``#`` comments, and an optional leading
``wt run`` are accepted), so a queue of rounds needs no second syntax::

    # round 1: baseline, then the candidate prompt
    --pack my_pack --runs 5 --label baseline
    --pack my_pack --runs 5 --label candidate --agents notes/candidate.md

Every spec is validated before the first one runs, so a typo on line 9 never
costs rounds 1-8. Specs then run in file order, each as its own sweep through
the scheduler (its own sweep id, events, ledger rows, and runtime lock), and a
failing spec never stops the next. The batch exits with the highest exit code
any spec returned.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shlex
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Spec:
    line: int
    text: str
    args: argparse.Namespace


def _cmd_batch(
    args: argparse.Namespace,
    *,
    run: Callable[[argparse.Namespace], int],
    run_parser: argparse.ArgumentParser,
) -> int:
    """Validate every spec in args.file, then run them in order via ``run``."""
    source = str(args.file)
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"wt batch: cannot read {source}: {exc}", file=sys.stderr)
        return 2

    defaults: list[str] = []
    if args.scheduler:
        defaults += ["--scheduler", args.scheduler]
    if args.max_concurrency is not None:
        defaults += ["--max-concurrency", str(args.max_concurrency)]
    if args.runs_dir:
        defaults += ["--runs-dir", args.runs_dir]
    if args.no_wait:
        defaults.append("--no-wait")

    specs: list[_Spec] = []
    invalid = False
    for number, raw in enumerate(text.splitlines(), start=1):
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError as exc:
            print(f"wt batch: {source}:{number}: {exc}", file=sys.stderr)
            invalid = True
            continue
        if tokens[:2] == ["wt", "run"]:
            tokens = tokens[2:]
        elif tokens[:1] == ["run"]:
            tokens = tokens[1:]
        if not tokens:
            continue
        parsed = _parse_spec(run_parser, [*defaults, *tokens])
        if isinstance(parsed, str):
            print(f"wt batch: {source}:{number}: {parsed}", file=sys.stderr)
            invalid = True
            continue
        specs.append(_Spec(line=number, text=shlex.join(tokens), args=parsed))

    if invalid:
        print("wt batch: no spec was run; fix the lines above.", file=sys.stderr)
        return 2
    if not specs:
        print(f"wt batch: {source} contains no run specs.", file=sys.stderr)
        return 2

    results: list[tuple[_Spec, int]] = []
    for position, spec in enumerate(specs, start=1):
        print(
            f"wt batch: [{position}/{len(specs)}] {source}:{spec.line}: {spec.text}",
            file=sys.stderr,
            flush=True,
        )
        try:
            code = run(spec.args)
        except SystemExit as exit_request:
            code = exit_request.code if isinstance(exit_request.code, int) else 1
        except Exception:  # noqa: BLE001 - one broken spec must not strand the queue
            traceback.print_exc()
            code = 1
        results.append((spec, code))

    print(f"wt batch: {len(results)} spec(s) finished:", file=sys.stderr)
    for spec, code in results:
        label = getattr(spec.args, "label", None) or "cli_run"
        print(f"  line {spec.line:<4} label {label:<24} exit {code}", file=sys.stderr)
    return max(code for _spec, code in results)


def _parse_spec(
    parser: argparse.ArgumentParser, tokens: list[str]
) -> argparse.Namespace | str:
    """Parse one spec with the `wt run` parser; return its error text on failure."""
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            return parser.parse_args(tokens)
    except SystemExit:
        lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        message = lines[-1] if lines else "invalid run spec"
        _prefix, sep, detail = message.partition("error: ")
        return detail if sep else message
