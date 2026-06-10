"""Memory seeder for dim_memory_conflict — direct file write into the agent platform's memories/ dir.

Design choice (option a — direct file write):
  The agent platform's memory tool stores facts as JSON files in
  {data_dir}/memories/. We seed a controlled memory state by writing files
  in the same format BEFORE each scenario run, then wipe them via the
  standard _reset_state() path (rm -rf memories/*) after.

  This is fast and transparent. Trade-off: schema-coupled to the platform's
  memory file format. If the platform changes the memory storage format,
  this seeder breaks. Acceptable for prototype phase — documented here so a
  future extraction pass can swap to option (b) (setup turn via api_server)
  if the format changes.

Memory file format (observed from the platform's memory tool):
  {data_dir}/memories/<key>.json  OR  <uuid>.json
  Content: { "content": <str>, "timestamp": <iso_str>, ... }

  The exact filename is not critical for reading — the platform scans all
  files in memories/ and loads their content. We use <key>.json as filename
  so the seeded memory is identifiable during debugging.

SSH seeding (runner use):
  The runner SSHes to the eval host and writes the file directly via:
    echo '<json>' | sudo tee {host_data_dir}/memories/<key>.json

  The seed_memory_remote() function in runner.py handles that step.
  This module provides the local write + verification contract only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class MemorySeedSpec:
    """Specification for a single memory entry to seed.

    key:       identifier for this memory (used as filename stem and in content)
    content:   the fact text the agent should "remember"
    timestamp: ISO-format string; defaults to now. Use explicit timestamps
               for two_conflicting_memories so recency ordering is deterministic.
    source:    optional provenance label (e.g. "bench_seed", "user_input").
               Stored in the file but not used by evaluators.
    """
    key: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "bench_seed"


def seed_memory_file(memories_dir: Path, spec: MemorySeedSpec) -> Path:
    """Write a single memory entry to memories_dir/<spec.key>.json.

    Args:
        memories_dir: Path to the memories directory (must exist).
        spec:         The memory content to write.

    Returns:
        Path to the written file.

    Raises:
        FileNotFoundError: if memories_dir does not exist.
        OSError:           on other I/O errors.
    """
    memories_dir = Path(memories_dir)
    if not memories_dir.exists():
        raise FileNotFoundError(
            f"memories_dir does not exist: {memories_dir}. "
            "Create the directory before seeding."
        )

    # Platform memory file format: minimal JSON with content + timestamp.
    # The key is stored in the payload so the agent can read it back.
    payload = {
        "key": spec.key,
        "content": spec.content,
        "timestamp": spec.timestamp,
        "source": spec.source,
    }

    # Filename: <key>.json — human-readable, unique per key.
    # If the same key is seeded twice, the second write overwrites the first
    # (last-write-wins). This is intentional for the two_conflicting_memories
    # scenario, which uses the same base key with different timestamps.
    filename = f"{spec.key}.json"
    out_path = memories_dir / filename
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path


def wipe_memories(memories_dir: Path) -> None:
    """Remove all files from memories_dir.

    Safe to call on an empty directory (no-op). Does NOT remove the
    directory itself — only the files inside it.

    This is the local equivalent of what _reset_state() does on the
    eval host via 'rm -rf {host_data_dir}/memories/*'.

    Args:
        memories_dir: Path to the memories directory.
    """
    memories_dir = Path(memories_dir)
    if not memories_dir.exists():
        return
    for f in memories_dir.iterdir():
        if f.is_file():
            f.unlink()


def wipe_then_verify_empty(memories_dir: Path) -> None:
    """Assert memories_dir contains no files after wipe.

    Used as a post-scenario verification step: confirms the wipe
    actually cleared all memory state so cross-scenario contamination
    is impossible.

    Raises:
        AssertionError: if memories_dir still contains files after wipe attempt.
    """
    memories_dir = Path(memories_dir)
    remaining = [f for f in memories_dir.iterdir() if f.is_file()] if memories_dir.exists() else []
    assert len(remaining) == 0, (
        f"memories_dir is not empty after wipe: {[f.name for f in remaining]}. "
        "Cross-scenario contamination risk — check wipe_memories() call."
    )
