"""Tests for `wt serve` — the local, read-only run viewer.

Covers:
  - wt serve --help works and names its options
  - ledger parsing: real writer round-trip, malformed lines, newest-first order
  - run drill-down: trace + .score.json sidecar resolution by run_id
  - scenario introspection: pack/scenario summaries and the declared tool surface
  - HTTP endpoints against a live server on an ephemeral port
  - run evidence: /api/run/<id>/evidence recomputes span-level evidence from
    (Scenario, Trace) via the scoring matchers, and degrades gracefully when
    the scenario is not among the discovered packs
  - the server is read-only: GET-only, and the runs/ directory is never modified
  - live watch: SSE tail streams appended complete JSONL lines, never history
    or torn lines
  - leak hygiene: the new source files bake in no absolute paths, hostnames,
    or IPs beyond loopback
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

# Fixture pack in the style of the built-in scenarios: one scenario the
# scripted in_memory runtime passes, one it fails (it answers "ok" and calls
# no tools), so the seeded runs/ dir carries both PASS and FAIL ledger rows.
_PACK_SOURCE_TEXT = '''\
"""Fixture scenario pack for the run-viewer tests."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario
from windtunnel.api.score import FailureCost

PACK = ScenarioPack(
    name="viewer_pack",
    owner="bench-team",
    scenarios=[
        Scenario(
            name="acknowledge_ok",
            prompt="say ok",
            target_facts=[["ok"]],
        ),
        Scenario(
            name="lookup_client_email",
            prompt="What is the email on file for the client Bluewing Logistics?",
            target_facts=[["ops@bluewing.example"]],
            must_call=["client_lookup"],
            requires_tool_use=True,
            failure_cost=FailureCost(severity="medium", customer_visible=True),
        ),
    ],
)
'''


def _wt(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `wt` CLI via `python -m windtunnel.cli` and return CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "windtunnel.cli", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Seed a runs/ dir through the REAL `wt run` pipeline (in_memory runtime).

    Using the actual CLI writer (ledger append, trace save, score sidecar)
    keeps these tests honest about the artifact shapes `wt serve` reads —
    a drifted writer fails here, not in a hand-built fixture.
    """
    import windtunnel.cli as cli

    root = tmp_path_factory.mktemp("serve")
    pack_path = root / "viewer_pack.py"
    pack_path.write_text(_PACK_SOURCE_TEXT, encoding="utf-8")
    pack_source = f"{pack_path}:PACK"
    runs_dir = root / "runs"
    rc = cli.main([
        "run",
        "--pack-source", pack_source,
        "--pack", "viewer_pack",
        "--label", "candidate",
        "--runs-dir", str(runs_dir),
    ])
    assert rc == 1  # lookup_client_email fails by design (no tool use)
    live_dir = root / "live"
    live_dir.mkdir()
    return SimpleNamespace(runs_dir=runs_dir, pack_source=pack_source, live_dir=live_dir)


@pytest.fixture(scope="module")
def viewer(seeded: SimpleNamespace) -> SimpleNamespace:
    """A live viewer on an ephemeral port over the seeded runs/ directory."""
    from windtunnel._cli.scenario_discovery import _load_scenario_pack_source
    from windtunnel._serve.server import build_server

    packs = [_load_scenario_pack_source(seeded.pack_source)]
    server = build_server(
        runs_dir=seeded.runs_dir,
        packs=packs,
        live_glob=str(seeded.live_dir / "*.jsonl"),
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.bound_address
    yield SimpleNamespace(
        base=f"http://{host}:{port}",
        runs_dir=seeded.runs_dir,
        live_dir=seeded.live_dir,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class TestServeHelp:
    def test_serve_help_exits_zero(self) -> None:
        result = _wt("serve", "--help")
        assert result.returncode == 0

    def test_serve_help_mentions_options(self) -> None:
        result = _wt("serve", "--help")
        for option in ("--runs-dir", "--port", "--pack-source", "--live-glob"):
            assert option in result.stdout


class TestLedgerParsing:
    def test_missing_ledger_is_empty_not_an_error(self, tmp_path: Path) -> None:
        from windtunnel._serve.data import load_ledger_rows

        assert load_ledger_rows(tmp_path) == {"rows": [], "skipped": 0}

    def test_malformed_lines_are_skipped_and_counted(self, tmp_path: Path) -> None:
        from windtunnel._serve.data import load_ledger_rows

        (tmp_path / "ledger.ndjsonl").write_text(
            '{"scenario_id": "a", "ts": "2026-01-01T00:00:00Z"}\n'
            "{torn line the sweep is still writ\n"
            '"a bare string is not a row"\n'
            '{"scenario_id": "b", "ts": "2026-01-02T00:00:00Z"}\n',
            encoding="utf-8",
        )
        parsed = load_ledger_rows(tmp_path)
        assert parsed["skipped"] == 2
        assert [row["scenario_id"] for row in parsed["rows"]] == ["b", "a"]

    def test_rows_sort_newest_first_by_ts(self, tmp_path: Path) -> None:
        from windtunnel._serve.data import load_ledger_rows

        (tmp_path / "ledger.ndjsonl").write_text(
            '{"scenario_id": "later", "ts": "2026-03-01T00:00:00Z"}\n'
            '{"scenario_id": "earlier", "ts": "2026-02-01T00:00:00Z"}\n',
            encoding="utf-8",
        )
        rows = load_ledger_rows(tmp_path)["rows"]
        assert [row["scenario_id"] for row in rows] == ["later", "earlier"]

    def test_real_writer_round_trip(self, seeded: SimpleNamespace) -> None:
        """Rows written by `wt run`'s ledger append parse with the fields the
        dashboard renders: identity, verdict vocabulary, layer pass rates."""
        from windtunnel._serve.data import load_ledger_rows

        parsed = load_ledger_rows(seeded.runs_dir)
        assert parsed["skipped"] == 0
        rows = parsed["rows"]
        assert {row["scenario_id"] for row in rows} == {
            "acknowledge_ok",
            "lookup_client_email",
        }
        for row in rows:
            assert row["pack"] == "viewer_pack"
            assert row["label"] == "candidate"
            assert row["verdict"] in {"PASS", "FAIL", "PASS_WITH_VARIANCE", "INVALID"}
            assert set(row["layer_pass_rates"]) == {
                "outcome",
                "trajectory",
                "constraint",
                "integrity",
            }
            assert row["run_ids"]
            assert row["ts"]
            assert "git_sha" in row


class TestRunResolution:
    def _first_run_id(self, runs_dir: Path) -> str:
        from windtunnel._serve.data import load_ledger_rows

        rows = load_ledger_rows(runs_dir)["rows"]
        failed = [row for row in rows if row["verdict"] == "FAIL"]
        return failed[0]["run_ids"][0]

    def test_resolves_trace_and_score_sidecar(self, seeded: SimpleNamespace) -> None:
        from windtunnel._serve.data import resolve_run

        run_id = self._first_run_id(seeded.runs_dir)
        resolved = resolve_run(seeded.runs_dir, run_id)
        assert resolved is not None
        assert resolved["trace"]["run_id"] == run_id
        assert resolved["trace"]["scenario_id"] == "lookup_client_email"
        assert resolved["trace"]["turns"]
        score = resolved["score"]
        assert score is not None
        assert score["verdict"] == "FAIL"
        # The four layer verdicts with their detail strings — the drill-down's
        # "why it failed" line comes straight from these.
        for layer in ("outcome", "trajectory", "constraint", "integrity"):
            assert isinstance(score[layer]["passed"], bool)
            assert isinstance(score[layer]["detail"], str)
        assert score["outcome"]["passed"] is False

    def test_unknown_run_id_is_none(self, seeded: SimpleNamespace) -> None:
        from windtunnel._serve.data import resolve_run

        assert resolve_run(seeded.runs_dir, "no-such-run-id") is None

    def test_path_syntax_never_reaches_the_walk(self, seeded: SimpleNamespace) -> None:
        from windtunnel._serve.data import resolve_run

        assert resolve_run(seeded.runs_dir, "../ledger") is None
        assert resolve_run(seeded.runs_dir, "a/b") is None
        assert resolve_run(seeded.runs_dir, "*") is None


class TestScenarioIntrospection:
    def test_pack_summary_carries_authored_expectations(
        self, seeded: SimpleNamespace
    ) -> None:
        from windtunnel._cli.scenario_discovery import _load_scenario_pack_source
        from windtunnel._serve.data import pack_summaries

        packs = pack_summaries([_load_scenario_pack_source(seeded.pack_source)])
        assert [pack["name"] for pack in packs] == ["viewer_pack"]
        pack = packs[0]
        assert pack["owner"] == "bench-team"
        summaries = {scenario["name"]: scenario for scenario in pack["scenarios"]}
        lookup = summaries["lookup_client_email"]
        assert lookup["must_call"] == ["client_lookup"]
        assert lookup["requires_tool_use"] is True
        assert lookup["target_facts"] == [["ops@bluewing.example"]]
        assert lookup["gate_layers"] == ["outcome", "trajectory"]
        assert lookup["failure_cost"]["severity"] == "medium"
        assert lookup["failure_cost"]["risk_weight"] == 6
        assert summaries["acknowledge_ok"]["gate_layers"] == ["outcome"]

    def test_summaries_are_json_serializable_for_builtin_packs(self) -> None:
        from windtunnel._serve.data import pack_summaries
        from windtunnel.scenarios import builtin_packs

        packs = pack_summaries(list(builtin_packs()))
        json.dumps(packs)  # the endpoint contract: plain data all the way down
        assert packs  # built-in dims exist
        perturbed = [
            scenario
            for pack in packs
            for scenario in pack["scenarios"]
            if scenario["perturbations"]
        ]
        assert perturbed, "some built-in scenario should declare perturbations"
        assert all(entry["type"] for scenario in perturbed for entry in scenario["perturbations"])

    def test_tool_surface_lists_introspectable_server(self) -> None:
        from windtunnel._serve.data import pack_summaries
        from windtunnel.api.pack import ScenarioPack
        from windtunnel.api.scenario import Scenario

        class IntrospectableServer:
            def served_tools(self) -> list[str]:
                return ["client_lookup", "order_query"]

            def start(self):  # pragma: no cover - the viewer must never call this
                raise AssertionError("wt serve must not start mock servers")

            def stop(self) -> None:  # pragma: no cover
                raise AssertionError("wt serve must not stop mock servers")

        pack = ScenarioPack(
            name="introspectable",
            scenarios=[Scenario(name="one", prompt="say ok", target_facts=[["ok"]])],
            mcp_factory=lambda _scenario: IntrospectableServer(),
        )
        surface = pack_summaries([pack])[0]["tool_surface"]
        assert surface["declared"] is True
        assert surface["tools"] == ["client_lookup", "order_query"]

    def test_tool_surface_reports_honest_absence_without_starting(self) -> None:
        from windtunnel._serve.data import pack_summaries
        from windtunnel.api.pack import ScenarioPack
        from windtunnel.api.scenario import Scenario

        class OpaqueServer:
            def start(self):  # pragma: no cover
                raise AssertionError("wt serve must not start mock servers")

            def stop(self) -> None:  # pragma: no cover
                raise AssertionError("wt serve must not stop mock servers")

        with_factory = ScenarioPack(
            name="opaque",
            scenarios=[Scenario(name="one", prompt="say ok", target_facts=[["ok"]])],
            mcp_factory=lambda _scenario: OpaqueServer(),
        )
        without_factory = ScenarioPack(name="bare", scenarios=[])
        surfaces = [pack["tool_surface"] for pack in pack_summaries([with_factory, without_factory])]
        assert surfaces[0]["declared"] is True
        assert surfaces[0]["tools"] is None
        assert "starting" in surfaces[0]["detail"]
        assert surfaces[1]["declared"] is False


class TestHttpEndpoints:
    def test_page_is_self_contained(self, viewer: SimpleNamespace) -> None:
        with urllib.request.urlopen(viewer.base + "/", timeout=10) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            body = response.read().decode("utf-8")
        assert "Wind Tunnel run viewer" in body
        # Zero external requests: no URL in the page points off-box.
        for url in re.findall(r"https?://[^\s\"'<>]+", body):
            assert url.startswith(("http://localhost", "http://127.0.0.1")), url

    def test_ledger_endpoint(self, viewer: SimpleNamespace) -> None:
        payload = _get_json(viewer.base, "/api/ledger")
        assert len(payload["rows"]) == 2
        assert payload["rows"][0]["verdict"] in {"PASS", "FAIL"}

    def test_run_endpoint_round_trip(self, viewer: SimpleNamespace) -> None:
        rows = _get_json(viewer.base, "/api/ledger")["rows"]
        run_id = rows[0]["run_ids"][0]
        payload = _get_json(viewer.base, f"/api/run/{run_id}")
        assert payload["trace"]["run_id"] == run_id
        assert payload["score"]["verdict"] in {"PASS", "FAIL"}
        assert payload["trace_file"].endswith(".json")

    def test_run_endpoint_unknown_id_404(self, viewer: SimpleNamespace) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(viewer.base + "/api/run/unknown", timeout=10)
        assert excinfo.value.code == 404

    def test_scenarios_endpoint(self, viewer: SimpleNamespace) -> None:
        payload = _get_json(viewer.base, "/api/scenarios")
        names = [pack["name"] for pack in payload["packs"]]
        assert names == ["viewer_pack"]

    def test_meta_endpoint(self, viewer: SimpleNamespace) -> None:
        payload = _get_json(viewer.base, "/api/meta")
        assert payload["live_glob"].endswith("*.jsonl")
        assert "wt_version" in payload

    def test_unknown_path_404(self, viewer: SimpleNamespace) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(viewer.base + "/api/nope", timeout=10)
        assert excinfo.value.code == 404


class TestEvidenceEndpoint:
    def _run_id_for(self, viewer: SimpleNamespace, scenario_id: str) -> str:
        rows = _get_json(viewer.base, "/api/ledger")["rows"]
        return next(r for r in rows if r["scenario_id"] == scenario_id)["run_ids"][0]

    def test_failing_run_evidence_names_the_misses(self, viewer: SimpleNamespace) -> None:
        run_id = self._run_id_for(viewer, "lookup_client_email")
        payload = _get_json(viewer.base, f"/api/run/{run_id}/evidence")
        assert payload["available"] is True
        assert payload["scenario"]["name"] == "lookup_client_email"
        outcome = payload["evidence"]["outcome"]
        assert outcome["fact_groups"] == [
            {"group": ["ops@bluewing.example"], "matched": False, "spans": []}
        ]
        assert outcome["tool_use_required"] is True
        assert outcome["tool_use_observed"] is False
        trajectory = payload["evidence"]["trajectory"]
        assert trajectory["must_call"] == [
            {"entry": "client_lookup", "satisfied": False, "matched_calls": []}
        ]

    def test_passing_run_evidence_spans_slice_the_answer(
        self, viewer: SimpleNamespace
    ) -> None:
        run_id = self._run_id_for(viewer, "acknowledge_ok")
        run = _get_json(viewer.base, f"/api/run/{run_id}")
        payload = _get_json(viewer.base, f"/api/run/{run_id}/evidence")
        outcome = payload["evidence"]["outcome"]
        group = outcome["fact_groups"][0]
        assert group["matched"] is True
        assert group["spans"]
        span = group["spans"][0]
        answer = run["trace"]["turns"][outcome["answer_turn_index"]]["content"]
        assert answer[span["start"]:span["end"]].lower() == "ok"

    def test_evidence_agrees_with_the_stored_verdict(self, viewer: SimpleNamespace) -> None:
        """The design law, end to end: recomputed fact evidence must agree
        with the outcome verdict the scorer wrote at run time."""
        rows = _get_json(viewer.base, "/api/ledger")["rows"]
        for row in rows:
            run_id = row["run_ids"][0]
            run = _get_json(viewer.base, f"/api/run/{run_id}")
            payload = _get_json(viewer.base, f"/api/run/{run_id}/evidence")
            outcome_ev = payload["evidence"]["outcome"]
            outcome_passed = run["score"]["outcome"]["passed"]
            facts_ok = all(group["matched"] for group in outcome_ev["fact_groups"])
            tool_gate_ok = (not outcome_ev["tool_use_required"]) or outcome_ev[
                "tool_use_observed"
            ]
            assert (facts_ok and tool_gate_ok) == outcome_passed, row["scenario_id"]

    def test_unknown_run_id_404(self, viewer: SimpleNamespace) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(viewer.base + "/api/run/unknown/evidence", timeout=10)
        assert excinfo.value.code == 404

    def test_degrades_without_the_scenario_pack(self, seeded: SimpleNamespace) -> None:
        """A server started without the run's pack must say evidence needs
        --pack-source rather than fabricating highlights."""
        from windtunnel._serve.server import build_server

        server = build_server(runs_dir=seeded.runs_dir, packs=[], port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.bound_address
            base = f"http://{host}:{port}"
            run_id = _get_json(base, "/api/ledger")["rows"][0]["run_ids"][0]
            payload = _get_json(base, f"/api/run/{run_id}/evidence")
            assert payload["available"] is False
            assert "--pack-source" in payload["reason"]
            assert "evidence" not in payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestEvidenceComputation:
    """compute_evidence over hand-built traces — the constructs the seeded
    in_memory runs cannot produce (witnessed mcp_calls, decorated names,
    forbidden calls, perturbation markers)."""

    def _trace(self, turns: list, mcp_calls: list | None = None, warnings: list | None = None):
        from datetime import UTC, datetime, timedelta

        from windtunnel.api.trace import Trace

        started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        return Trace(
            scenario_id="evidence_case",
            agent_id="wt-cli",
            variant_id="candidate",
            model="model-x",
            quant="q4",
            sampler={},
            started_at=started,
            finished_at=started + timedelta(seconds=1),
            turns=turns,
            tool_schema_hash=None,
            worker_warnings=list(warnings or []),
            mcp_calls=list(mcp_calls or []),
        )

    def _turn(self, role: str, content: str, tool_calls: list | None = None):
        from windtunnel.api.trace import Turn

        return Turn(
            role=role,
            content=content,
            tool_calls=list(tool_calls or []),
            tool_results=[],
            latency_ms=1.0,
        )

    def test_witnessed_decorated_calls_satisfy_and_violate(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Scenario

        scenario = Scenario(
            name="evidence_case",
            prompt="look up the client, never delete",
            target_facts=[["ok"]],
            must_call=["client_lookup", ["order_query", "order_search"]],
            forbidden_calls=["delete_record"],
        )
        trace = self._trace(
            turns=[self._turn("user", "go"), self._turn("assistant", "ok")],
            mcp_calls=[
                {"tool_name": "mcp_acme_ops_client_lookup", "args": {}, "result": "", "timestamp_ms": 1},
                {"tool_name": "order_search", "args": {}, "result": "", "timestamp_ms": 2},
                {"tool_name": "ops.delete_record", "args": {}, "result": "", "timestamp_ms": 3},
            ],
        )
        trajectory = compute_evidence(scenario, trace)["trajectory"]
        assert trajectory["evidence_source"] == "server-witnessed"
        assert trajectory["observed_calls"] == [
            "mcp_acme_ops_client_lookup",
            "order_search",
            "ops.delete_record",
        ]
        assert trajectory["must_call"] == [
            {"entry": "client_lookup", "satisfied": True, "matched_calls": [0]},
            {"entry": ["order_query", "order_search"], "satisfied": True, "matched_calls": [1]},
        ]
        assert trajectory["forbidden_calls"] == [
            {"name": "delete_record", "violated": True, "offending_calls": [2]}
        ]

    def test_transcript_fallback_source(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Scenario

        scenario = Scenario(name="evidence_case", prompt="go", must_call=["client_lookup"])
        trace = self._trace(
            turns=[
                self._turn(
                    "assistant",
                    "done",
                    tool_calls=[{"id": "call_1", "name": "client_lookup", "args": {}}],
                )
            ],
        )
        trajectory = compute_evidence(scenario, trace)["trajectory"]
        assert trajectory["evidence_source"] == "transcript"
        assert trajectory["must_call"][0]["satisfied"] is True

    def test_order_evidence_mirrors_the_subsequence_walk(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Scenario

        scenario = Scenario(
            name="evidence_case",
            prompt="go",
            must_call=["client_lookup", "order_query"],
            order_matters=True,
        )
        out_of_order = self._trace(
            turns=[self._turn("assistant", "done")],
            mcp_calls=[
                {"tool_name": "order_query", "args": {}, "result": "", "timestamp_ms": 1},
                {"tool_name": "client_lookup", "args": {}, "result": "", "timestamp_ms": 2},
            ],
        )
        trajectory = compute_evidence(scenario, out_of_order)["trajectory"]
        assert trajectory["order_matters"] is True
        assert trajectory["order_satisfied"] is False

    def test_forbidden_fact_spans_slice_the_answer(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Scenario

        scenario = Scenario(
            name="evidence_case",
            prompt="which module is broken?",
            target_facts=[["divide"]],
            forbidden_facts=["multiply"],
        )
        answer = "the divide module is broken, and so is multiply"
        trace = self._trace(turns=[self._turn("assistant", answer)])
        outcome = compute_evidence(scenario, trace)["outcome"]
        assert outcome["fact_groups"][0]["matched"] is True
        forbidden = outcome["forbidden_facts"][0]
        assert forbidden["asserted"] is True
        span = forbidden["spans"][0]
        assert answer[span["start"]:span["end"]] == "multiply"

    def test_integrity_marker_presence(self) -> None:
        from dataclasses import dataclass

        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Perturbation, Scenario
        from windtunnel.api.trace import Trace

        @dataclass
        class MarkerOnly(Perturbation):
            def apply(self, trace: Trace) -> Trace:  # pragma: no cover - never run here
                return trace

            @property
            def marker(self) -> str:
                return "perturbation_applied: test_condition"

        scenario = Scenario(
            name="evidence_case", prompt="go", perturbations=[MarkerOnly()]
        )
        applied = self._trace(
            turns=[self._turn("assistant", "ok")],
            warnings=["perturbation_applied: test_condition turn_idx=0"],
        )
        missing = self._trace(turns=[self._turn("assistant", "ok")])
        assert compute_evidence(scenario, applied)["integrity"]["markers"] == [
            {
                "type": "MarkerOnly",
                "marker": "perturbation_applied: test_condition",
                "applied": True,
                "warning_index": 0,
            }
        ]
        assert compute_evidence(scenario, missing)["integrity"]["markers"][0]["applied"] is False

    def test_custom_outcome_fn_yields_no_fact_spans(self) -> None:
        from windtunnel._serve.evidence import compute_evidence
        from windtunnel.api.scenario import Scenario
        from windtunnel.api.score import LayerResult

        scenario = Scenario(
            name="evidence_case",
            prompt="go",
            target_facts=[["ignored"]],
            outcome_fn=lambda trace: LayerResult(passed=True, detail="custom"),
        )
        trace = self._trace(turns=[self._turn("assistant", "ignored appears here")])
        outcome = compute_evidence(scenario, trace)["outcome"]
        assert outcome["custom_outcome_fn"] is True
        assert outcome["fact_groups"] == []


class TestReadOnlyByConstruction:
    def _fingerprint(self, runs_dir: Path) -> dict[str, str]:
        return {
            path.relative_to(runs_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(runs_dir.rglob("*"))
            if path.is_file()
        }

    def test_non_get_methods_are_not_implemented(self, viewer: SimpleNamespace) -> None:
        for method in ("POST", "PUT", "DELETE"):
            request = urllib.request.Request(
                viewer.base + "/api/ledger", data=b"{}", method=method
            )
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(request, timeout=10)
            assert excinfo.value.code == 501  # unsupported method — nothing to mutate

    def test_runs_dir_is_untouched_by_every_endpoint(self, viewer: SimpleNamespace) -> None:
        before = self._fingerprint(viewer.runs_dir)
        rows = _get_json(viewer.base, "/api/ledger")["rows"]
        _get_json(viewer.base, f"/api/run/{rows[0]['run_ids'][0]}")
        _get_json(viewer.base, "/api/scenarios")
        _get_json(viewer.base, "/api/meta")
        urllib.request.urlopen(viewer.base + "/", timeout=10).read()
        assert self._fingerprint(viewer.runs_dir) == before


class TestLiveWatch:
    def test_tail_streams_new_complete_lines_only(self, viewer: SimpleNamespace) -> None:
        log_path = viewer.live_dir / "session.jsonl"
        log_path.write_text('{"type": "text_delta", "text": "history"}\n', encoding="utf-8")

        response = urllib.request.urlopen(viewer.base + "/api/live", timeout=10)
        # The first comment arrives after the tail primed its offsets, so
        # everything below is deterministically "new".
        first = response.readline().decode("utf-8")
        assert first.startswith(": live tail started")

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "text_delta"')  # torn line: must NOT stream yet
            handle.flush()
            handle.write(', "text": "hello"}\n')
            handle.write("plain non-JSON line\n")

        events = []
        while len(events) < 2:
            line = response.readline().decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
        response.close()

        # The pre-existing history line was never replayed, and the torn line
        # arrived exactly once, as one completed line.
        assert [event["line"] for event in events] == [
            '{"type": "text_delta", "text": "hello"}',
            "plain non-JSON line",
        ]
        assert all(event["file"].endswith("session.jsonl") for event in events)

    def test_live_endpoint_404s_without_a_glob(self, seeded: SimpleNamespace) -> None:
        from windtunnel._cli.scenario_discovery import _load_scenario_pack_source
        from windtunnel._serve.server import build_server

        server = build_server(
            runs_dir=seeded.runs_dir,
            packs=[_load_scenario_pack_source(seeded.pack_source)],
            live_glob=None,
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.bound_address
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"http://{host}:{port}/api/live", timeout=10)
            assert excinfo.value.code == 404
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestNoEnvironmentLeak:
    """The viewer is generic infrastructure: nothing about any particular
    deployment may be baked into its source. Rather than a denylist of
    known-sensitive names, assert structural invariants — no absolute
    filesystem paths, no non-loopback IPs, no real-looking hostnames."""

    _NEW_FILES = (
        "windtunnel/_serve/__init__.py",
        "windtunnel/_serve/data.py",
        "windtunnel/_serve/evidence.py",
        "windtunnel/_serve/server.py",
        "windtunnel/_serve/page.py",
        "tests/test_cli_serve.py",
        "tests/test_matching_spans.py",
        "docs/viewing-runs.md",
    )

    def _sources(self) -> list[tuple[str, str]]:
        root = Path(__file__).resolve().parents[1]
        return [(name, (root / name).read_text(encoding="utf-8")) for name in self._NEW_FILES]

    def test_no_absolute_filesystem_paths(self) -> None:
        # Prefixes are assembled from parts so this file's own denylist does
        # not trip the scan of this file.
        prefixes = ["/" + part + "/" for part in ("home", "Users", "srv", "opt", "var")]
        prefixes.append("C:" + "\\")
        for name, text in self._sources():
            for prefix in prefixes:
                assert prefix not in text, f"{name} bakes in an absolute path ({prefix}…)"

    def test_no_ips_beyond_loopback(self) -> None:
        allowed = {"127.0.0.1", "0.0.0.0"}
        pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for name, text in self._sources():
            found = set(pattern.findall(text)) - allowed
            # CSS/JS numeric coincidences (e.g. version-like tokens) don't
            # match this pattern; any hit is a literal dotted quad.
            assert not found, f"{name} bakes in IP address(es): {sorted(found)}"

    def test_no_urls_beyond_loopback(self) -> None:
        pattern = re.compile(r"https?://[^\s\"'<>)]+")
        for name, text in self._sources():
            for url in pattern.findall(text):
                assert url.startswith(
                    ("http://localhost", "http://127.0.0.1", "http://{host}")
                ), f"{name} bakes in URL: {url}"

    def test_no_real_looking_hostnames(self) -> None:
        # Reserved documentation domains (.example, example.com) are the only
        # host-shaped strings the OSS repo's own fixtures use.
        pattern = re.compile(
            r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|ai|dev|cloud|internal|local)\b"
        )
        allowed = {"example.com"}
        for name, text in self._sources():
            found = set(pattern.findall(text)) - allowed
            assert not found, f"{name} bakes in hostname-like token(s): {sorted(found)}"
