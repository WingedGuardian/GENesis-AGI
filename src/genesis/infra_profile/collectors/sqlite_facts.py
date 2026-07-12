"""SQLite configuration facts for the live Genesis database.

Opens a SEPARATE read-only connection (``mode=ro`` URI — WAL-aware, unlike
``immutable=1``) in a worker thread. Never touches the runtime's
``SerializedConnection``, never ATTACHes the live WAL db.

Pragmas are facts (a changed journal_mode or page_size is a real config
event); file sizes and freelist are metrics.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from genesis.env import genesis_db_path
from genesis.infra_profile.types import SectionResult

logger = logging.getLogger(__name__)

_FACT_PRAGMAS = (
    "journal_mode",
    "synchronous",
    "page_size",
    "mmap_size",
    "wal_autocheckpoint",
    "auto_vacuum",
    "cache_size",
    "user_version",
)


def _collect_sync(db_path: Path) -> SectionResult:
    facts: dict = {"path": str(db_path)}
    metrics: dict = {}

    if not db_path.exists():
        return SectionResult.failed("sqlite", f"database not found at {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        pragmas: dict[str, object] = {}
        for name in _FACT_PRAGMAS:
            row = conn.execute(f"PRAGMA {name}").fetchone()
            pragmas[name] = row[0] if row else None
        facts["pragmas"] = pragmas
        row = conn.execute("PRAGMA freelist_count").fetchone()
        metrics["freelist_pages"] = row[0] if row else None
    finally:
        conn.close()

    metrics["db_size_bytes"] = db_path.stat().st_size
    wal = db_path.with_name(db_path.name + "-wal")
    metrics["wal_size_bytes"] = wal.stat().st_size if wal.exists() else 0

    return SectionResult(name="sqlite", facts=facts, metrics=metrics)


async def collect_sqlite(db_path: Path | None = None) -> SectionResult:
    """Collect SQLite pragmas + sizes off-thread (sqlite3 is blocking)."""
    path = db_path if db_path is not None else genesis_db_path()
    try:
        return await asyncio.to_thread(_collect_sync, path)
    except sqlite3.Error as exc:
        return SectionResult.failed("sqlite", f"pragma read failed: {exc}")
