"""Scheduler SPI — how a sweep's run jobs execute.

`wt run` turns its selected scenarios into RunJobs, one per scenario: a job
provisions a handle from the runtime, drives that scenario's N runs, and
records the outcome through the sweep's own thread-safe bookkeeping (trace
and sidecar files, ledger row, progress events, the printed summary line).
A Scheduler decides only *how* the jobs execute — their order and how many
run at once. It never sees scenarios, runtimes, or results.

Two ship:

- SequentialScheduler — a plain loop, one job at a time. The default, and
  identical to the historical sweep.
- ConcurrentScheduler — a thread pool running up to ``max_concurrency`` jobs
  at once. Each job provisions its own handle from the one runtime object, so
  it is only safe for runtimes that tolerate concurrent sessions: the CLI
  never exceeds the limit a RuntimePlugin declares (``max_concurrency``,
  default 1 — see windtunnel.spi.runtime_plugin).

A third party can plug in its own (``wt run --scheduler package.module:Class``)
by subclassing Scheduler. The contract every implementation keeps:

1. Run each job at most once, and start no job after ``stop`` is set (the
   sweep sets it when its circuit breaker trips).
2. Never run more than ``max_concurrency`` jobs at the same time.
3. Jobs report scenario failures themselves; an exception that escapes a job
   is a sweep-level failure (for example, the disk rejecting a trace). On
   one, start no further jobs, let running jobs finish, then re-raise the
   first such exception.

The CLI additionally holds one runtime slot per running job, so even a
scheduler that broke rule 2 could not exceed what the runtime declared.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class RunJob:
    """One unit of sweep work: every run of one scenario on one handle.

    index: the job's position in the sweep (selection order); outputs are
        reported in this order whatever order jobs finish in.
    name:  the scenario name, for scheduler logging and diagnostics.
    runs:  how many runs the job executes (the sweep's ``--runs``).
    fn:    executes the job. Call the RunJob itself rather than ``fn``.
    """

    index: int
    name: str
    runs: int
    fn: Callable[[], None]

    def __call__(self) -> None:
        self.fn()


class Scheduler(ABC):
    """Executes a sweep's RunJobs; see the module docstring for the contract."""

    name: ClassVar[str] = "custom"

    def __init__(self, max_concurrency: int = 1) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError(f"max_concurrency must be an integer >= 1, got {max_concurrency!r}")
        self.max_concurrency = max_concurrency

    @abstractmethod
    def execute(self, jobs: Sequence[RunJob], stop: threading.Event) -> None:
        """Run ``jobs`` under the contract; return when no job is running."""


class SequentialScheduler(Scheduler):
    """Run jobs one after another in sweep order (the default)."""

    name = "sequential"

    def __init__(self, max_concurrency: int = 1) -> None:
        super().__init__(1)

    def execute(self, jobs: Sequence[RunJob], stop: threading.Event) -> None:
        for job in jobs:
            if stop.is_set():
                return
            job()


class ConcurrentScheduler(Scheduler):
    """Run up to ``max_concurrency`` jobs at once on a thread pool.

    Jobs start in sweep order; one is submitted only when a worker is free,
    so setting ``stop`` prevents every job that has not started yet.
    """

    name = "concurrent"

    def execute(self, jobs: Sequence[RunJob], stop: threading.Event) -> None:
        pending = iter(jobs)
        running: set[Future[None]] = set()
        first_error: BaseException | None = None
        with ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="wt-job"
        ) as pool:
            while True:
                while (
                    first_error is None
                    and not stop.is_set()
                    and len(running) < self.max_concurrency
                ):
                    job = next(pending, None)
                    if job is None:
                        break
                    running.add(pool.submit(job))
                if not running:
                    break
                done, still_running = wait(running, return_when=FIRST_COMPLETED)
                running = set(still_running)
                for future in done:
                    error = future.exception()
                    if error is not None and first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error


BUILTIN_SCHEDULERS: dict[str, type[Scheduler]] = {
    SequentialScheduler.name: SequentialScheduler,
    ConcurrentScheduler.name: ConcurrentScheduler,
}
