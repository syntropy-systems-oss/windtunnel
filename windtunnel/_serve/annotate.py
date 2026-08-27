"""Preference annotation over sibling runs — the wt serve --annotate surface.

Runs that share a scenario_id are siblings: the same task, sampled
completions (same label = same arm/config; different labels = different
arms). Comparing two siblings side-by-side and recording which one a human
prefers turns bench artifacts into a preference dataset.

Storage is one append-only NDJSON file beside the ledger:

    <runs-dir>/annotations.ndjsonl

one row per judgment:

    {"ts": ..., "scenario_id": ..., "label_a": ..., "run_id_a": ...,
     "label_b": ..., "run_id_b": ..., "preferred": "a"|"b"|null,
     "annotator": ...}

Nothing under runs/ is ever edited — annotation appends exactly like the
sweep ledger does, and reading is tolerant of torn/malformed lines for the
same reason. Without --annotate the viewer keeps its read-only posture:
the write endpoint refuses and the queue does not exist; the read-side
display of previously recorded annotations stays available everywhere.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

ANNOTATIONS_FILENAME = "annotations.ndjsonl"

PairMode = str  # "within" | "cross" | "both"
PAIR_MODES = ("within", "cross", "both")


# ─── reading (available everywhere, read-only) ───────────────────────────────


def read_annotations(runs_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    """Parse annotations.ndjsonl, newest first; optionally filter by run.

    Same tolerance stance as the ledger reader: malformed lines are counted
    and skipped, a missing file is an empty dataset, never an error.
    """
    path = Path(runs_dir) / ANNOTATIONS_FILENAME
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"rows": [], "skipped": 0}
    for line in text.splitlines():
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
        rows.append(record)
    if run_id is not None:
        rows = [
            row
            for row in rows
            if run_id in (row.get("run_id_a"), row.get("run_id_b"))
        ]
    rows.reverse()  # append order is chronological; render newest first
    return {"rows": rows, "skipped": skipped}


# ─── pair enumeration ────────────────────────────────────────────────────────


def run_entries(ledger_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ledger rows into one entry per stored run."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ledger_rows:
        for run_id in row.get("run_ids") or []:
            if not isinstance(run_id, str) or run_id in seen:
                continue
            seen.add(run_id)
            entries.append(
                {
                    "run_id": run_id,
                    "scenario_id": row.get("scenario_id"),
                    "label": row.get("label"),
                    "verdict": row.get("verdict"),
                    "ts": row.get("ts"),
                }
            )
    return entries


def sibling_runs(ledger_rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """All runs sharing the given run's scenario_id, excluding itself."""
    entries = run_entries(ledger_rows)
    me = next((entry for entry in entries if entry["run_id"] == run_id), None)
    if me is None:
        return {"scenario_id": None, "label": None, "siblings": []}
    siblings = [
        {**entry, "same_label": entry["label"] == me["label"]}
        for entry in entries
        if entry["scenario_id"] == me["scenario_id"] and entry["run_id"] != run_id
    ]
    return {"scenario_id": me["scenario_id"], "label": me["label"], "siblings": siblings}


def enumerate_pairs(
    ledger_rows: list[dict[str, Any]], mode: PairMode
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Comparable sibling pairs, deterministic order.

    within: same scenario_id AND same label (sampled completions of one arm)
    cross:  same scenario_id, different labels (arm-vs-arm comparison)
    both:   the union
    """
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for entry in run_entries(ledger_rows):
        scenario_id = str(entry.get("scenario_id") or "")
        by_scenario.setdefault(scenario_id, []).append(entry)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for scenario_id in sorted(by_scenario):
        entries = sorted(
            by_scenario[scenario_id], key=lambda e: (str(e["ts"] or ""), e["run_id"])
        )
        for entry_a, entry_b in combinations(entries, 2):
            same_label = entry_a["label"] == entry_b["label"]
            if mode == "within" and not same_label:
                continue
            if mode == "cross" and same_label:
                continue
            pairs.append((entry_a, entry_b))
    return pairs


def pair_key(run_id_a: str, run_id_b: str) -> tuple[str, str]:
    """Order-independent identity of a compared pair."""
    return (run_id_a, run_id_b) if run_id_a <= run_id_b else (run_id_b, run_id_a)


def annotated_pair_keys(runs_dir: Path, annotator: str) -> set[tuple[str, str]]:
    """Pairs this annotator has already judged (order-independent)."""
    keys: set[tuple[str, str]] = set()
    for row in read_annotations(runs_dir)["rows"]:
        if row.get("annotator") != annotator:
            continue
        run_id_a, run_id_b = row.get("run_id_a"), row.get("run_id_b")
        if isinstance(run_id_a, str) and isinstance(run_id_b, str):
            keys.add(pair_key(run_id_a, run_id_b))
    return keys


def next_unlabeled_pair(
    runs_dir: Path,
    ledger_rows: list[dict[str, Any]],
    annotator: str,
    mode: PairMode,
) -> dict[str, Any]:
    """The queue: first pair this annotator has not judged, plus progress."""
    pairs = enumerate_pairs(ledger_rows, mode)
    done = annotated_pair_keys(runs_dir, annotator)
    remaining = [
        (a, b) for a, b in pairs if pair_key(a["run_id"], b["run_id"]) not in done
    ]
    progress = {"labeled": len(pairs) - len(remaining), "available": len(pairs)}
    if not remaining:
        return {"pair": None, "progress": progress}
    entry_a, entry_b = remaining[0]
    return {"pair": {"a": entry_a, "b": entry_b}, "progress": progress}


# ─── writing (opt-in via --annotate only) ────────────────────────────────────


def append_annotation(
    runs_dir: Path,
    *,
    scenario_id: str,
    label_a: str,
    run_id_a: str,
    label_b: str,
    run_id_b: str,
    preferred: str | None,
    annotator: str,
) -> dict[str, Any]:
    """Append one judgment row. The only write the annotate surface performs."""
    record = {
        "ts": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scenario_id": scenario_id,
        "label_a": label_a,
        "run_id_a": run_id_a,
        "label_b": label_b,
        "run_id_b": run_id_b,
        "preferred": preferred,
        "annotator": annotator,
    }
    path = Path(runs_dir) / ANNOTATIONS_FILENAME
    try:
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            output.write("\n")
    except OSError as exc:
        print(f"wt serve: warning: could not write {path}: {exc}", file=sys.stderr)
        raise
    return record
