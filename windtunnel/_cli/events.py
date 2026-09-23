"""Append-only sweep progress events: the stream `wt watch` follows.

Each `wt run` sweep appends one JSON object per line to
``<runs-dir>/events.ndjsonl`` as it goes: ``sweep_started``, then per run
``run_started`` / ``run_finished``, per scenario ``scenario_finished`` or
``scenario_error``, and finally ``sweep_finished``. Every event carries the
sweep's id and label so several sweeps may share one runs directory.

The stream is observational. Writing it is best-effort — an I/O error warns
once and never fails the sweep — and nothing in Wind Tunnel reads it back to
decide a verdict: traces, sidecars, and the ledger stay the record.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENTS_FILENAME = "events.ndjsonl"
EVENT_FORMAT_VERSION = 1


def _event_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SweepEvents:
    """Thread-safe appender for one sweep's progress events."""

    def __init__(self, runs_dir: Path, *, label: str) -> None:
        self.path = Path(runs_dir) / EVENTS_FILENAME
        self.sweep_id = uuid.uuid4().hex[:12]
        self._label = label
        self._lock = threading.Lock()
        self._warned = False

    def started(self, **fields: Any) -> None:
        """Emit ``sweep_started`` with the writer's identity for liveness checks."""
        self.emit(
            "sweep_started",
            pid=os.getpid(),
            host=socket.gethostname(),
            **fields,
        )

    def emit(self, event: str, **fields: Any) -> None:
        """Append one event line; never raises for I/O problems."""
        record = {
            "windtunnel_event": EVENT_FORMAT_VERSION,
            "ts": _event_timestamp(),
            "event": event,
            "sweep_id": self.sweep_id,
            "label": self._label,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(line)
            except OSError as exc:
                if not self._warned:
                    self._warned = True
                    print(
                        f"wt run: warning: could not write progress events {self.path}: {exc}",
                        file=sys.stderr,
                    )
