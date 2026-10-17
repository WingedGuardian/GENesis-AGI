"""Clear the dream run id from rows an explicit supersede already owns.

``dream_cycle_run_id`` now tells a dream retirement from an explicit
supersession: the dream paths write it together with ``superseded_by``, and
``mark_superseded`` clears it. Before that change ``mark_superseded`` left the
run id in place, and no dream path wrote ``superseded_by``, so on an existing
database a row carrying BOTH was explicitly superseded after the dream cycle
touched it (a retired original, or a synthesis stamped ``synthesis:<run>``).
Left alone, the new reading takes it for a dream retirement: a supersede retry
loses its repair path, and a rollback of that dream run would un-deprecate the
row or hard-delete the synthesis, erasing the explicit pointer.

Runs at startup before any init step (``runtime/init/db.py``), so before any
new-code dream retirement can write the same combination. Self-contained, no
``db.commit()`` (the runner owns the transaction), and idempotent: a second
run matches nothing it changed.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_metadata'"
    )
    if not await cursor.fetchone():
        return
    cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(memory_metadata)")}
    if not {"dream_cycle_run_id", "superseded_by"} <= cols:
        return
    await db.execute(
        "UPDATE memory_metadata SET dream_cycle_run_id = NULL "
        "WHERE dream_cycle_run_id IS NOT NULL AND superseded_by IS NOT NULL"
    )
