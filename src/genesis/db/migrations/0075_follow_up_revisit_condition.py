"""Add ``follow_ups.revisit_condition`` — the trigger that resurfaces an item.

Part of the follow-up lane-discipline fix (CC memory ``followup_kind_conflation``).
The MCP ``follow_up_create``/``follow_up_update`` handlers now take a ``work_state``
(``ready`` / ``blocked_on_trigger`` / ``deferred_cold``) and DERIVE ``kind``, so
priority can no longer leak into the hot(follow_up)/cold(tabled) lane choice.
``blocked_on_trigger`` requires a ``revisit_condition`` naming the time/event it
waits on; ``deferred_cold`` may optionally carry the condition that would revive it.

``revisit_condition`` is NULL for ``ready`` follow-ups and for rule-based
programmatic rows (e.g. inbox WATCH/BOOKMARK markers) that set ``kind`` directly.

Self-contained upgrade path: the column ADD is PRAGMA-guarded so this migration
applies whether it runs via ``create_all_tables`` (which builds the canonical
CREATE TABLE — already carrying the column — before the runner) OR via the
standalone numbered-migration runner (``python -m genesis.db.migrations --apply``,
used by update.sh) on a legacy DB. No commit — the runner owns the transaction.
(Numbered 0075: 0074 is claimed by the in-flight memory-reconcile PR; the runner
applies by per-id tracking and tolerates gaps — only duplicate prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    if not await cursor.fetchone():
        return  # fresh DB — the CREATE TABLE already carries the column

    cursor = await db.execute("PRAGMA table_info(follow_ups)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "revisit_condition" not in cols:
        await db.execute("ALTER TABLE follow_ups ADD COLUMN revisit_condition TEXT")


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    if not await cursor.fetchone():
        return

    cursor = await db.execute("PRAGMA table_info(follow_ups)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "revisit_condition" in cols:
        await db.execute("ALTER TABLE follow_ups DROP COLUMN revisit_condition")
