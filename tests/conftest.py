"""Suite-wide test isolation."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_runtime_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep `wt run`'s machine-wide runtime locks inside each test's tmp dir.

    Without this, a test sweep would lock the real per-user lock directory
    and could queue behind (or block) a developer's own concurrent sweep.
    Subprocess-based tests inherit the variable through os.environ.
    """
    monkeypatch.setenv("WT_LOCK_DIR", str(tmp_path / ".wt-locks"))
