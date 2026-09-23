"""`wt watch`: follow one sweep's progress events until it ends.

The command only reads ``<runs-dir>/events.ndjsonl`` (see events.py), so it
works for any runtime and from any other process. It prints one line per
event and exits with the followed sweep's own exit code, which makes
``wt run ... &`` followed by ``wt watch`` a blocking wait that still reports
the verdict.

Which sweep: ``--sweep ID`` follows exactly that sweep, replaying it if it
already finished. Otherwise the newest sweep (matching ``--label``) that is
still running or started within the last few seconds is followed, and when
there is none the command waits for the next one to start. The recent-start
window covers a sweep that finished before `wt watch` got going without
ever re-reporting an older sweep that finished long ago.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from windtunnel._cli.events import EVENT_FORMAT_VERSION, EVENTS_FILENAME

RECENT_START_WINDOW_S = 10.0
EXIT_TIMEOUT = 124


class _EventTail:
    """Incremental reader of an append-only NDJSON event file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
            if size < self._offset:  # truncated or replaced: start over
                self._offset, self._partial = 0, b""
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except OSError:
            return []
        if not data:
            return []
        self._offset += len(data)
        *lines, self._partial = (self._partial + data).split(b"\n")
        events: list[dict[str, Any]] = []
        for raw in lines:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if (
                isinstance(event, dict)
                and event.get("windtunnel_event") == EVENT_FORMAT_VERSION
                and isinstance(event.get("sweep_id"), str)
                and isinstance(event.get("event"), str)
            ):
                events.append(event)
        return events


def _cmd_watch(args: argparse.Namespace) -> int:
    """Handle the `wt watch` subcommand."""
    runs_dir = Path(args.runs)
    tail = _EventTail(runs_dir / EVENTS_FILENAME)
    poll_s = max(0.01, float(args.poll))
    deadline = None if args.timeout is None else time.monotonic() + float(args.timeout)
    label: str | None = args.label
    target: str | None = args.sweep
    started: dict[str, dict[str, Any]] = {}

    def _matches(event: dict[str, Any]) -> bool:
        return label is None or event.get("label") == label

    history = tail.read_new()
    finished = {e["sweep_id"]: e for e in history if e["event"] == "sweep_finished"}
    for event in history:
        if event["event"] == "sweep_started":
            started[event["sweep_id"]] = event
    if target is None:
        candidates = [
            event
            for sweep_id, event in started.items()
            if _matches(event)
            and (sweep_id not in finished or _started_recently(event))
        ]
        if candidates:
            target = candidates[-1]["sweep_id"]

    if target is not None:
        for event in history:
            if event["sweep_id"] == target:
                _print_event(event, as_json=args.json)
        if target in finished:
            return _exit_code(finished[target])
    else:
        wanted = f" with label {label!r}" if label is not None else ""
        print(
            f"wt watch: waiting for a sweep{wanted} to start in {runs_dir}",
            file=sys.stderr,
            flush=True,
        )

    idle_since = time.monotonic()
    while True:
        new_events = tail.read_new()
        for event in new_events:
            if event["event"] == "sweep_started":
                started[event["sweep_id"]] = event
                if target is None and _matches(event):
                    target = event["sweep_id"]
            if event["sweep_id"] != target:
                continue
            _print_event(event, as_json=args.json)
            if event["event"] == "sweep_finished":
                return _exit_code(event)
        now = time.monotonic()
        if new_events:
            idle_since = now
        elif target is not None and now - idle_since >= 4 * poll_s:
            if _writer_alive(started.get(target)) is False:
                print(
                    f"wt watch: sweep {target}'s process exited without finishing "
                    "(killed or crashed); its completed runs are on disk",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            idle_since = now
        if deadline is not None and now >= deadline:
            print(
                f"wt watch: timed out after {args.timeout}s "
                f"({'sweep ' + target + ' still running' if target else 'no sweep started'})",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TIMEOUT
        time.sleep(poll_s)


def _started_recently(event: dict[str, Any]) -> bool:
    started_at = _parse_ts(event.get("ts"))
    if started_at is None:
        return False
    return datetime.now(UTC) - started_at <= timedelta(seconds=RECENT_START_WINDOW_S)


def _exit_code(finished: dict[str, Any]) -> int:
    code = finished.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 1


def _writer_alive(started: dict[str, Any] | None) -> bool | None:
    """True/False when the writer's liveness is knowable, else None.

    Only a sweep written from this host on a POSIX system is checked; on
    Windows a signal-0 probe is not side-effect free, so it is never sent.
    """
    if started is None or os.name != "posix":
        return None
    pid = started.get("pid")
    if started.get("host") != socket.gethostname() or not isinstance(pid, int):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _print_event(event: dict[str, Any], *, as_json: bool) -> None:
    line = json.dumps(event, ensure_ascii=False) if as_json else format_event(event)
    print(line, flush=True)


def format_event(event: dict[str, Any]) -> str:
    """Render one progress event as a single human-readable line."""
    parsed = _parse_ts(event.get("ts"))
    clock = parsed.astimezone().strftime("%H:%M:%S") if parsed else str(event.get("ts", ""))
    kind = event["event"]
    sweep = event["sweep_id"]
    scenario = event.get("scenario_id", "")
    if kind == "sweep_started":
        count = len(event.get("scenarios") or [])
        return (
            f"{clock} sweep {sweep} started: label {event.get('label')}, runtime "
            f"{event.get('runtime')}, {count} scenario(s) x {event.get('runs_per_scenario')} run(s)"
        )
    if kind == "run_started":
        return f"{clock} {scenario} run {event.get('run')}/{event.get('runs')} started"
    if kind == "run_finished":
        line = f"{clock} {scenario} run {event.get('run')}/{event.get('runs')} {event.get('verdict')}"
        duration = event.get("duration_s")
        if isinstance(duration, int | float):
            line += f" ({duration:.1f}s)"
        failed = [name for name, ok in (event.get("layers") or {}).items() if not ok]
        if failed and event.get("verdict") != "PASS":
            line += f" failed: {', '.join(failed)}"
        metrics = event.get("metrics") or {}
        if metrics:
            line += " " + " ".join(f"{name}={value}" for name, value in metrics.items())
        return line
    if kind == "scenario_finished":
        return (
            f"{clock} {scenario} {event.get('verdict')} "
            f"{event.get('passed')}/{event.get('total')} pass"
        )
    if kind == "scenario_error":
        tag = "WORLD" if event.get("kind") == "world_mismatch" else "ERROR"
        return f"{clock} {scenario} {tag} {str(event.get('error', ''))[:200]}"
    if kind == "sweep_finished":
        return (
            f"{clock} sweep {sweep} finished: exit {event.get('exit_code')} "
            f"({event.get('status')}; {event.get('completed')}/{event.get('scenarios')} "
            f"scenario(s), {event.get('errors')} error(s))"
        )
    if kind == "lock_waiting":
        holder = event.get("holder") or {}
        held_by = f" held by pid {holder.get('pid')}" if holder.get("pid") else ""
        return f"{clock} sweep {sweep} waiting for runtime {event.get('lock')!r}{held_by}"
    if kind == "lock_acquired":
        return (
            f"{clock} sweep {sweep} acquired runtime {event.get('lock')!r} "
            f"after {float(event.get('waited_s') or 0.0):.1f}s"
        )
    extra = {
        key: value
        for key, value in event.items()
        if key not in {"windtunnel_event", "ts", "event", "sweep_id", "label"}
    }
    return f"{clock} sweep {sweep} {kind} {json.dumps(extra, ensure_ascii=False)}"
