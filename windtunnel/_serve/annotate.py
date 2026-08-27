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

# Verdict filters for the labeling queue. Human annotation complements the
# verifier, it never repeats it: a PASS-vs-FAIL pair is already ranked by
# scoring, so the queue defaults to pairs the verifier cannot separate.
#
#   both-pass: both runs' ledger verdicts are PASS. PASS_WITH_VARIANCE is
#       non-passing here, per the harness's own fail-closed law.
#   tie-break: scoring is fully indifferent — equal verdict within the
#       passing family (PASS or PASS_WITH_VARIANCE), equal pass_rate, and
#       equal failure_risk (row aggregates; exact equality — the values
#       come from the same arithmetic). Two PASS runs always tie (1.0 /
#       0.0); two equal-rate PASS_WITH_VARIANCE arms tie; PASS never ties
#       with PASS_WITH_VARIANCE. These are the pairs where a human
#       judgment adds maximal information.
#   all: no verdict filtering (kept for other annotation uses).
VerdictFilter = str
VERDICT_FILTERS = ("both-pass", "tie-break", "all")
DEFAULT_VERDICT_FILTER = "both-pass"
_PASSING_FAMILY = ("PASS", "PASS_WITH_VARIANCE")


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
                    # Row aggregates, carried for the queue's verdict filters.
                    "pass_rate": row.get("pass_rate"),
                    "failure_risk": row.get("failure_risk"),
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


def _passes_verdict_filter(
    entry_a: dict[str, Any], entry_b: dict[str, Any], verdict_filter: VerdictFilter
) -> bool:
    """Apply one queue verdict filter to a candidate pair. See VERDICT_FILTERS."""
    if verdict_filter == "all":
        return True
    if verdict_filter == "both-pass":
        return bool(entry_a["verdict"] == "PASS" and entry_b["verdict"] == "PASS")
    # tie-break: scoring fully indifferent between the two runs.
    return bool(
        entry_a["verdict"] in _PASSING_FAMILY
        and entry_a["verdict"] == entry_b["verdict"]
        and entry_a["pass_rate"] == entry_b["pass_rate"]
        and entry_a["failure_risk"] == entry_b["failure_risk"]
    )


def enumerate_pairs(
    ledger_rows: list[dict[str, Any]],
    mode: PairMode,
    verdict_filter: VerdictFilter = "all",
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Comparable sibling pairs, deterministic order.

    within: same scenario_id AND same label (sampled completions of one arm)
    cross:  same scenario_id, different labels (arm-vs-arm comparison)
    both:   the union

    verdict_filter narrows by ledger verdicts (see VERDICT_FILTERS) so the
    labeling queue can offer only pairs the verifier is indifferent
    between — human annotation complements scoring, it never repeats it.
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
            if not _passes_verdict_filter(entry_a, entry_b, verdict_filter):
                continue
            pairs.append((entry_a, entry_b))
    return pairs


def pair_key(run_id_a: str, run_id_b: str) -> tuple[str, str]:
    """Order-independent identity of a compared pair."""
    return (run_id_a, run_id_b) if run_id_a <= run_id_b else (run_id_b, run_id_a)


def annotator_history(
    runs_dir: Path, annotator: str
) -> tuple[set[tuple[str, str]], dict[str, int]]:
    """One annotator's record: pairs judged, and how often each run was seen.

    EVERY recorded judgment counts, `"a"` and `"b"` and the no-preference
    `null` alike. "These two are indistinguishable" is an answer, not the
    absence of one, so a null retires its pair from that annotator's queue
    exactly like a decisive verdict — asking again would only collect the
    same shrug twice.

    Read fresh on every call: the store is an append-only file that the
    annotator is writing to as they label, and a cached view of it would
    re-offer whatever it had not noticed yet.
    """
    keys: set[tuple[str, str]] = set()
    exposure: dict[str, int] = {}
    for row in read_annotations(runs_dir)["rows"]:
        if row.get("annotator") != annotator:
            continue
        run_id_a, run_id_b = row.get("run_id_a"), row.get("run_id_b")
        if not (isinstance(run_id_a, str) and isinstance(run_id_b, str)):
            continue
        keys.add(pair_key(run_id_a, run_id_b))
        for run_id in (run_id_a, run_id_b):
            exposure[run_id] = exposure.get(run_id, 0) + 1
    return keys, exposure


def annotated_pair_keys(runs_dir: Path, annotator: str) -> set[tuple[str, str]]:
    """Pairs this annotator has already judged (order-independent)."""
    return annotator_history(runs_dir, annotator)[0]


def next_unlabeled_pair(
    runs_dir: Path,
    ledger_rows: list[dict[str, Any]],
    annotator: str,
    mode: PairMode,
    verdict_filter: VerdictFilter = DEFAULT_VERDICT_FILTER,
) -> dict[str, Any]:
    """The queue: the least-seen pair this annotator has not judged.

    Selection is coverage-first, not enumeration-first. `enumerate_pairs`
    walks `combinations` index order, which is depth-first: every partner
    of the first run before the second run is ever offered, and one whole
    scenario before the next. Served straight down that order the queue
    pins one run on the A side for as many rounds as it has siblings —
    and because sampled completions of one passing scenario render alike,
    that reads as the SAME comparison handed back over and over. The
    annotator shrugs at it repeatedly instead of labeling anything new.

    So candidates are ordered by how often this annotator has already seen
    each of the two runs; the enumeration index breaks ties, keeping the
    choice deterministic. Every judgment moves the queue to runs it has
    shown least, which spreads offers across arms and scenarios and covers
    every run once before asking about any run twice.

    Progress counts reflect the active mode AND verdict filter.
    """
    pairs = enumerate_pairs(ledger_rows, mode, verdict_filter)
    done, exposure = annotator_history(runs_dir, annotator)
    remaining = [
        (index, entry_a, entry_b)
        for index, (entry_a, entry_b) in enumerate(pairs)
        if pair_key(entry_a["run_id"], entry_b["run_id"]) not in done
    ]
    progress = {
        "labeled": len(pairs) - len(remaining),
        "available": len(pairs),
        "remaining": len(remaining),
    }
    if not remaining:
        return {"pair": None, "progress": progress}
    _, entry_a, entry_b = min(
        remaining,
        key=lambda candidate: (
            exposure.get(candidate[1]["run_id"], 0)
            + exposure.get(candidate[2]["run_id"], 0),
            candidate[0],
        ),
    )
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
