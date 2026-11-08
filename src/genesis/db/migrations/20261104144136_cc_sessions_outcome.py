"""Record how a session ENDED, not only that it ended, on ``cc_sessions``.

``outcome`` / ``outcome_reason`` are written by ``crud.cc_sessions.update_status``
in the same UPDATE as the terminal status; ``outcome_paged_at`` is stamped by
whatever told the owner about a stopped session, so nothing pages it twice.
Nullable, no backfill: an old row reads NULL, which means "not judged" and is
never paged. Mirrored in ``schema._migrations._migrate_add_columns`` so an
existing DB gets the columns on the base ``create_all_tables`` path too.
"""

_COLUMNS = ("outcome", "outcome_reason", "outcome_paged_at")


async def up(db):
    row = await (
        await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cc_sessions'")
    ).fetchone()
    if row is None:
        return  # Pre-base path: canonical create_all_tables installs the columns.
    cursor = await db.execute("PRAGMA table_info(cc_sessions)")
    cols = {r[1] for r in await cursor.fetchall()}
    for name in _COLUMNS:
        if name not in cols:
            await db.execute(f"ALTER TABLE cc_sessions ADD COLUMN {name} TEXT")
