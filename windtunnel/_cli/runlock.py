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
) -> Iterator[None]:
    """Hold the exclusive lock for ``key`` for the duration of the block.

    If another process holds it: raise RuntimeBusy when ``wait`` is False;
    otherwise call ``on_wait(holder)`` once, poll until the lock frees, and
    call ``on_acquired(seconds_waited)``.
    """
    path = lock_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            current = _read_holder(path)
            if not wait:
                raise RuntimeBusy(key, current)
            if on_wait is not None:
                on_wait(current)
            started = time.monotonic()
            while not _try_lock(fd):
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
