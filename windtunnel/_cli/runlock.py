"""Cross-process runtime lock: one `wt run` sweep per runtime at a time.

A runtime that tolerates a bounded number of concurrent jobs (every runtime
whose plugin does not declare ``max_concurrency = None``) usually owns
something machine-wide: fixed ports, a container name, one reset-able
backend. Two sweeps against it from two shells, agents, or CI steps would
reset each other mid-run. Each sweep therefore takes an exclusive OS file
lock for its runtime before building it and holds it until post_run() has
returned; a second sweep waits (printing who holds it) or, with --no-wait,
exits EXIT_RUNTIME_BUSY.

The lock is advisory and keyed by the runtime name, or by the plugin's
optional ``lock_key(runtime_name)`` when one name can reach different
backends (the built-in http_inject plugin keys by endpoint URL). The OS
releases it when the holding process exits, so a killed sweep never leaves
a stale lock behind. Lock files live in ``$WT_LOCK_DIR``, else a per-user
directory under the system temp dir.

Waiters are served by priority, then arrival (see docs/design/0005): each
waiting sweep holds a lock on its own ticket file in ``<lock>.queue/``, named
so that sorting the names orders the queue. Only the waiter at the head of the
queue tries the runtime lock. A ticket whose file lock can be taken belongs to
a process that is gone, and whoever notices removes it — the same
kernel-released-lock argument that keeps the runtime lock itself from going
stale. A new sweep never jumps ahead of a live waiter, including with
``--no-wait``.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCK_DIR_ENV = "WT_LOCK_DIR"
# sysexits.h EX_TEMPFAIL: the runtime is busy; retrying later may succeed.
EXIT_RUNTIME_BUSY = 75
# Windows byte-range locks are mandatory, so lock a byte far past the holder
# record rather than the record itself, which waiters still need to read.
_WINDOWS_LOCK_OFFSET = 1 << 30
# Queue priorities (lower first); `wt run` maps probe/focused/regression here.
DEFAULT_PRIORITY = 2
_TICKET_SUFFIX = ".ticket"


class RuntimeBusy(Exception):
    """Raised by runtime_lock(wait=False) while another process holds the lock."""

    def __init__(self, key: str, holder: dict[str, Any] | None) -> None:
        super().__init__(f"runtime {key!r} is in use: {describe_holder(holder)}")
        self.key = key
        self.holder = holder


def lock_dir() -> Path:
    override = os.environ.get(LOCK_DIR_ENV)
    if override:
        return Path(override)
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name available (containers, CI)
        user = "user"
    safe_user = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in user) or "user"
    return Path(tempfile.gettempdir()) / f"windtunnel-locks-{safe_user}"


def lock_path(key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)[:60]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return lock_dir() / f"{safe or 'runtime'}-{digest}.lock"


def holder_record(**fields: Any) -> dict[str, Any]:
    """Who holds a lock: written into the lock file for waiters to report."""
    return {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "since": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "command": " ".join(["wt", *sys.argv[1:]]),
        **fields,
    }


def describe_holder(holder: dict[str, Any] | None) -> str:
    if not holder:
        return "held by another process"
    parts = [f"held by pid {holder.get('pid')} on {holder.get('host')}"]
    if holder.get("since"):
        parts.append(f"since {holder['since']}")
    if holder.get("command"):
        parts.append(f"running `{holder['command']}`")
    return ", ".join(parts)


@contextmanager
def runtime_lock(
    key: str,
    *,
    wait: bool,
    holder: dict[str, Any],
    on_wait: Callable[[dict[str, Any] | None], None] | None = None,
    on_acquired: Callable[[float], None] | None = None,
    poll_s: float = 0.5,
    priority: int = DEFAULT_PRIORITY,
) -> Iterator[None]:
    """Hold the exclusive lock for ``key`` for the duration of the block.

    If another process holds it, or a live waiter is already queued for it:
    raise RuntimeBusy when ``wait`` is False; otherwise queue a ticket at
    ``priority`` (lower is served first; ties by arrival), call
    ``on_wait(holder)`` once, poll until this ticket is at the head of the
    queue and the lock frees, and call ``on_acquired(seconds_waited)``.
    """
    path = lock_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    queue = _queue_dir(path)
    try:
        if _head_ticket(queue) is not None or not _try_lock(fd):
            current = _read_holder(path)
            if not wait:
                raise RuntimeBusy(key, current)
            started = time.monotonic()
            with _ticket(queue, priority) as mine:
                if on_wait is not None:
                    on_wait(current)
                while True:
                    mine.ensure()
                    if mine.locked and _head_ticket(queue) == mine.path and _try_lock(fd):
                        break
                    time.sleep(poll_s)
            if on_acquired is not None:
                on_acquired(time.monotonic() - started)
        try:
            _write_holder(fd, holder)
            yield
        finally:
            _write_holder(fd, None)
            _unlock(fd)
    finally:
        os.close(fd)


def _queue_dir(path: Path) -> Path:
    return path.with_name(path.name + ".queue")


class _Ticket:
    """A waiter's place in the queue: a file this process holds locked.

    Between creating the file and locking it, another waiter probing the queue
    can hold the file's lock for a moment (to test whether it is dead), so the
    first lock attempt may fail; or it can decide the file is dead and remove
    it. ``ensure()`` is called on every poll and re-creates or re-locks the
    same path (keeping its place in line) until this process holds it, and
    ``locked`` is False until then, so the waiter never mistakes an unheld
    ticket for its turn.
    """

    def __init__(self, queue: Path, priority: int) -> None:
        priority = max(0, min(9, int(priority)))
        self.path = queue / f"{priority}-{time.time_ns():020d}-{os.getpid()}{_TICKET_SUFFIX}"
        self._fd: int | None = None
        self.locked = False

    def ensure(self) -> None:
        if self.locked and self.path.exists():
            return
        self._close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if _try_lock(fd):
            self._fd, self.locked = fd, True
        else:
            os.close(fd)  # a prober holds it this instant; try again next poll

    def _close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        if self.locked:
            _unlock(fd)
        self.locked = False
        os.close(fd)

    def release(self) -> None:
        self._close()
        try:
            self.path.unlink()
        except OSError:
            pass


@contextmanager
def _ticket(queue: Path, priority: int) -> Iterator[_Ticket]:
    """Hold a queue ticket for the duration of the wait."""
    ticket = _Ticket(queue, priority)
    ticket.ensure()
    try:
        yield ticket
    finally:
        ticket.release()


def _head_ticket(queue: Path) -> Path | None:
    """Return the first live ticket in queue order, reaping dead ones."""
    try:
        tickets = sorted(queue.glob(f"*{_TICKET_SUFFIX}"), key=lambda item: item.name)
    except OSError:
        return None
    for ticket in tickets:
        if _ticket_is_live(ticket):
            return ticket
    return None


def _ticket_is_live(ticket: Path) -> bool:
    try:
        fd = os.open(ticket, os.O_RDWR)
    except OSError:
        return False  # already removed by its owner
    try:
        if not _try_lock(fd):
            return True
        # Nobody holds it: the waiter that queued it is gone.
        _unlock(fd)
    finally:
        os.close(fd)
    try:
        ticket.unlink()
    except OSError:
        pass
    return False


def _read_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_holder(fd: int, holder: dict[str, Any] | None) -> None:
    try:
        os.ftruncate(fd, 0)
        if holder is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(holder, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # the record is a courtesy for waiters; the lock itself still holds


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
