"""The experiment ladder: tiers, budgets, fingerprints, and the evidence gate.

See docs/design/0005-experiment-ladder.md. In short: a sweep's tier is derived
from what it runs (a replayed probe, one focused scenario, or a regression
across several), each tier has a wall-clock budget, every sweep into a runs
directory with history declares the question it answers and the verdict it
expects, and a regression sweep is refused while any selected scenario whose
most recent regression run failed has no passing focused run since, on the
current artifact.

There is deliberately no bypass. Guidance an agent can talk its way around is
guidance it will talk its way around; the only ways past the gate are to run
the focused sweeps or to leave the failing scenario out of the selection, and
the latter is recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Tier = Literal["probe", "focused", "regression"]
TIER_ORDER: tuple[Tier, ...] = ("probe", "focused", "regression")
EXPECT_CHOICES = ("pass", "fail")
PASSING_VERDICTS = frozenset({"PASS", "PASS_WITH_VARIANCE"})

DEFAULT_TIER_CAPS_S: dict[str, float | None] = {
    "probe": 300.0,
    "focused": 900.0,
    "regression": None,
}
LADDER_CONFIG_TABLE = ("tool", "windtunnel", "ladder")
_CAP_KEYS = {"probe": "probe_budget_s", "focused": "focused_budget_s"}
_GIT_TIMEOUT_S = 60.0
ALWAYS_EXCLUDED_PATTERNS = ("*.pyc", "*.pyo")
OUTPUTS_FILENAME = ".wt-outputs"
MANIFESTS_DIRNAME = ".fingerprints"
_MAX_LISTED_PATHS = 12


class LadderError(Exception):
    """A usage error the ladder reports before any runtime is touched (exit 2)."""


# ─── Tiers and budgets ────────────────────────────────────────────────────────


def derive_tier(*, from_trace: bool, selected_count: int) -> Tier:
    """Return the tier a sweep is, from what it runs — never from a flag."""
    if from_trace:
        return "probe"
    return "focused" if selected_count == 1 else "regression"


def tier_priority(tier: str | None) -> int:
    """Queue priority for the runtime lock: lower is served first."""
    if tier in TIER_ORDER:
        return TIER_ORDER.index(tier)
    return len(TIER_ORDER) - 1


def load_tier_caps(start: Path | None = None) -> dict[str, float | None]:
    """Read ``[tool.windtunnel.ladder]`` from the nearest pyproject.toml.

    Walks up from ``start`` (default: the working directory) to the first
    pyproject.toml. Missing file, missing table, or unreadable TOML all fall
    back to the defaults; a present but invalid value is a usage error, since
    silently ignoring a cap the operator set would be worse than refusing.
    """
    caps = dict(DEFAULT_TIER_CAPS_S)
    pyproject = _nearest_pyproject(start or Path.cwd())
    if pyproject is None:
        return caps
    try:
        import tomllib

        data: Any = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return caps
    for key in LADDER_CONFIG_TABLE:
        data = data.get(key) if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return caps
    for tier, key in _CAP_KEYS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise LadderError(
                f"{pyproject}: [tool.windtunnel.ladder] {key} must be a positive "
                f"number of seconds, got {value!r}"
            )
        caps[tier] = float(value)
    return caps


def _nearest_pyproject(start: Path) -> Path | None:
    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def resolve_budget(
    tier: Tier, requested: float | None, caps: dict[str, float | None]
) -> float | None:
    """Return the sweep's budget: the request, the cap, or None (unbounded).

    A request may lower a tier's cap but never raise it.
    """
    cap = caps.get(tier)
    if requested is None:
        return cap
    if requested <= 0:
        raise LadderError("--budget must be a positive number of seconds")
    if cap is not None and requested > cap:
        raise LadderError(
            f"--budget {requested:g}s exceeds the {tier} tier cap of {cap:g}s. "
            f"A {tier} sweep is meant to answer one question quickly; if it cannot, "
            "make the experiment smaller rather than the budget larger. (Caps are "
            "set in [tool.windtunnel.ladder] of pyproject.toml.)"
        )
    return requested


class Budget:
    """Wall-clock budget for one sweep, started when the runtime is acquired."""

    def __init__(self, seconds: float | None, *, clock: Any = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._started: float | None = None
        self.exhausted = False

    def start(self) -> None:
        if self._started is None:
            self._started = self._clock()

    def elapsed(self) -> float:
        return 0.0 if self._started is None else self._clock() - self._started

    def allows_start(self) -> bool:
        """True while another run may start; latches exhausted once spent."""
        if self.seconds is None or self._started is None:
            return True
        if self.elapsed() >= self.seconds:
            self.exhausted = True
        return not self.exhausted


# ─── Artifact fingerprint ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fingerprint:
    """What the sweep ran against; evidence is only valid for the same value."""

    value: str
    parts: dict[str, str | None]
    notes: tuple[str, ...] = ()
    # path -> blob id of the git snapshot, for explaining WHICH files changed.
    manifest: dict[str, str] | None = field(default=None, compare=False)


def compute_fingerprint(
    *,
    runtime_name: str,
    soul_path: Path | None,
    agents_path: Path | None,
    exclude: Sequence[Path] = (),
    target: str | None = None,
    plugin: object | None = None,
    wt_version: str | None = None,
    cwd: Path | None = None,
) -> Fingerprint:
    """Fingerprint the artifact under test (see the design doc for the parts).

    ``exclude`` lists paths Wind Tunnel itself writes during a sweep (the runs
    directory, a --out report): they change every sweep and are not part of
    the artifact. ``target`` is the runtime's lock key — the backend a runtime
    name resolves to, e.g. http_inject's endpoint URL — so evidence from one
    endpoint never counts for another.
    """
    notes: list[str] = []
    git_tree, git_error, manifest = _worktree_snapshot(cwd or Path.cwd(), exclude=exclude)
    if git_error is not None:
        notes.append(
            f"could not snapshot the git working tree ({git_error}); the fingerprint "
            "falls back to the runtime, --soul/--agents, and plugin parts, so code "
            "changes will not invalidate earlier evidence until this is fixed"
        )
    elif git_tree is None:
        notes.append(
            "not in a git repository: the artifact fingerprint covers only the runtime, "
            "--soul/--agents, and the runtime plugin's own fingerprint, so code changes "
            "will not invalidate earlier evidence"
        )
    plugin_part: str | None = None
    fingerprint_fn = getattr(plugin, "artifact_fingerprint", None)
    if callable(fingerprint_fn):
        try:
            raw = fingerprint_fn(runtime_name)
        except Exception as exc:  # noqa: BLE001 - a broken optional hook must not stop the sweep
            notes.append(f"runtime plugin artifact_fingerprint() raised {type(exc).__name__}")
        else:
            plugin_part = None if raw is None else str(raw)
    parts: dict[str, str | None] = {
        "git_tree": git_tree,
        "runtime": runtime_name,
        "target": target if target is not None else runtime_name,
        "soul": _file_hash(soul_path),
        "agents": _file_hash(agents_path),
        "plugin": plugin_part,
        "wt_version": wt_version,
    }
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Fingerprint(
        value=f"sha256:{digest}", parts=parts, notes=tuple(notes), manifest=manifest
    )


def _file_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_worktree_tree(cwd: Path, *, exclude: Iterable[Path] = ()) -> str | None:
    """Return the tree id of the working tree, uncommitted changes included.

    None outside a repository or when the snapshot fails; see
    _worktree_snapshot for how the snapshot is taken.
    """
    tree, _error, _manifest = _worktree_snapshot(cwd, exclude=exclude)
    return tree


def _worktree_snapshot(
    cwd: Path, *, exclude: Iterable[Path] = ()
) -> tuple[str | None, str | None, dict[str, str] | None]:
    """Return ``(tree_id, error, manifest)`` for the working tree at ``cwd``.

    Stages everything (tracked edits and untracked, non-ignored files) into a
    COPY of the repository's index and writes it as a tree, the way
    ``git stash create`` snapshots a dirty tree, without side effects on the
    repository: the real index and working tree are never touched, and the
    blobs git hashes along the way go to a scratch object directory (the real
    one is only read, as an alternate). Git LFS's clean filter is disabled for
    the snapshot so it does not write into ``.git/lfs``; a changed LFS file is
    hashed by its content instead.

    Paths under ``exclude`` that lie inside the repository are dropped from
    the snapshot after staging. That order matters: excluding a gitignored
    directory with a pathspec makes ``git add`` fail outright.

    Compiled Python (``*.pyc``, ``*.pyo``) is always excluded: importing the
    agent under test writes it, which would otherwise make every run look like
    an edit when a repository does not ignore it.

    ``manifest`` maps each snapshotted path to its blob id, so a changed
    fingerprint can be explained file by file.

    ``(None, None, None)`` means "not a repository" (or no git on PATH);
    ``(None, reason, None)`` means a repository whose snapshot failed.
    """
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return None, None, None
    top_path = Path(top)
    index = _git(cwd, "rev-parse", "--git-path", "index")
    objects = _git(cwd, "rev-parse", "--git-path", "objects")
    if index is None or objects is None:
        return None, "git rev-parse failed", None
    index_path = Path(index) if Path(index).is_absolute() else (cwd / index).resolve()
    objects_path = Path(objects) if Path(objects).is_absolute() else (cwd / objects).resolve()

    excluded: list[str] = list(ALWAYS_EXCLUDED_PATTERNS)
    for path in exclude:
        try:
            relative = Path(path).resolve().relative_to(top_path.resolve())
        except ValueError:
            continue
        if str(relative) not in ("", "."):
            excluded.append(relative.as_posix())

    with tempfile.TemporaryDirectory(prefix="wt-fingerprint-") as scratch:
        temp_index = Path(scratch) / "index"
        temp_objects = Path(scratch) / "objects"
        temp_objects.mkdir()
        if index_path.is_file():
            # copy2 keeps the index's mtime, which git's racy-clean check needs:
            # with a fresh mtime, a same-size edit made in the same second as
            # the last index write would look unchanged and not be re-hashed.
            shutil.copy2(index_path, temp_index)
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(temp_index),
            "GIT_OBJECT_DIRECTORY": str(temp_objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects_path),
        }
        no_lfs = (
            "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=",
            "-c", "filter.lfs.required=false",
        )
        if _git(top_path, *no_lfs, "add", "-A", "--", ".", env=env) is None:
            return None, "git add failed or timed out", None
        if _git(
            top_path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *excluded,
            env=env,
        ) is None:
            return None, "git rm --cached failed", None
        tree = _git(top_path, "write-tree", env=env)
        if not tree:
            return None, "git write-tree failed", None
        listing = _git(top_path, "ls-files", "-s", "-z", env=env, strip=False) or ""
        manifest: dict[str, str] = {}
        for entry in listing.split("\0"):
            meta, _tab, path_text = entry.partition("\t")
            fields_ = meta.split()
            if path_text and len(fields_) >= 2:
                manifest[path_text] = fields_[1]
        return tree, None, manifest


def _git(
    cwd: Path, *args: str, env: dict[str, str] | None = None, strip: bool = True
) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if strip else result.stdout


# ─── What wt writes, and explaining a changed fingerprint ─────────────────────


def record_output(runs_dir: Path, path: Path) -> None:
    """Remember a file wt wrote (a --out report) so fingerprints exclude it.

    Every later sweep into ``runs_dir`` excludes every path recorded here, not
    just its own --out: a report left in the tree by an earlier command must
    not make a focused pass look stale to a regression that writes none.
    """
    resolved = str(Path(path).resolve())
    if resolved in {str(item) for item in recorded_outputs(runs_dir)}:
        return
    try:
        Path(runs_dir).mkdir(parents=True, exist_ok=True)
        with (Path(runs_dir) / OUTPUTS_FILENAME).open("a", encoding="utf-8") as registry:
            registry.write(resolved + "\n")
    except OSError:
        pass  # a missed registration costs a spurious stale verdict, not a wrong one


def recorded_outputs(runs_dir: Path) -> list[Path]:
    try:
        text = (Path(runs_dir) / OUTPUTS_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return []
    return [Path(line) for line in text.splitlines() if line.strip()]


def save_manifest(runs_dir: Path, fingerprint: Fingerprint) -> None:
    """Keep the snapshot's per-file listing so later refusals can name files."""
    tree = fingerprint.parts.get("git_tree")
    if not tree or fingerprint.manifest is None:
        return
    path = Path(runs_dir) / MANIFESTS_DIRNAME / f"{tree}.manifest"
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fingerprint.manifest, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _load_manifest(runs_dir: Path, tree: str | None) -> dict[str, str] | None:
    if not tree:
        return None
    try:
        data = json.loads(
            (Path(runs_dir) / MANIFESTS_DIRNAME / f"{tree}.manifest").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe_changes(
    old_parts: dict[str, Any],
    old_manifest: dict[str, str] | None,
    new: Fingerprint,
) -> str:
    """Name what differs between an earlier fingerprint and ``new``."""
    changed = [
        name for name in ("target", "soul", "agents", "plugin", "wt_version")
        if old_parts.get(name) != new.parts.get(name)
    ]
    pieces: list[str] = []
    if changed:
        pieces.append("changed: " + ", ".join(changed))
    if old_parts.get("git_tree") != new.parts.get("git_tree"):
        if old_manifest is not None and new.manifest is not None:
            paths = sorted(
                path for path in set(old_manifest) | set(new.manifest)
                if old_manifest.get(path) != new.manifest.get(path)
            )
            shown = ", ".join(paths[:_MAX_LISTED_PATHS])
            more = len(paths) - _MAX_LISTED_PATHS
            pieces.append(
                "files: " + shown + (f" (+{more} more)" if more > 0 else "")
                if paths else "the working tree"
            )
        else:
            pieces.append("the working tree")
    return "; ".join(pieces) if pieces else "nothing recorded"


def normalize_target(plugin: object, lock_key: str | None, runtime_name: str) -> str:
    """Identify the backend under test independent of how it was spelled.

    The plugin's class (an entry-point name and a ``module:Class`` path to the
    same plugin agree), plus its lock key when it has one, with a URL's scheme
    and host lower-cased, loopback spellings unified, and a trailing slash
    dropped. Without a plugin class, the runtime name as given.
    """
    cls = plugin if isinstance(plugin, type) else type(plugin)
    identity = (
        runtime_name if cls is object else f"{cls.__module__}.{cls.__qualname__}"
    )
    if lock_key is None or lock_key == runtime_name:
        return identity
    return f"{identity}|{_normalize_url(lock_key)}"


def _normalize_url(raw: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return raw.strip()
    if not parts.scheme or not parts.netloc:
        return raw.strip()
    host = (parts.hostname or "").lower()
    if host in ("localhost", "::1", "0.0.0.0"):
        host = "127.0.0.1"
    port = parts.port
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    netloc = host if port in (None, default) else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), parts.query, ""))


# ─── Ledger reading and the evidence gate ─────────────────────────────────────


def read_ledger(runs_dir: Path) -> list[dict[str, Any]]:
    """Return every parseable ledger row, in file order."""
    path = Path(runs_dir) / "ledger.ndjsonl"
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _experiment(row: dict[str, Any]) -> dict[str, Any]:
    experiment = row.get("experiment")
    return experiment if isinstance(experiment, dict) else {}


@dataclass
class GateDecision:
    """Outcome of the regression evidence gate for one prospective sweep.

    ``missing`` and ``satisfied`` hold scenario names; ``gated_sweeps`` names
    the regression sweeps whose failures were checked.
    """

    gated_sweeps: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # For a missing scenario: its latest passing focused row since the failure,
    # on some OTHER artifact — the pass that went stale.
    stale_passes: dict[str, dict[str, Any]] = field(default_factory=dict)
    satisfied: list[str] = field(default_factory=list)
    excluded_failing: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.missing


def _row_failed(row: dict[str, Any]) -> bool:
    """Whether a ledger row is a failure the gate cares about.

    Prefers the sweep's own judgement (``experiment.counts_as_failure``, the
    same rule that sets `wt run`'s exit code, so a transport-only verdict is not
    one); falls back to the verdict for rows written without it.
    """
    counted = _experiment(row).get("counts_as_failure")
    if isinstance(counted, bool):
        return counted
    return row.get("verdict") not in PASSING_VERDICTS


def check_regression_gate(
    rows: Sequence[dict[str, Any]],
    *,
    selected: Sequence[tuple[str | None, str]],
    fingerprint: str,
    target: str,
) -> GateDecision:
    """Decide whether a regression sweep over ``selected`` may run now.

    ``selected`` holds ``(pack, scenario)`` pairs. The gate works per
    scenario, in ledger (append) order, among rows against the same runtime
    ``target``: a selected scenario whose most recent regression row failed
    needs a passing focused row written AFTER it, on ``fingerprint``, from a
    sweep that finished within its budget. Working per scenario means a
    failure is not forgotten because a later regression left it out of the
    selection or ran out of budget before reaching it.
    """
    def key(row: dict[str, Any]) -> tuple[str | None, str]:
        return (row.get("pack"), str(row.get("scenario_id")))

    def same_target(row: dict[str, Any]) -> bool:
        parts = _experiment(row).get("fingerprint_parts")
        if not isinstance(parts, dict):
            return False
        recorded = parts.get("target", parts.get("runtime"))
        return recorded == target

    last_regression: dict[tuple[str | None, str], int] = {}
    for position, row in enumerate(rows):
        if _experiment(row).get("tier") == "regression" and same_target(row):
            last_regression[key(row)] = position

    decision = GateDecision()
    selected_set = set(selected)
    selected_packs = {pack for pack, _name in selected}
    failing = sorted(
        (item for item, position in last_regression.items() if _row_failed(rows[position])),
        key=lambda item: (item[1], item[0] or ""),
    )
    gated: list[str] = []
    for item in failing:
        position = last_regression[item]
        sweep = rows[position].get("sweep_id")
        if sweep and sweep not in gated:
            gated.append(str(sweep))
        if item not in selected_set:
            # Only report what this selection plausibly meant to cover: a
            # failure from a pack the sweep does not touch is not "excluded".
            if item[0] in selected_packs:
                decision.excluded_failing.append(item[1])
            continue
        passes = [
            row for row in rows[position + 1:]
            if key(row) == item
            and _experiment(row).get("tier") == "focused"
            and not _experiment(row).get("budget_exhausted")
            and not _row_failed(row)
        ]
        if any(_experiment(row).get("fingerprint") == fingerprint for row in passes):
            decision.satisfied.append(item[1])
        else:
            decision.missing.append(item[1])
            if passes:
                decision.stale_passes[item[1]] = passes[-1]
    decision.gated_sweeps = gated
    return decision


def gate_refusal_message(
    decision: GateDecision,
    fingerprint: Fingerprint,
    *,
    context_flags: Sequence[str] = (),
    runs_dir: Path | None = None,
) -> str:
    """The exit-2 explanation, written for an agent that will act on it.

    ``context_flags`` are the flags that decide WHAT is under test (runtime,
    runs directory, pack sources, --soul/--agents). They are repeated in the
    suggested commands so a copied command produces evidence for this very
    artifact instead of a different fingerprint.
    """
    names = decision.missing
    context = "".join(f" {shlex.quote(flag)}" for flag in context_flags)
    lines = [
        f"wt run: refusing a regression sweep: {len(names)} scenario(s) failed in their "
        f"most recent regression run and have no passing focused run since, on the "
        f"current artifact ({fingerprint.value[:19]}…):",
        *(_missing_line(name, decision, fingerprint, runs_dir) for name in names),
        "Run each one on its own first; a focused run answers \"did my change fix "
        "this?\" in minutes, a regression answers \"does everything still work?\" "
        "once the fixes have earned it:",
        *(
            f"  wt run --scenario {shlex.quote(name)}{context} --runs 3 --question "
            f"\"<what you expect to learn>\" --expect pass"
            for name in names
        ),
        "Any change to the working tree, --soul/--agents, or the runtime after a "
        "focused pass makes that pass stale. If a file named above is something "
        "the runtime or your tooling writes (a log, a cache), gitignore it; it is "
        "not part of the artifact. To deliberately leave a failing scenario out "
        "of this regression, drop it from the selection; the sweep records it as "
        "excluded.",
    ]
    return "\n".join(lines)


def _missing_line(
    name: str, decision: GateDecision, fingerprint: Fingerprint, runs_dir: Path | None
) -> str:
    stale = decision.stale_passes.get(name)
    if stale is None:
        return f"  {name}"
    parts = _experiment(stale).get("fingerprint_parts") or {}
    old_manifest = (
        _load_manifest(runs_dir, parts.get("git_tree")) if runs_dir is not None else None
    )
    if _experiment(stale).get("fingerprint") == "changed-during-sweep":
        return f"  {name} (its focused pass ran while the artifact was changing)"
    return (
        f"  {name} (its focused pass is stale; since then "
        f"{describe_changes(parts, old_manifest, fingerprint)})"
    )


def ledger_has_history(rows: Sequence[dict[str, Any]]) -> bool:
    """True once a runs directory holds any sweep: iteration has begun."""
    return bool(rows)


def prediction_line(expect: str | None, passed: bool) -> str | None:
    """One line comparing the declared expectation with the sweep's result."""
    if expect is None:
        return None
    actual = "pass" if passed else "fail"
    verdict = "held" if expect == actual else "MISSED"
    return f"wt run: prediction {verdict}: expected {expect}, got {actual}"


def experiment_record(
    *,
    tier: Tier,
    question: str | None,
    expect: str | None,
    budget: Budget,
    fingerprint: Fingerprint,
    source_trace: str | None = None,
    from_turn: int | None = None,
    decision: GateDecision | None = None,
    counts_as_failure: bool | None = None,
) -> dict[str, Any]:
    """The ``experiment`` object written into each of the sweep's ledger rows."""
    return {
        "counts_as_failure": counts_as_failure,
        "tier": tier,
        "question": question,
        "expect": expect,
        "budget_s": budget.seconds,
        "budget_exhausted": budget.exhausted,
        "fingerprint": fingerprint.value,
        "fingerprint_parts": dict(fingerprint.parts),
        "source_trace": source_trace,
        "from_turn": from_turn,
        "evidence_for": (
            list(decision.gated_sweeps) if decision is not None and decision.satisfied else []
        ),
        "excluded_failing": list(decision.excluded_failing) if decision else [],
    }
