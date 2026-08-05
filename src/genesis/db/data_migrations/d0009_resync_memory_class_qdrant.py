"""d0009 — re-sync memory_class onto Qdrant payloads (recompute-regression repair).

Auto-runs on boot. Repairs Qdrant payloads that a past buggy re-embed diverged
from the authoritative SQLite ``memory_metadata.memory_class`` (the recovery
worker used to recompute the class heuristically, discarding explicit overrides
like reference-extraction's ``fact`` on URL content). The worker itself is now
fixed to restore the stored class; this heals the points already corrupted —
which the reconcile lane's mirror requeue never revisits because they still
have vectors.

On an install that never hit the regression this is a clean no-op (0 diverged
points); on a lagging install it self-heals on the next pull+restart with no
control plane — the whole point of the data-migration framework.

migrate()/verify() are SYNC (blocking SQLite + Qdrant I/O); the runner offloads
them via ``asyncio.to_thread``. They open their OWN read connection — never the
runtime's async ``rt._db``.
"""

from __future__ import annotations

import sqlite3

from genesis.env import genesis_db_path
from genesis.memory.memory_class_backfill import (
    count_diverged_memory_class,
    resync_memory_class,
)
from genesis.qdrant.collections import get_client

requires_operator = False


def migrate() -> dict:
    """Re-sync memory_class onto diverged Qdrant payloads (see module docstring).

    Returns per-target-class repair counts; a clean no-op ``{}`` where nothing
    drifted."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        return resync_memory_class(db, get_client(), dry_run=False)
    finally:
        db.close()


def verify() -> bool:
    """Complete when no point's Qdrant memory_class disagrees with SQLite."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        return count_diverged_memory_class(db, get_client()) == 0
    finally:
        db.close()
