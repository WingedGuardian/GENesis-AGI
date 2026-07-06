"""Add ``attention_events.source`` — device provenance of the trigger utterance.

The engine now threads ``AmbientUtterance.source`` onto every ``AttentionEvent``
(``omi`` for the wearable connector, the edge connection id for home ambient), so the
shadow store can tell which device a perk came from and the Judgment tab can filter
by it. A machine-derived column: it rides the normal label-preserving upsert (in
``crud.attention.COLUMNS``), unlike the human-input ``acceptance_note``.

Fresh/test DBs get it from the canonical CREATE TABLE in ``db/schema/_tables.py``;
this numbered migration covers the existing-DB upgrade path. Idempotent:
PRAGMA-guarded ADD COLUMN. No commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='attention_events'"
    )
    if await cursor.fetchone():
        col_cursor = await db.execute("PRAGMA table_info(attention_events)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "source" not in cols:
            await db.execute("ALTER TABLE attention_events ADD COLUMN source TEXT")
