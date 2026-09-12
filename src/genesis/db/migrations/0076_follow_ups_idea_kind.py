"""Add ``follow_ups.kind = 'idea'`` — the WS-M PR-2 ideas review lane.

SQLite cannot ALTER a CHECK constraint, so widening ``kind IN ('follow_up',
'tabled')`` to also allow ``'idea'`` requires a full table rebuild: create a
``follow_ups_new`` with the corrected CHECK, copy rows over the column
intersection, drop the original, rename into place, and recreate every index.

The ``'idea'`` lane holds autonomous brainstorm findings promoted from
surplus_insights staging; like ``'tabled'`` it is auto-excluded from every
dispatch reader (they positively filter ``kind = 'follow_up'``), so this is a
browse/review lane, never auto-dispatched.

Self-contained + idempotent (mirrors 0012's CHECK-rebuild): guards on the live
DDL already containing ``'idea'`` (fresh DBs get it from the canonical CREATE in
_tables.py; a re-run is a no-op). The row copy uses ``_intersection_copy`` so a
future column added to _tables.py but not here can never silently drop data — it
RAISES, and (per the runner's atomic BEGIN/COMMIT + rollback) the raise rolls the
whole migration back rather than false-greening a partial COMMIT; we therefore do
NOT wrap it in try/except. No ``db.commit()`` — the runner owns the transaction.

(Numbered 0076: 0075 is the highest present; the runner applies by per-id
tracking and only duplicate prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite

from genesis.db.schema._migrations import _intersection_copy

# Full canonical follow_ups column set (must match _tables.py's CREATE verbatim,
# revisit_condition LAST for fresh/migrated column-order parity) — only the kind
# CHECK differs between up() (3 values) and down() (2 values).
_COLUMNS_TEMPLATE = """
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    source_session   TEXT,
    content          TEXT NOT NULL,
    reason           TEXT,
    strategy         TEXT NOT NULL CHECK (
        strategy IN ('scheduled_task', 'surplus_task', 'ego_judgment', 'user_input_needed')
    ),
    scheduled_at     TEXT,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'blocked')
    ),
    linked_task_id   TEXT,
    priority         TEXT NOT NULL DEFAULT 'medium' CHECK (
        priority IN ('low', 'medium', 'high', 'critical')
    ),
    created_at       TEXT NOT NULL,
    completed_at     TEXT,
    resolution_notes TEXT,
    blocked_reason   TEXT,
    escalated_to     TEXT,
    verified_at      TEXT,
    verification_notes TEXT,
    pinned           INTEGER NOT NULL DEFAULT 0,
    kind             TEXT NOT NULL DEFAULT 'follow_up' CHECK (
        kind IN ({kind_values})
    ),
    domain           TEXT CHECK (
        domain IN ('internal', 'user_world')
    ),
    goal_id          TEXT,
    dedup_key        TEXT,
    revisit_condition TEXT
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_status ON follow_ups(status)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_scheduled ON follow_ups(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_source ON follow_ups(source)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_linked_task ON follow_ups(linked_task_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_ups_dedup "
    "ON follow_ups(dedup_key) WHERE dedup_key IS NOT NULL",
)


async def _rebuild(db: aiosqlite.Connection, *, kind_values: str) -> None:
    """Rebuild follow_ups with the given kind CHECK, preserving all rows/indexes."""
    await db.execute("DROP TABLE IF EXISTS follow_ups_new")
    await db.execute(
        f"CREATE TABLE follow_ups_new ({_COLUMNS_TEMPLATE.format(kind_values=kind_values)})"
    )
    # Copy over the column intersection (raise-on-drift — do NOT swallow; the
    # runner rolls the migration back on the raise, never a partial COMMIT).
    await _intersection_copy(db, src="follow_ups", dst="follow_ups_new")
    await db.execute("DROP TABLE follow_ups")
    await db.execute("ALTER TABLE follow_ups_new RENAME TO follow_ups")
    for stmt in _INDEXES:
        await db.execute(stmt)


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    row = await cursor.fetchone()
    if not row:
        return  # fresh DB — the canonical CREATE already carries the 'idea' CHECK
    ddl = row[0] or ""
    if "'idea'" in ddl:
        return  # already widened
    await _rebuild(db, kind_values="'follow_up', 'tabled', 'idea'")


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    row = await cursor.fetchone()
    if not row:
        return
    ddl = row[0] or ""
    if "'idea'" not in ddl:
        return  # already narrowed
    # Preserve data: reclassify any 'idea' rows to 'tabled' so the narrowed CHECK
    # is satisfiable, then rebuild without 'idea'.
    await db.execute("UPDATE follow_ups SET kind = 'tabled' WHERE kind = 'idea'")
    await _rebuild(db, kind_values="'follow_up', 'tabled'")
