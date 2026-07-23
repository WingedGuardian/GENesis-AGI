"""Add ``job_health.error_type`` — the exception class behind a job failure.

Before this, ``job_health.last_error`` was the only failure detail stored, and it
was written as a bare ``str(exception)``. APScheduler exceptions routinely render
as an EMPTY string, so live rows carried ``last_error = ""`` — a failure record
with no diagnosable content. The exception TYPE is precisely the signal that was
being dropped.

``error_type`` is NULL when no exception caused the failure (a semantic failure —
e.g. an external quota block surfaced through a job result's reason). That makes
the column a structural discriminator between an internal defect and an external
blocker, not just extra detail.

Cleared on recovery alongside ``last_error`` (``record_job_success`` /
``clear_stale_job_failures`` both pop it), so a recovered job never keeps a stale
type.

Self-contained upgrade path: the column ADD is PRAGMA-guarded so this migration
applies whether it runs via ``create_all_tables``' base path (where
``_migrate_add_columns`` has already added the column — the guard then skips) OR
via the standalone numbered-migration runner (``python -m genesis.db.migrations
--apply``, used by update.sh) with no create_all_tables. Fresh/test DBs get the
column from the CREATE TABLE in ``db/schema/_migrations.py``. No commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_health'"
    )
    if not await cursor.fetchone():
        return  # fresh DB — the CREATE TABLE already carries the column

    cursor = await db.execute("PRAGMA table_info(job_health)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "error_type" not in cols:
        await db.execute("ALTER TABLE job_health ADD COLUMN error_type TEXT")


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_health'"
    )
    if not await cursor.fetchone():
        return

    cursor = await db.execute("PRAGMA table_info(job_health)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "error_type" in cols:
        await db.execute("ALTER TABLE job_health DROP COLUMN error_type")
