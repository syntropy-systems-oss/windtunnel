"""The experiment ladder: tiers, budgets, fingerprints, and the evidence gate.

See docs/design/0005-experiment-ladder.md. In short: a sweep's tier is derived
from what it runs (a replayed probe, one focused scenario, or a regression
across several), each tier has a wall-clock budget, every sweep into a runs
directory with history declares the question it answers and the verdict it
expects, and a regression sweep must be earned: by a passing focused run on the
current artifact since the last regression, and, for every selected scenario
whose most recent regression run failed, by a passing focused run of that
scenario.

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
    pyproject, data = _ladder_table(start)
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


def load_model_policy(start: Path | None = None) -> dict[str, list[str]]:
    """Read ``[tool.windtunnel.ladder.models]``: which model labels each tier may use.

    A tier that is not listed may use any model. Labels are opaque strings
    compared exactly, as the runtime reports them (see ``model_label``).
    """
    pyproject, data = _ladder_table(start)
    table = data.get("models", {})
    if not isinstance(table, dict):
        raise LadderError(f"{pyproject}: [tool.windtunnel.ladder] models must be a table")
    policy: dict[str, list[str]] = {}
    for tier, labels in table.items():
        if tier not in TIER_ORDER:
            raise LadderError(
                f"{pyproject}: [tool.windtunnel.ladder.models] {tier!r} is not a tier "
                f"({', '.join(TIER_ORDER)})"
            )
        if (
            not isinstance(labels, list) or not labels
            or not all(isinstance(label, str) and label for label in labels)
        ):
            raise LadderError(
                f"{pyproject}: [tool.windtunnel.ladder.models] {tier} must be a "
                f"non-empty list of model labels, got {labels!r}"
            )
        policy[tier] = list(labels)
    return policy


def load_pricing(start: Path | None = None) -> dict[str, Any] | None:
    """Read ``[tool.windtunnel.ladder.pricing]``: $/hour and $/M tokens per model label.

    None when the table is absent (the default): no ``cost_usd`` is computed
    anywhere, and rates are never built in. A model label with no entry of
    its own falls back to ``models.default`` when the table gives one.
    Per label, ``cache_read_per_m`` is optional and falls back to that
    label's own ``input_per_m`` when unset (a runtime that never reports a
    cache split then still prices correctly — every input token at the one
    rate). ``time_per_hour`` is also optional per label and overrides the
    top-level ``time_per_hour`` for sweeps on that model (different compute
    lanes cost different amounts per hour); a label without one falls back
    to the global rate.
    """
    pyproject, data = _ladder_table(start)
    table = data.get("pricing")
    if table is None:
        return None
    where = "[tool.windtunnel.ladder.pricing]"
    if not isinstance(table, dict):
        raise LadderError(f"{pyproject}: [tool.windtunnel.ladder] pricing must be a table")
    time_per_hour = _non_negative(pyproject, where, "time_per_hour", table.get("time_per_hour", 0.0))
    models_table = table.get("models", {})
    if not isinstance(models_table, dict):
        raise LadderError(f"{pyproject}: {where} models must be a table")
    models: dict[str, dict[str, float]] = {}
    for label, rates in models_table.items():
        rates_where = f"{where}.models {label!r}"
        if not isinstance(rates, dict):
            raise LadderError(
                f"{pyproject}: {rates_where} must be a table with input_per_m and output_per_m"
            )
        entry = {
            "input_per_m": _non_negative(pyproject, rates_where, "input_per_m", rates.get("input_per_m")),
            "output_per_m": _non_negative(
                pyproject, rates_where, "output_per_m", rates.get("output_per_m")
            ),
        }
        if "cache_read_per_m" in rates:
            entry["cache_read_per_m"] = _non_negative(
                pyproject, rates_where, "cache_read_per_m", rates.get("cache_read_per_m")
            )
        if "time_per_hour" in rates:
            entry["time_per_hour"] = _non_negative(
                pyproject, rates_where, "time_per_hour", rates.get("time_per_hour")
            )
        models[label] = entry
    return {"time_per_hour": time_per_hour, "models": models}


def _non_negative(pyproject: Path | None, where: str, key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise LadderError(f"{pyproject}: {where} {key} must be a non-negative number, got {value!r}")
    return float(value)


def compute_cost_usd(
    *,
    wall_s: float,
    input_tokens: int | None,
    cached_tokens: int | None,
    output_tokens: int | None,
    model: str | None,
    pricing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """$ cost of one sweep or ledger row, from its wall time and token usage.

    None when pricing is not configured. ``input_tokens`` is the TOTAL
    prompt tokens, including any ``cached_tokens`` (see
    ``Trace.model_calls``) — never just the uncached remainder.

    ``tokens_known`` is False (every token component null, ``total`` =
    ``time``) when usage was not reported, or the model has no price and
    no ``[pricing.models.default]`` fallback — never a guess.

    When tokens are known but ``cached_tokens`` is None (the runtime
    didn't report a per-call cache split), every input token is priced at
    the uncached ``input_per_m`` rate and ``cache_split_known`` is False —
    ``cache_read`` stays null rather than guessing the split.

    The time cost uses the model label's own ``time_per_hour`` when its
    pricing entry sets one, else the top-level rate — a cheap GPU lane and
    an expensive shared lane are not priced the same per hour.
    """
    if pricing is None:
        return None
    rates = pricing["models"].get(model) if model is not None else None
    if rates is None:
        rates = pricing["models"].get("default")
    time_per_hour = rates.get("time_per_hour", pricing["time_per_hour"]) if rates else pricing["time_per_hour"]
    time_cost = round(wall_s / 3600.0 * time_per_hour, 6)
    if rates is None or not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return {
            "uncached_input": None,
            "cache_read": None,
            "output": None,
            "time": time_cost,
            "total": time_cost,
            "tokens_known": False,
            "cache_split_known": False,
        }
    cache_split_known = isinstance(cached_tokens, int)
    output_cost = round(output_tokens * rates["output_per_m"] / 1e6, 6)
    if cache_split_known:
        assert isinstance(cached_tokens, int)
        cache_read_per_m = rates.get("cache_read_per_m", rates["input_per_m"])
        uncached_input_cost = round((input_tokens - cached_tokens) * rates["input_per_m"] / 1e6, 6)
        cache_read_cost = round(cached_tokens * cache_read_per_m / 1e6, 6)
    else:
        uncached_input_cost = round(input_tokens * rates["input_per_m"] / 1e6, 6)
        cache_read_cost = None
    total = round(time_cost + uncached_input_cost + (cache_read_cost or 0.0) + output_cost, 6)
    return {
        "uncached_input": uncached_input_cost,
        "cache_read": cache_read_cost,
        "output": output_cost,
        "time": time_cost,
        "total": total,
        "tokens_known": True,
        "cache_split_known": cache_split_known,
    }


def cost_block(
    question: str | None,
    cost: dict[str, Any],
    cost_usd: dict[str, Any] | None,
    cache_check: dict[str, Any] | None = None,
) -> str:
    """The end-of-sweep cost block `wt run` prints.

    tokens in (cached)/out, wall time, and — when priced — $ uncached input
    + cache read + output + time = total. When ``cache_check`` is given
    (the prompt-cache-miss check ran; see ``check_cache_misses``), also
    prints its pass/fail/unknown verdict and one compact ``call k: cached
    M/P`` line per checked call.
    """
    tokens_in, tokens_out = cost.get("input_tokens"), cost.get("output_tokens")
    cached = cost.get("cached_tokens")
    if type(tokens_in) is int and type(tokens_out) is int:
        cache_part = f"{cached} cached" if type(cached) is int else "cache split unknown"
        tokens = f"{tokens_in} in ({cache_part}) / {tokens_out} out"
    else:
        tokens = "unknown"
    lines = [
        f"wt run: cost — {question!r}" if question else "wt run: cost",
        f"wt run:   tokens: {tokens}",
        f"wt run:   wall: {float(cost.get('wall_s') or 0.0):.1f}s",
    ]
    if cost_usd is not None:
        if not cost_usd["tokens_known"]:
            lines.append(
                f"wt run:   $ tokens unknown — time cost only: total {cost_usd['total']:.4f}"
            )
        elif cost_usd["cache_split_known"]:
            lines.append(
                f"wt run:   $ uncached {cost_usd['uncached_input']:.4f} + "
                f"cache read {cost_usd['cache_read']:.4f} + output {cost_usd['output']:.4f} + "
                f"time {cost_usd['time']:.4f} = total {cost_usd['total']:.4f}"
            )
        else:
            lines.append(
                f"wt run:   $ uncached {cost_usd['uncached_input']:.4f} (cache split unknown) + "
                f"output {cost_usd['output']:.4f} + time {cost_usd['time']:.4f} "
                f"= total {cost_usd['total']:.4f}"
            )
    if cache_check is not None:
        reason = f" — {cache_check['reason']}" if cache_check.get("reason") else ""
        lines.append(f"wt run:   cache check: {cache_check['result']}{reason}")
        lines.extend(f"wt run:   {line}" for line in cache_check.get("calls", []))
    return "\n".join(lines)


def check_model_policy(tier: Tier, label: str | None, policy: dict[str, list[str]]) -> None:
    """Refuse a sweep whose model the tier's policy does not allow."""
    allowed = policy.get(tier)
    if allowed is None or label in allowed:
        return
    if label is None:
        raise LadderError(
            f"a {tier} sweep must use one of: {', '.join(allowed)} "
            "([tool.windtunnel.ladder.models]), but this runtime does not report "
            "which model it uses (a runtime plugin reports it with model_label())"
        )
    raise LadderError(
        f"a {tier} sweep must use one of: {', '.join(allowed)} "
        f"([tool.windtunnel.ladder.models]); this runtime uses {label!r}"
    )


def model_label(plugin: object, runtime_name: str) -> str | None:
    """The model the runtime will answer with, from its plugin's optional hook.

    ``model_label(runtime_name) -> str | None`` on the runtime plugin. A
    missing hook, a None, or a hook that raises all mean "not reported":
    the label is never inferred from anything else.
    """
    hook = getattr(plugin, "model_label", None)
    if not callable(hook):
        return None
    try:
        label = hook(runtime_name)
    except Exception:  # noqa: BLE001 - a broken optional hook reports nothing
        return None
    return str(label) if label else None


# ─── Prompt-cache-miss check ────────────────────────────────────────────────


def load_cache_config(start: Path | None = None) -> dict[str, Any] | None:
    """Read ``[tool.windtunnel.cache]``: the prompt-cache-miss check.

    None when the table is absent (the default) — off: no check runs, and
    no ``cache_check`` field is added to any record.

    fail_on_miss (bool, default False): whether a miss (or an unknown
    split) makes the sweep's ledger row count as a failure.
    min_cached_ratio (number in [0, 1], default 0.5): every model call
    after the first in a conversation must report
    cached_tokens / prompt_tokens at or above this, or it is a miss.
    """
    pyproject, windtunnel = _windtunnel_table(start)
    table = windtunnel.get("cache")
    if table is None:
        return None
    where = "[tool.windtunnel.cache]"
    if not isinstance(table, dict):
        raise LadderError(f"{pyproject}: {where} must be a table")
    fail_on_miss = table.get("fail_on_miss", False)
    if not isinstance(fail_on_miss, bool):
        raise LadderError(f"{pyproject}: {where} fail_on_miss must be a boolean")
    ratio = table.get("min_cached_ratio", 0.5)
    if isinstance(ratio, bool) or not isinstance(ratio, int | float) or not 0 <= ratio <= 1:
        raise LadderError(
            f"{pyproject}: {where} min_cached_ratio must be a number between 0 and 1, "
            f"got {ratio!r}"
        )
    return {"fail_on_miss": fail_on_miss, "min_cached_ratio": float(ratio)}


def call_usage_totals(model_calls: Sequence[dict[str, Any]] | None) -> dict[str, int | None]:
    """Sum ``model_calls`` (see ``Trace.model_calls``) into one run's token totals.

    {"input_tokens" (total prompt tokens, including cached), "cached_tokens",
    "output_tokens"}. Each goes None, independently, as soon as any call is
    missing that one field — a run whose totals are known but whose cache
    split is unreported on one call still reports the totals; only
    ``cached_tokens`` goes None. None (every field None) when there are no
    calls to sum.
    """
    if not model_calls:
        return {"input_tokens": None, "cached_tokens": None, "output_tokens": None}
    call_keys = {
        "input_tokens": "prompt_tokens",
        "cached_tokens": "cached_tokens",
        "output_tokens": "completion_tokens",
    }
    totals: dict[str, int | None] = dict.fromkeys(call_keys, 0)
    for call in model_calls:
        for total_key, call_key in call_keys.items():
            value = call.get(call_key) if isinstance(call, dict) else None
            current = totals[total_key]
            totals[total_key] = (
                current + value if current is not None and type(value) is int else None
            )
    return totals


def check_cache_misses(
    model_calls: Sequence[dict[str, Any]] | None, *, min_cached_ratio: float
) -> dict[str, Any]:
    """Evaluate the prompt-cache-miss check over one or more conversations.

    ``model_calls`` may pool several runs' (or several scenarios') calls —
    grouped here by each call's ``conversation`` field (Trace.model_calls),
    in list order, so "the first call in a conversation" and "call k"
    are always relative to one run's own turn sequence, however many
    conversations are pooled. Every call after a conversation's first is
    checked: cached_tokens / prompt_tokens must be at least
    min_cached_ratio. Never derives or estimates a ratio, and never
    compares prompts between calls — only the reported numbers.

    Returns {"result": "pass" | "fail" | "unknown", "reason": str | None,
    "calls": [one compact "call k: cached M/P" line per checked call]}.
    "unknown" (never "pass") when a checked call did not report its cache
    split; "fail" wins when both a miss and an unknown call occur.
    """
    if not model_calls:
        return {"result": "pass", "reason": None, "calls": []}
    seen: dict[Any, int] = {}
    calls_out: list[str] = []
    misses: list[str] = []
    any_unknown = False
    for call in model_calls:
        conversation = call.get("conversation")
        call_no = seen.get(conversation, 0) + 1
        seen[conversation] = call_no
        if call_no == 1:
            continue  # the first call in a conversation is never checked
        prompt, cached = call.get("prompt_tokens"), call.get("cached_tokens")
        if type(prompt) is not int or type(cached) is not int:
            any_unknown = True
            calls_out.append(f"call {call_no}: cache split unknown")
            continue
        calls_out.append(f"call {call_no}: cached {cached}/{prompt}")
        if prompt > 0 and cached / prompt < min_cached_ratio:
            misses.append(
                f"prompt cache miss at call {call_no}: cached {cached} of {prompt} prompt tokens"
            )
    if misses:
        return {"result": "fail", "reason": "; ".join(misses), "calls": calls_out}
    if any_unknown:
        return {
            "result": "unknown",
            "reason": "cache split not reported for one or more calls",
            "calls": calls_out,
        }
    return {"result": "pass", "reason": None, "calls": calls_out}


def combine_cache_checks(checks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Combine several scenarios' ``check_cache_misses`` results for one sweep.

    fail beats unknown beats pass; ``calls`` concatenates every scenario's
    checked-call lines, in order.
    """
    if not checks:
        return {"result": "pass", "reason": None, "calls": []}
    calls = [line for check in checks for line in check.get("calls", [])]
    fail_reasons = [check["reason"] for check in checks if check.get("result") == "fail"]
    if fail_reasons:
        return {"result": "fail", "reason": "; ".join(fail_reasons), "calls": calls}
    unknown = next((check for check in checks if check.get("result") == "unknown"), None)
    if unknown is not None:
        return {"result": "unknown", "reason": unknown.get("reason"), "calls": calls}
    return {"result": "pass", "reason": None, "calls": calls}


def _pyproject_table(start: Path | None, keys: Sequence[str]) -> tuple[Path | None, dict[str, Any]]:
    """Return the nearest pyproject.toml and the table at ``keys``.

    A missing file, a missing table, or unreadable TOML all give an empty
    table, so the defaults apply.
    """
    pyproject = _nearest_pyproject(start or Path.cwd())
    if pyproject is None:
        return None, {}
    try:
        import tomllib

        data: Any = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return pyproject, {}
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return pyproject, data if isinstance(data, dict) else {}


def _ladder_table(start: Path | None) -> tuple[Path | None, dict[str, Any]]:
    """Return the nearest pyproject.toml and its ``[tool.windtunnel.ladder]`` table."""
    return _pyproject_table(start, LADDER_CONFIG_TABLE)


def _windtunnel_table(start: Path | None) -> tuple[Path | None, dict[str, Any]]:
    """Return the nearest pyproject.toml and its ``[tool.windtunnel]`` table."""
    return _pyproject_table(start, ("tool", "windtunnel"))


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
    model: str | None = None,
) -> Fingerprint:
    """Fingerprint the artifact under test (see the design doc for the parts).

    ``exclude`` lists paths Wind Tunnel itself writes during a sweep (the runs
    directory, a --out report): they change every sweep and are not part of
    the artifact. ``target`` is the runtime's lock key — the backend a runtime
    name resolves to, e.g. http_inject's endpoint URL — so evidence from one
    endpoint never counts for another. ``model`` is the runtime's model
    label, so a pass on one model never earns a regression on another.
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
        "model": model,
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
        name for name in ("target", "model", "soul", "agents", "plugin", "wt_version")
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
    # The earning rule: a regression needs a passing focused run on the
    # current artifact since the last regression. ``earned_by`` names that
    # focused sweep; ``unearned`` is True when there is none; ``stale_earner``
    # is the latest focused pass since the last regression on another artifact.
    earned_by: str | None = None
    unearned: bool = False
    stale_earner: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return not self.missing and not self.unearned


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

    Two rules, both over ledger rows (in append order) against the same
    runtime ``target``. A "focused pass" below means a passing focused row on
    ``fingerprint`` from a sweep that finished within its budget.

    1. Earning. Once this target has any ladder history, a regression needs
       at least one focused pass written after the most recent regression.
       You do not build the whole plane to find out what flies: a full run is
       earned by showing, on this exact artifact, that the thing you changed
       works. The first sweep against a target (the baseline) is free.
    2. Failures. ``selected`` holds ``(pack, scenario)`` pairs; a selected
       scenario whose most recent regression row failed needs a focused pass
       of its own written after that row. Working per scenario means a
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

    def focused_pass(row: dict[str, Any]) -> bool:
        return (
            _experiment(row).get("tier") == "focused"
            and not _experiment(row).get("budget_exhausted")
            and not _row_failed(row)
        )

    last_regression: dict[tuple[str | None, str], int] = {}
    last_any_regression = -1
    has_history = False
    for position, row in enumerate(rows):
        if not same_target(row):
            continue
        has_history = True
        if _experiment(row).get("tier") == "regression":
            last_regression[key(row)] = position
            last_any_regression = position

    decision = GateDecision()
    if has_history:
        since = [
            row for position, row in enumerate(rows)
            if position > last_any_regression and same_target(row) and focused_pass(row)
        ]
        earners = [row for row in since if _experiment(row).get("fingerprint") == fingerprint]
        if earners:
            decision.earned_by = str(earners[-1].get("sweep_id") or "") or None
        else:
            decision.unearned = True
            decision.stale_earner = since[-1] if since else None
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
        passes = [row for row in rows[position + 1:] if key(row) == item and focused_pass(row)]
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
    if not names:
        return _unearned_message(decision, fingerprint, context, runs_dir)
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


def _unearned_message(
    decision: GateDecision, fingerprint: Fingerprint, context: str, runs_dir: Path | None
) -> str:
    runs_flag = "" if runs_dir is None else f" --runs {shlex.quote(str(runs_dir))}"
    lines = [
        "wt run: refusing a regression sweep: nothing has earned it. A full run "
        "needs at least one passing focused run on the current artifact "
        f"({fingerprint.value[:19]}…) since the last full run, and there is none.",
    ]
    stale = decision.stale_earner
    if stale is not None:
        parts = _experiment(stale).get("fingerprint_parts") or {}
        old_manifest = (
            _load_manifest(runs_dir, parts.get("git_tree")) if runs_dir is not None else None
        )
        lines.append(
            f"  The latest focused pass ({stale.get('scenario_id')}) is stale; since then "
            f"{describe_changes(parts, old_manifest, fingerprint)}."
        )
    lines += [
        "Test the wing before you build the plane:",
        f"  To see what is working now, read what is already on disk: "
        f"wt results{runs_flag}",
        "  To test your change, run the scenario it targets on its own:",
        f"  wt run --scenario <scenario>{context} --runs 3 --question \"<what you "
        "expect to learn>\" --expect pass",
        "When that passes, this regression is earned. Any change after it makes the "
        "pass stale again. If a file named above is something the runtime or your "
        "tooling writes (a log, a cache), gitignore it; it is not part of the artifact.",
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
    if_pass: str | None = None,
    if_fail: str | None = None,
    cost: dict[str, Any] | None = None,
    cost_usd: dict[str, Any] | None = None,
    cache_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``experiment`` object written into each of the sweep's ledger rows.

    ``cost_usd`` (see ``compute_cost_usd``) and ``cache_check`` (see
    ``check_cache_misses``) are only present when pricing / the cache check
    are configured — their absence, not a null, is what "off" looks like.
    """
    record: dict[str, Any] = {
        "counts_as_failure": counts_as_failure,
        "tier": tier,
        "question": question,
        "expect": expect,
        "if_pass": if_pass,
        "if_fail": if_fail,
        "cost": cost,
        "budget_s": budget.seconds,
        "budget_exhausted": budget.exhausted,
        "fingerprint": fingerprint.value,
        "fingerprint_parts": dict(fingerprint.parts),
        "source_trace": source_trace,
        "from_turn": from_turn,
        "evidence_for": (
            list(decision.gated_sweeps) if decision is not None and decision.satisfied else []
        ),
        "earned_by": decision.earned_by if decision is not None else None,
        "excluded_failing": list(decision.excluded_failing) if decision else [],
    }
    if cost_usd is not None:
        record["cost_usd"] = cost_usd
    if cache_check is not None:
        record["cache_check"] = cache_check
    return record


# ─── Cost and value: the experiments record ───────────────────────────────────

EXPERIMENTS_FILENAME = "experiments.ndjsonl"
EXPERIMENTS_FORMAT_VERSION = 1


def sum_tokens(usages: Iterable[dict[str, Any] | None]) -> dict[str, int | None]:
    """Sum token usage; a field is None as soon as any part did not report it.

    input_tokens is the TOTAL prompt tokens, including cached_tokens — see
    Trace.model_calls / call_usage_totals. Each of the three fields goes
    None independently.
    """
    totals: dict[str, int | None] = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    for usage in usages:
        for key in totals:
            value = usage.get(key) if isinstance(usage, dict) else None
            current = totals[key]
            totals[key] = (
                current + value if current is not None and type(value) is int else None
            )
    return totals


def _append_experiments(runs_dir: Path, record: dict[str, Any]) -> None:
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    with (Path(runs_dir) / EXPERIMENTS_FILENAME).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, sort_keys=True) + "\n")


def record_sweep(runs_dir: Path, **fields: Any) -> None:
    """Append one finished sweep (what it asked, what it cost, how it came out)."""
    _append_experiments(
        runs_dir,
        {"windtunnel_experiment": EXPERIMENTS_FORMAT_VERSION, "kind": "sweep",
         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields},
    )


def read_experiments(runs_dir: Path) -> list[dict[str, Any]]:
    """Every parseable experiments record, in file order."""
    rows: list[dict[str, Any]] = []
    try:
        lines = (Path(runs_dir) / EXPERIMENTS_FILENAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("windtunnel_experiment") == 1:
            rows.append(row)
    return rows


def record_review(runs_dir: Path, sweep_id: str, *, decision: str | None) -> None:
    """Record what a finished sweep changed (``decision``), or that it changed nothing.

    Raises LadderError for a sweep this runs directory never finished.
    """
    if not any(
        row.get("kind") == "sweep" and row.get("sweep_id") == sweep_id
        for row in read_experiments(runs_dir)
    ):
        raise LadderError(f"no finished sweep {sweep_id!r} in {runs_dir}/{EXPERIMENTS_FILENAME}")
    _append_experiments(
        runs_dir,
        {"windtunnel_experiment": EXPERIMENTS_FORMAT_VERSION, "kind": "review",
         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "sweep_id": sweep_id, "changed": decision is not None, "decision": decision},
    )


def ladder_summary(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per tier: how many sweeps, what they cost, and what they were worth.

    Token totals cover only the sweeps that reported tokens
    (``tokens_reported``); a sweep that did not report is counted, never
    guessed. A later review of the same sweep replaces an earlier one.
    """
    reviews = {
        row.get("sweep_id"): bool(row.get("changed"))
        for row in rows if row.get("kind") == "review"
    }
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") != "sweep":
            continue
        tier = str(row.get("tier"))
        entry = summary.setdefault(tier, {
            "sweeps": 0, "wall_s": 0.0, "input_tokens": 0, "cached_tokens": 0,
            "output_tokens": 0, "tokens_reported": 0, "cached_reported": 0,
            "predictions": 0, "predictions_held": 0,
            "reviewed": 0, "changed_decision": 0,
            "cost_usd": 0.0, "priced_sweeps": 0, "tokens_unknown_sweeps": 0,
            "uncached_input_usd": 0.0, "cache_read_usd": 0.0,
            "output_usd": 0.0, "time_usd": 0.0,
            "cache_fail_sweeps": 0, "cache_unknown_sweeps": 0,
        })
        entry["sweeps"] += 1
        raw_cost = row.get("cost")
        cost: dict[str, Any] = raw_cost if isinstance(raw_cost, dict) else {}
        entry["wall_s"] = round(entry["wall_s"] + float(cost.get("wall_s") or 0.0), 3)
        if type(cost.get("input_tokens")) is int and type(cost.get("output_tokens")) is int:
            entry["tokens_reported"] += 1
            entry["input_tokens"] += cost["input_tokens"]
            entry["output_tokens"] += cost["output_tokens"]
        if type(cost.get("cached_tokens")) is int:
            entry["cached_reported"] += 1
            entry["cached_tokens"] += cost["cached_tokens"]
        cost_usd = row.get("cost_usd")
        if isinstance(cost_usd, dict) and isinstance(cost_usd.get("total"), int | float):
            entry["cost_usd"] = round(entry["cost_usd"] + float(cost_usd["total"]), 6)
            entry["priced_sweeps"] += 1
            entry["time_usd"] = round(entry["time_usd"] + float(cost_usd.get("time") or 0.0), 6)
            if isinstance(cost_usd.get("uncached_input"), int | float):
                entry["uncached_input_usd"] = round(
                    entry["uncached_input_usd"] + float(cost_usd["uncached_input"]), 6
                )
            if isinstance(cost_usd.get("cache_read"), int | float):
                entry["cache_read_usd"] = round(
                    entry["cache_read_usd"] + float(cost_usd["cache_read"]), 6
                )
            if isinstance(cost_usd.get("output"), int | float):
                entry["output_usd"] = round(entry["output_usd"] + float(cost_usd["output"]), 6)
            if not cost_usd.get("tokens_known"):
                entry["tokens_unknown_sweeps"] += 1
        cache_check = row.get("cache_check")
        if isinstance(cache_check, dict):
            if cache_check.get("result") == "fail":
                entry["cache_fail_sweeps"] += 1
            elif cache_check.get("result") == "unknown":
                entry["cache_unknown_sweeps"] += 1
        if isinstance(row.get("prediction_held"), bool):
            entry["predictions"] += 1
            entry["predictions_held"] += int(row["prediction_held"])
        sweep_id = row.get("sweep_id")
        if sweep_id in reviews:
            entry["reviewed"] += 1
            entry["changed_decision"] += int(reviews[sweep_id])
    return {tier: summary[tier] for tier in (*TIER_ORDER, *summary) if tier in summary}


def cumulative_cost_usd(summary: dict[str, dict[str, Any]]) -> float | None:
    """Total $ across every tier's priced sweeps, or None if none were priced."""
    priced = [entry for entry in summary.values() if entry.get("priced_sweeps")]
    if not priced:
        return None
    return round(sum(float(entry["cost_usd"]) for entry in priced), 6)
