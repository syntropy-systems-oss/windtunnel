"""Preference annotation — sibling pairs, the queue, and the NDJSON store.

Covers:
  1. Pair enumeration: within-label, cross-label, both; multi-run ledger rows
  2. The append-only store: row shape, tolerant read-back, run_id filtering
  3. Queue skip logic: annotated pairs skipped per annotator,
     order-independent pair identity, progress counts
  4. HTTP surface: siblings endpoint, queue, POST validation, and the
     opt-in posture (--annotate gating; POST refused without any flag)
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from windtunnel._serve.annotate import (
    annotated_pair_keys,
    append_annotation,
    enumerate_pairs,
    next_unlabeled_pair,
    pair_key,
    read_annotations,
    run_entries,
    sibling_runs,
)

# Synthetic ledger rows in the writer's shape: one scenario sampled twice
# under one label plus once under another, and an unrelated scenario.
_LEDGER_ROWS = [
    {"scenario_id": "acknowledge_ok", "label": "candidate",
     "run_ids": ["run-c1", "run-c2"], "verdict": "PASS", "ts": "2026-08-27T01:00:00Z"},
    {"scenario_id": "acknowledge_ok", "label": "baseline",
     "run_ids": ["run-b1"], "verdict": "FAIL", "ts": "2026-08-27T02:00:00Z"},
    {"scenario_id": "lookup_client_email", "label": "candidate",
     "run_ids": ["run-x1"], "verdict": "FAIL", "ts": "2026-08-27T03:00:00Z"},
]


class TestPairEnumeration:
    def test_run_entries_flatten_multi_run_rows(self) -> None:
        entries = run_entries(_LEDGER_ROWS)
        assert [entry["run_id"] for entry in entries] == ["run-c1", "run-c2", "run-b1", "run-x1"]
        assert entries[0]["label"] == "candidate"

    def test_within_label_pairs(self) -> None:
        pairs = enumerate_pairs(_LEDGER_ROWS, "within")
        assert [(a["run_id"], b["run_id"]) for a, b in pairs] == [("run-c1", "run-c2")]

    def test_cross_label_pairs(self) -> None:
        pairs = enumerate_pairs(_LEDGER_ROWS, "cross")
        assert {(a["run_id"], b["run_id"]) for a, b in pairs} == {
            ("run-c1", "run-b1"),
            ("run-c2", "run-b1"),
        }

    def test_both_is_the_union_and_never_crosses_scenarios(self) -> None:
        pairs = enumerate_pairs(_LEDGER_ROWS, "both")
        assert len(pairs) == 3
        for a, b in pairs:
            assert a["scenario_id"] == b["scenario_id"] == "acknowledge_ok"

    def test_sibling_runs_marks_same_label(self) -> None:
        payload = sibling_runs(_LEDGER_ROWS, "run-c1")
        assert payload["scenario_id"] == "acknowledge_ok"
        by_id = {sib["run_id"]: sib["same_label"] for sib in payload["siblings"]}
        assert by_id == {"run-c2": True, "run-b1": False}

    def test_unknown_run_has_no_siblings(self) -> None:
        assert sibling_runs(_LEDGER_ROWS, "missing")["siblings"] == []


class TestAnnotationStore:
    def test_append_shape_and_read_back(self, tmp_path: Path) -> None:
        record = append_annotation(
            tmp_path,
            scenario_id="acknowledge_ok",
            label_a="candidate", run_id_a="run-c1",
            label_b="baseline", run_id_b="run-b1",
            preferred="a", annotator="reviewer-1",
        )
        assert set(record) == {
            "ts", "scenario_id", "label_a", "run_id_a",
            "label_b", "run_id_b", "preferred", "annotator",
        }
        rows = read_annotations(tmp_path)["rows"]
        assert rows == [record]
        # No-preference judgments store null.
        append_annotation(
            tmp_path, scenario_id="acknowledge_ok",
            label_a="candidate", run_id_a="run-c1",
            label_b="candidate", run_id_b="run-c2",
            preferred=None, annotator="reviewer-1",
        )
        assert read_annotations(tmp_path)["rows"][0]["preferred"] is None  # newest first

    def test_tolerant_read_and_run_filter(self, tmp_path: Path) -> None:
        path = tmp_path / "annotations.ndjsonl"
        path.write_text(
            json.dumps({"run_id_a": "run-c1", "run_id_b": "run-b1", "preferred": "b"}) + "\n"
            "{torn line the annotator is still writ\n"
            '"not an object"\n'
            + json.dumps({"run_id_a": "run-x1", "run_id_b": "run-x2", "preferred": None}) + "\n",
            encoding="utf-8",
        )
        parsed = read_annotations(tmp_path)
        assert parsed["skipped"] == 2
        assert len(parsed["rows"]) == 2
        filtered = read_annotations(tmp_path, "run-b1")
        assert [row["run_id_a"] for row in filtered["rows"]] == ["run-c1"]

    def test_missing_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert read_annotations(tmp_path) == {"rows": [], "skipped": 0}


class TestQueueSkipLogic:
    def test_annotated_pairs_are_skipped_per_annotator(self, tmp_path: Path) -> None:
        first = next_unlabeled_pair(tmp_path, _LEDGER_ROWS, "reviewer-1", "both")
        assert first["progress"] == {"labeled": 0, "available": 3}
        pair = first["pair"]
        # Record the judgment with the pair REVERSED — identity is
        # order-independent, so the queue must still skip it.
        append_annotation(
            tmp_path, scenario_id="acknowledge_ok",
            label_a=pair["b"]["label"], run_id_a=pair["b"]["run_id"],
            label_b=pair["a"]["label"], run_id_b=pair["a"]["run_id"],
            preferred="b", annotator="reviewer-1",
        )
        second = next_unlabeled_pair(tmp_path, _LEDGER_ROWS, "reviewer-1", "both")
        assert second["progress"] == {"labeled": 1, "available": 3}
        assert pair_key(
            second["pair"]["a"]["run_id"], second["pair"]["b"]["run_id"]
        ) != pair_key(pair["a"]["run_id"], pair["b"]["run_id"])
        # A different annotator still sees the pair.
        other = next_unlabeled_pair(tmp_path, _LEDGER_ROWS, "reviewer-2", "both")
        assert other["progress"] == {"labeled": 0, "available": 3}

    def test_exhausted_queue_reports_done(self, tmp_path: Path) -> None:
        for a, b in enumerate_pairs(_LEDGER_ROWS, "within"):
            append_annotation(
                tmp_path, scenario_id=a["scenario_id"],
                label_a=a["label"], run_id_a=a["run_id"],
                label_b=b["label"], run_id_b=b["run_id"],
                preferred=None, annotator="reviewer-1",
            )
        result = next_unlabeled_pair(tmp_path, _LEDGER_ROWS, "reviewer-1", "within")
        assert result["pair"] is None
        assert result["progress"] == {"labeled": 1, "available": 1}

    def test_annotated_pair_keys_are_order_independent(self, tmp_path: Path) -> None:
        append_annotation(
            tmp_path, scenario_id="s", label_a="l", run_id_a="run-2",
            label_b="l", run_id_b="run-1", preferred="a", annotator="reviewer-1",
        )
        assert annotated_pair_keys(tmp_path, "reviewer-1") == {("run-1", "run-2")}
        assert annotated_pair_keys(tmp_path, "reviewer-2") == set()


# ─── HTTP surface ────────────────────────────────────────────────────────────

_PACK_SOURCE_TEXT = '''\
"""Fixture scenario pack for the annotation tests."""
from windtunnel.api.pack import ScenarioPack
from windtunnel.api.scenario import Scenario

PACK = ScenarioPack(
    name="annotation_pack",
    scenarios=[
        Scenario(name="acknowledge_ok", prompt="say ok", target_facts=[["ok"]]),
        Scenario(name="count_the_orders", prompt="say ok", target_facts=[["ok"]]),
    ],
)
'''


@pytest.fixture(scope="module")
def annotate_viewer(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """An --annotate viewer over sibling runs: 2× candidate + 1× baseline of
    one scenario, plus one run of an unrelated scenario."""
    import windtunnel.cli as cli
    from windtunnel._cli.scenario_discovery import _load_scenario_pack_source
    from windtunnel._serve.server import build_server

    root = tmp_path_factory.mktemp("annotate")
    pack_path = root / "annotation_pack.py"
    pack_path.write_text(_PACK_SOURCE_TEXT, encoding="utf-8")
    pack_source = f"{pack_path}:PACK"
    runs_dir = root / "runs"
    for scenario, label in (
        ("acknowledge_ok", "candidate"),
        ("acknowledge_ok", "candidate"),
        ("acknowledge_ok", "baseline"),
        ("count_the_orders", "candidate"),
    ):
        rc = cli.main([
            "run", "--pack-source", pack_source, "--scenario", scenario,
            "--label", label, "--runs-dir", str(runs_dir),
        ])
        assert rc == 0

    server = build_server(
        runs_dir=runs_dir,
        packs=[_load_scenario_pack_source(pack_source)],
        annotator="reviewer-1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.bound_address
    yield SimpleNamespace(base=f"http://{host}:{port}", runs_dir=runs_dir)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _runs_by(base: str) -> dict[tuple[str, str], list[str]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in _get(base, "/api/ledger")["rows"]:
        grouped.setdefault((row["scenario_id"], row["label"]), []).extend(row["run_ids"])
    return grouped


class TestAnnotateHttpSurface:
    def test_meta_reports_annotate_mode(self, annotate_viewer: SimpleNamespace) -> None:
        meta = _get(annotate_viewer.base, "/api/meta")
        assert meta["annotate"] is True
        assert meta["annotator"] == "reviewer-1"

    def test_siblings_endpoint_marks_labels(self, annotate_viewer: SimpleNamespace) -> None:
        runs = _runs_by(annotate_viewer.base)
        me = runs[("acknowledge_ok", "candidate")][0]
        payload = _get(annotate_viewer.base, f"/api/siblings/{me}")
        assert payload["scenario_id"] == "acknowledge_ok"
        flags = sorted(sib["same_label"] for sib in payload["siblings"])
        assert flags == [False, True]  # one cross-label + one within-label sibling

    def test_full_labeling_round_trip(self, annotate_viewer: SimpleNamespace) -> None:
        base = annotate_viewer.base
        queue = _get(base, "/api/annotate/queue?mode=both")
        assert queue["progress"] == {"labeled": 0, "available": 3}
        pair = queue["pair"]
        status, payload = _post(base, "/api/annotate", {
            "run_id_a": pair["a"]["run_id"],
            "run_id_b": pair["b"]["run_id"],
            "preferred": "b",
        })
        assert status == 200
        annotation = payload["annotation"]
        assert annotation["preferred"] == "b"
        assert annotation["annotator"] == "reviewer-1"
        assert annotation["scenario_id"] == "acknowledge_ok"
        # Identity fields are derived from the stored traces.
        assert {annotation["label_a"], annotation["label_b"]} <= {"candidate", "baseline"}
        # The file is append-only NDJSON beside the ledger.
        lines = (annotate_viewer.runs_dir / "annotations.ndjsonl").read_text().splitlines()
        assert json.loads(lines[-1]) == annotation
        # Queue advances; read-back filters by run.
        after = _get(base, "/api/annotate/queue?mode=both")
        assert after["progress"]["labeled"] == 1
        involving = _get(base, f"/api/annotations?run_id={pair['a']['run_id']}")
        assert involving["rows"][0] == annotation

    def test_post_validation(self, annotate_viewer: SimpleNamespace) -> None:
        base = annotate_viewer.base
        runs = _runs_by(base)
        a = runs[("acknowledge_ok", "candidate")][0]
        other = runs[("count_the_orders", "candidate")][0]
        assert _post(base, "/api/annotate", {"run_id_a": a, "run_id_b": a})[0] == 400
        assert _post(base, "/api/annotate", {"run_id_a": a, "run_id_b": "missing"})[0] == 404
        status, payload = _post(base, "/api/annotate",
                                {"run_id_a": a, "run_id_b": other})
        assert status == 400 and "siblings" in payload["error"]
        assert _post(base, "/api/annotate",
                     {"run_id_a": a, "run_id_b": other, "preferred": "left"})[0] == 400

    def test_queue_mode_validation(self, annotate_viewer: SimpleNamespace) -> None:
        status, payload = _post(annotate_viewer.base, "/api/experiment/rerun", {"run_id": "x"})
        assert status == 404  # experiment stays off on an annotate-only server
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                annotate_viewer.base + "/api/annotate/queue?mode=sideways", timeout=10
            )
        assert excinfo.value.code == 400
