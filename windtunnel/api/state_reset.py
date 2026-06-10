"""State-reset hook — per-scenario wipe of agent profile state surfaces.

The reset contract: every scenario starts from a clean agent state — no
conversation, session, or search-index residue from a prior scenario run.

Why this matters:
  An early specific_field_lookup scenario was answered from a prior
  eval transcript via messages_fts_trigram (session_search backing store),
  not the mock ops suite — producing a passing-looking run that was actually
  testing cached state. This module closes that contamination path.

What gets wiped per-scenario (default keep_state=False):
  - messages table (cross-scenario turn content)
  - sessions table (cross-scenario session metadata)
  - state_meta table (except 'db_initialized' key — the agent checks it)
  - messages_fts + messages_fts_trigram FTS5 indexes (rebuilt after wipe)

What is NEVER wiped:
  - schema_version table — the agent checks this on startup; wiping crashes it

Per-bench-run surfaces (not handled here — done at bench boundary):
  - sessions/*.json (CLI path legacy) — rm -f
  - memories/ and memory/ directories — rm -rf
  - skills/ directory — rm -rf
  SOUL.md is handled by writing the target file + fresh session_id per scenario.

The StateResetConfig.keep_state=True flag is the --soul-keep equivalent for
debugging: skips all wipes so you can inspect state after a run. Use with
caution — consecutive scenarios will contaminate each other.

This module is designed to run against a LOCAL sqlite3 file (when the agent
runs in a local Docker container) OR via parameterised SQL sent through an
ssh_fn callback for remote hosts. The local sqlite3 path is the primary use
case; StateResetConfig can be extended with an ssh_fn for remote targets.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StateResetConfig:
    """Configuration for the state-reset hook.

    state_db_path: path to state.db (local file — the primary mode).
    keep_state:    if True, skip all wipes (debug mode). Default False.
    """
    state_db_path: Path
    keep_state: bool = False


def reset_state_db(cfg: StateResetConfig) -> None:
    """Wipe all cross-scenario state from state.db.

    Safe to call before every scenario. Idempotent — calling on an
    already-empty DB is a no-op (DELETEs from empty tables, rebuild
    on empty content table is harmless).

    When keep_state=True: no-op (debug mode — state preserved for inspection).

    Raises:
        sqlite3.OperationalError if state.db doesn't exist or schema is
        missing expected tables. The caller (bench runner) should ensure
        the agent container has initialised state.db before calling.
    """
    if cfg.keep_state:
        return

    db_path = Path(cfg.state_db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        # Run everything in a single transaction for atomicity.
        # FTS 'rebuild' commands must run AFTER the DELETE so they
        # re-index from the (now-empty) messages table.
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        # Preserve 'db_initialized' — the agent checks it on startup.
        conn.execute("DELETE FROM state_meta WHERE key != 'db_initialized'")
        # Rebuild FTS indexes from the now-empty messages table.
        # This clears messages_fts and messages_fts_trigram — the
        # session_search backing store behind the contamination path
        # described in the module docstring.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.execute(
            "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')"
        )
        conn.commit()
    finally:
        conn.close()
