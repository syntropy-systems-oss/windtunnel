"""stdlib HTTP server behind `wt serve` — JSON endpoints plus SSE live tail.

Design: http.server.ThreadingHTTPServer with a hand-rolled handler — no web
framework, matching the CLI's argparse-over-click stance. Only GET is
implemented, so the server is read-only by construction (anything else gets
the stock 501 from BaseHTTPRequestHandler).

Endpoints:
    GET /               the self-contained viewer page (page.py)
    GET /api/meta       viewer configuration (runs dir, live glob, version)
    GET /api/ledger     parsed ledger rows, newest first (dashboard polls this)
    GET /api/run/<id>   one saved run: raw trace JSON + .score.json sidecar
    GET /api/run/<id>/evidence
                        span-level evidence recomputed from (Scenario, Trace)
                        via the scoring matchers — see _serve/evidence.py;
                        degrades to {"available": false, reason} when the
                        run's scenario is not among the discovered packs
    GET /api/scenarios  discovered pack/scenario summaries (the test cases)
    GET /api/live       SSE stream tailing JSONL files matching --live-glob

Experiment mode (opt-in via `wt serve --experiment`, otherwise absent —
the read-only posture stays: without it, ANY non-GET method gets the stock
501, and these routes 404):
    GET  /api/experiment/status   current/last rerun job
    GET  /api/experiment/events   SSE stream of the rerun's output lines
    POST /api/experiment/rerun    {"run_id", "knobs": {...}} — spawn a
                                  scoped `wt run` of that run's scenario
                                  with knob overrides; one in flight at a
                                  time (409 on concurrency); strict knob
                                  validation against the runtime's declared
                                  KnobSpecs (400 on any mismatch)

Live tail: generic by design. It watches whatever files match the glob,
starts at end-of-file for files that already exist, streams each newly
appended complete line as one SSE event, and restarts from the top when a
file shrinks (rotation/truncation). It knows nothing about who writes the
files or how they are named — "tail whatever JSONL your runtime writes".
"""

from __future__ import annotations

import glob as _glob
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from windtunnel._serve import data as _data
from windtunnel._serve.experiment import ExperimentRunner
from windtunnel._serve.page import PAGE_HTML

_LIVE_POLL_SECONDS = 0.5
_LIVE_KEEPALIVE_SECONDS = 15.0


class RunViewerServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the viewer's read-only state."""

    daemon_threads = True  # SSE connections must not block shutdown

    def __init__(
        self,
        address: tuple[str, int],
        *,
        runs_dir: Path,
        pack_data: list[dict[str, Any]],
        scenario_index: dict[str, Any],
        scenario_packs: dict[str, str],
        live_glob: str | None,
        wt_version: str,
        experiment: ExperimentRunner | None,
    ) -> None:
        super().__init__(address, _RunViewerHandler)
        self.runs_dir = Path(runs_dir)
        self.pack_data = pack_data
        self.scenario_index = scenario_index
        self.scenario_packs = scenario_packs
        self.live_glob = live_glob
        self.wt_version = wt_version
        self.experiment = experiment

    @property
    def bound_address(self) -> tuple[str, int]:
        """The (host, port) actually bound — resolves port=0 requests."""
        host, port = self.server_address[:2]
        text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return text, int(port)


def build_server(
    *,
    runs_dir: Path,
    packs: list[Any],
    live_glob: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8686,
    wt_version: str = "unknown",
    experiment: ExperimentRunner | None = None,
) -> RunViewerServer:
    """Bind the run viewer. port=0 asks the OS for an ephemeral port.

    Pack summaries are computed once here — pack discovery is an import-time
    affair, exactly like `wt run` resolving its selection once per
    invocation. Runs/ artifacts, by contrast, are re-read on every request so
    the dashboard follows a sweep that is writing right now.

    experiment: None (the default) keeps the server read-only by
    construction — do_POST refuses everything with the stock 501.
    """
    pack_data = _data.pack_summaries(packs)
    scenario_index = _data.scenarios_by_id(packs)
    scenario_packs = _data.scenario_pack_names(packs)
    return RunViewerServer(
        (host, port),
        runs_dir=runs_dir,
        pack_data=pack_data,
        scenario_index=scenario_index,
        scenario_packs=scenario_packs,
        live_glob=live_glob,
        wt_version=wt_version,
        experiment=experiment,
    )


class _RunViewerHandler(BaseHTTPRequestHandler):
    """GET-only handler: HTML page, JSON endpoints, and the SSE live tail."""

    server: RunViewerServer  # narrowed for type-checkers
    protocol_version = "HTTP/1.1"

    # The dashboard polls /api/ledger; per-request access logging would bury
    # the terminal. The viewer stays quiet after its startup line.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        del format, args

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                self._send_html(PAGE_HTML)
            elif path == "/api/meta":
                experiment = self.server.experiment
                self._send_json(
                    {
                        "runs_dir": str(self.server.runs_dir),
                        "live_glob": self.server.live_glob,
                        "wt_version": self.server.wt_version,
                        "experiment": experiment is not None,
                        "runtime": experiment.runtime_name if experiment else None,
                        "knobs": experiment.knobs_payload() if experiment else None,
                    }
                )
            elif path == "/api/ledger":
                self._send_json(_data.load_ledger_rows(self.server.runs_dir))
            elif path.startswith("/api/run/") and path.endswith("/evidence"):
                run_id = path.removeprefix("/api/run/").removesuffix("/evidence")
                self._send_run_evidence(run_id)
            elif path.startswith("/api/run/"):
                run_id = path.removeprefix("/api/run/")
                resolved = _data.resolve_run(self.server.runs_dir, run_id)
                if resolved is None:
                    self._send_json({"error": f"no stored trace for run_id {run_id!r}"}, status=404)
                else:
                    self._send_json(resolved)
            elif path == "/api/scenarios":
                self._send_json({"packs": self.server.pack_data})
            elif path == "/api/live":
                self._stream_live()
            elif path == "/api/experiment/status":
                experiment = self.server.experiment
                if experiment is None:
                    self._send_json(
                        {"error": "experiment mode is off; start wt serve with --experiment"},
                        status=404,
                    )
                else:
                    self._send_json(experiment.status())
            elif path == "/api/experiment/events":
                self._stream_experiment()
            else:
                self._send_json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; nothing to clean up

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
        """POST exists ONLY in experiment mode, and only for one route.

        Without --experiment the posture is identical to not implementing
        POST at all: the stock 501 for every path, so the default server
        remains read-only by construction.
        """
        experiment = self.server.experiment
        if experiment is None:
            self.send_error(501, "Unsupported method ('POST')")
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/experiment/rerun":
                self._handle_rerun(experiment)
            else:
                self._send_json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_rerun(self, experiment: ExperimentRunner) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return

        run_id = body.get("run_id")
        overrides = body.get("knobs") or {}
        if not isinstance(run_id, str) or not run_id:
            self._send_json({"error": "run_id is required"}, status=400)
            return
        if not isinstance(overrides, dict):
            self._send_json({"error": "knobs must be an object"}, status=400)
            return

        trace_path = _data.resolve_run_path(self.server.runs_dir, run_id)
        if trace_path is None:
            self._send_json({"error": f"no stored trace for run_id {run_id!r}"}, status=404)
            return
        try:
            scenario_id = str(
                json.loads(trace_path.read_text(encoding="utf-8")).get("scenario_id")
            )
        except (OSError, json.JSONDecodeError):
            self._send_json({"error": "trace could not be read"}, status=500)
            return
        pack_name = self.server.scenario_packs.get(scenario_id)
        if pack_name is None:
            self._send_json(
                {
                    "error": (
                        f"scenario {scenario_id!r} is not among the discovered packs — "
                        "start wt serve with the --pack-source this run was executed with"
                    )
                },
                status=400,
            )
            return

        status, payload = experiment.start(
            parent_run_id=run_id,
            scenario_id=scenario_id,
            pack_name=pack_name,
            overrides=overrides,
        )
        self._send_json(payload, status=status)

    # ── responses ────────────────────────────────────────────────────────────

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_run_evidence(self, run_id: str) -> None:
        """Recompute span-level evidence for one saved run.

        The trace is re-loaded through the same load_trace path `wt rescore`
        uses, and the scenario definition comes from the packs discovered at
        startup. Degradation is graceful and explicit: an unknown scenario
        (or an unloadable trace) returns available=false with a reason the
        UI can show, never a fabricated highlight.
        """
        from windtunnel._serve.evidence import compute_evidence  # noqa: PLC0415
        from windtunnel.api.trace import load_trace  # noqa: PLC0415

        trace_path = _data.resolve_run_path(self.server.runs_dir, run_id)
        if trace_path is None:
            self._send_json({"error": f"no stored trace for run_id {run_id!r}"}, status=404)
            return

        try:
            trace = load_trace(trace_path)
        except Exception as exc:  # noqa: BLE001 - a bad trace degrades, never 500s
            self._send_json(
                {"available": False, "reason": f"trace could not be loaded: {exc}"}
            )
            return

        scenario = self.server.scenario_index.get(trace.scenario_id)
        if scenario is None:
            self._send_json(
                {
                    "available": False,
                    "reason": (
                        f"scenario {trace.scenario_id!r} is not among the discovered "
                        "packs — start wt serve with the --pack-source this run was "
                        "executed with to recompute evidence"
                    ),
                }
            )
            return

        try:
            evidence = compute_evidence(scenario, trace)
        except Exception as exc:  # noqa: BLE001 - same degradation stance
            self._send_json(
                {"available": False, "reason": f"evidence computation failed: {exc}"}
            )
            return
        self._send_json(
            {
                "available": True,
                "scenario": _data.scenario_summary(scenario),
                "evidence": evidence,
            }
        )

    # ── SSE live tail ────────────────────────────────────────────────────────

    def _stream_live(self) -> None:
        pattern = self.server.live_glob
        if pattern is None:
            self._send_json(
                {"error": "live watch is off; start wt serve with --live-glob <pattern>"},
                status=404,
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # SSE is an unbounded stream; chunked framing under HTTP/1.1 keep-alive
        # would require chunk headers per event. Closing delimits the body.
        self.send_header("Connection", "close")
        self.end_headers()

        offsets: dict[str, int] = {}
        # First scan primes offsets to end-of-file: tailing means NEW lines,
        # not replaying history. Files appearing later stream from offset 0.
        for path in _glob.glob(pattern, recursive=True):
            try:
                offsets[path] = Path(path).stat().st_size
            except OSError:
                continue

        self._sse_comment("live tail started")
        last_keepalive = time.monotonic()
        while True:
            emitted = False
            for path in sorted(_glob.glob(pattern, recursive=True)):
                for line in _read_new_lines(Path(path), offsets):
                    self._sse_event({"file": path, "line": line})
                    emitted = True
            now = time.monotonic()
            if emitted:
                last_keepalive = now
            elif now - last_keepalive >= _LIVE_KEEPALIVE_SECONDS:
                self._sse_comment("keep-alive")
                last_keepalive = now
            time.sleep(_LIVE_POLL_SECONDS)

    def _stream_experiment(self) -> None:
        """SSE stream of the current/last rerun's output lines.

        Replays the buffered lines from the top, then follows the live
        subprocess; the stream closes when the job finishes (or immediately
        after replay when no job is running), so a client reads one
        complete progress log per connection.
        """
        experiment = self.server.experiment
        if experiment is None:
            self._send_json(
                {"error": "experiment mode is off; start wt serve with --experiment"},
                status=404,
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        cursor = 0
        while True:
            lines = experiment.output_lines()
            while cursor < len(lines):
                self._sse_event({"line": lines[cursor]})
                cursor += 1
            if experiment.job_finished():
                status = experiment.status()
                self._sse_event({"done": True, "job": status["job"]})
                return
            time.sleep(_LIVE_POLL_SECONDS)

    def _sse_event(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {body}\n\n".encode())
        self.wfile.flush()

    def _sse_comment(self, note: str) -> None:
        self.wfile.write(f": {note}\n\n".encode())
        self.wfile.flush()


def _read_new_lines(path: Path, offsets: dict[str, int]) -> list[str]:
    """Return complete lines appended to path since the tracked offset.

    Only complete (newline-terminated) lines are consumed — a torn final
    line stays unread until its writer finishes it, so JSON lines are never
    delivered half-written. A shrunken file (rotation/truncation) restarts
    from the top.
    """
    key = str(path)
    offset = offsets.get(key, 0)
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            offsets[key] = offset
            return []
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
    except OSError:
        return []

    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        offsets[key] = offset  # incomplete line — wait for the writer
        return []
    complete, _partial = chunk[: last_newline + 1], chunk[last_newline + 1 :]
    offsets[key] = offset + last_newline + 1
    return [
        line.decode("utf-8", errors="replace")
        for line in complete.splitlines()
        if line.strip()
    ]
