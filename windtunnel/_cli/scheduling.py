"""Scheduler selection and runtime concurrency limits for `wt run`."""

from __future__ import annotations

from pathlib import Path

from windtunnel._cli.module_identity import load_module_by_dotted_path, load_module_from_file
from windtunnel.spi.scheduler import BUILTIN_SCHEDULERS, Scheduler, SequentialScheduler

# How many jobs a concurrent scheduler runs when neither --max-concurrency nor
# the runtime plugin sets a bound (a runtime that declares no limit at all).
DEFAULT_UNBOUNDED_CONCURRENCY = 4


class SchedulingError(ValueError):
    """A scheduler or concurrency setting the CLI cannot honor (usage error)."""


def runtime_concurrency_limit(plugin: object, runtime_name: str) -> int | None:
    """Return how many jobs the runtime tolerates at once; None means no limit.

    Read from the optional RuntimePlugin attribute ``max_concurrency``. A
    plugin that declares nothing gets 1: many runtimes bind fixed ports or
    share one reset-able backend, so concurrency is opt-in.
    """
    if not hasattr(plugin, "max_concurrency"):
        return 1
    value = getattr(plugin, "max_concurrency")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulingError(
            f"runtime plugin {runtime_name!r} declares max_concurrency={value!r}; "
            "expected an integer >= 1, or None for no limit"
        )
    return value


def runtime_lock_key(plugin: object, runtime_name: str) -> str:
    """Return the identity two sweeps must share to exclude each other.

    The runtime name, unless the plugin defines ``lock_key(runtime_name)``
    because one name can reach different backends (for example an endpoint
    URL taken from the environment).
    """
    method = getattr(plugin, "lock_key", None)
    if not callable(method):
        return runtime_name
    key = method(runtime_name)
    if not isinstance(key, str) or not key.strip():
        raise SchedulingError(
            f"runtime plugin {runtime_name!r} lock_key() returned {key!r}; expected a "
            "non-empty string"
        )
    return key


def resolve_scheduler(
    spec: str,
    *,
    requested: int | None,
    runtime_limit: int | None,
    runtime_name: str,
    hooks_active: bool,
) -> tuple[Scheduler, int, list[str]]:
    """Build the scheduler for one sweep.

    Returns (scheduler, effective concurrency, notices). The effective value
    is the most jobs the sweep will let run at once — the requested value
    (default: the runtime's limit, else DEFAULT_UNBOUNDED_CONCURRENCY),
    clamped to the runtime's declared limit and to 1 while lifecycle hooks
    are active. Each clamp produces a one-line notice.
    """
    target = _scheduler_target(spec)
    notices: list[str] = []
    is_sequential = target is SequentialScheduler or isinstance(target, SequentialScheduler)
    if is_sequential:
        if requested is not None and requested > 1:
            notices.append(
                f"--max-concurrency {requested} has no effect with the sequential scheduler; "
                "pass --scheduler concurrent to run scenario jobs in parallel"
            )
        return SequentialScheduler(), 1, notices

    effective = requested
    if effective is None:
        effective = runtime_limit if runtime_limit is not None else DEFAULT_UNBOUNDED_CONCURRENCY
    if runtime_limit is not None and effective > runtime_limit:
        notices.append(
            f"runtime {runtime_name!r} tolerates at most {runtime_limit} concurrent job(s); "
            f"running {runtime_limit} at a time (requested {effective})"
        )
        effective = runtime_limit
    if hooks_active and effective > 1:
        notices.append(
            "lifecycle hooks are active and may keep state that assumes sequential runs; "
            "running one scenario job at a time"
        )
        effective = 1
    if isinstance(target, Scheduler):
        return target, effective, notices
    return target(max_concurrency=effective), effective, notices


def _scheduler_target(spec: str) -> type[Scheduler] | Scheduler:
    """Resolve a --scheduler value to a Scheduler subclass or instance."""
    if spec in BUILTIN_SCHEDULERS:
        return BUILTIN_SCHEDULERS[spec]
    module_or_path, sep, attr = spec.partition(":")
    if not sep or not module_or_path or not attr:
        raise SchedulingError(
            f"unknown scheduler {spec!r}; expected one of "
            f"{', '.join(sorted(BUILTIN_SCHEDULERS))}, or package.module:Class"
        )
    try:
        if module_or_path.endswith(".py") or "/" in module_or_path or "\\" in module_or_path:
            path = Path(module_or_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            module = load_module_from_file(path, "_windtunnel_scheduler")
        else:
            module = load_module_by_dotted_path(module_or_path)
        obj = getattr(module, attr)
    except Exception as exc:  # noqa: BLE001 - load failures are usage errors
        raise SchedulingError(f"could not load scheduler {spec!r}: {exc}") from exc
    if isinstance(obj, type) and issubclass(obj, Scheduler):
        return obj
    if isinstance(obj, Scheduler):
        return obj
    raise SchedulingError(
        f"scheduler {spec!r} must name a windtunnel.spi.Scheduler subclass or instance, "
        f"got {type(obj).__name__}"
    )
