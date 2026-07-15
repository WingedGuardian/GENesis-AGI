"""d0001 — backfill origin_class onto Qdrant payloads (WS-3 B0).

Auto-runs on boot. On an install that already ran the manual
``scripts/backfill_origin_class_qdrant.py`` this is a clean no-op (idempotent
skip of already-classified points); on a LAGGING install it heals the Qdrant
payloads on the next pull+restart with no control plane — which is the whole
point of the data-migration framework.

migrate()/verify() are SYNC (blocking SQLite + Qdrant I/O); the runner offloads
them via ``asyncio.to_thread``. They open their OWN read connection — never the
runtime's async ``rt._db``.
"""

from __future__ import annotations

import sqlite3

from genesis.env import genesis_db_path
from genesis.memory.origin_class_backfill import backfill_origin_class, count_missing_origin_class
from genesis.qdrant.collections import get_client

requires_operator = False


def migrate() -> dict:
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        return backfill_origin_class(db, get_client(), dry_run=False)
    finally:
        db.close()


def verify() -> bool:
    """Complete when no point in either collection lacks origin_class."""
    return count_missing_origin_class(get_client()) == 0
