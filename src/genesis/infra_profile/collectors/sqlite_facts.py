"""SQLite configuration facts for the live Genesis database.

The raw SQL lives in ``db/crud/pragma_facts.py`` (repo rule: DB access goes
through db.crud); this collector owns file sizes, path resolution, and the
facts/metrics degradation policy.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from genesis.db.crud.pragma_facts import read_pragma_facts
from genesis.env import genesis_db_path
from genesis.infra_profile.types import SectionResult

logger = logging.getLogger(__name__)


def _collect_sync(db_path: Path) -> SectionResult:
    facts: dict = {"path": str(db_path)}
    metrics: dict = {}

    if not db_path.exists():
        return SectionResult.failed("sqlite", f"database not found at {db_path}")

    pragma_facts, pragma_metrics = read_pragma_facts(db_path)
    facts["pragmas"] = pragma_facts
    metrics.update(pragma_metrics)

    metrics["db_size_bytes"] = db_path.stat().st_size
    try:
        # No exists() pre-check — a WAL checkpoint can rotate the -wal file
        # between check and stat; treat any miss as size 0.
        metrics["wal_size_bytes"] = db_path.with_name(db_path.name + "-wal").stat().st_size
    except OSError:
        metrics["wal_size_bytes"] = 0

    return SectionResult(name="sqlite", facts=facts, metrics=metrics)


async def collect_sqlite(db_path: Path | None = None) -> SectionResult:
    """Collect SQLite pragmas + sizes off-thread (sqlite3 is blocking)."""
    path = db_path if db_path is not None else genesis_db_path()
    try:
        return await asyncio.to_thread(_collect_sync, path)
    except (sqlite3.Error, OSError) as exc:
        # OSError: the size stat() calls can race a WAL checkpoint rotating
        # the -wal file, or a restore swapping the db (review 2026-07-12).
        return SectionResult.failed("sqlite", f"collection failed: {exc}")
