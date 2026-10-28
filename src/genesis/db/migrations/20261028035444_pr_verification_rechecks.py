"""Add ``rechecks`` to ``pr_verifications``: the measurements a closed row still owes.

WHY. A validator closes a row ``pass-with-measured-gaps`` when something was
established and some gap remains. MEASURED 2026-10-27 on one validator backlog run
of a live install: 99 rows closed, carrying 158 gaps, of which 44 were EVENT-GATED —
a measurement that could not be taken yet because the event it needs (a scheduled
job's next run, a date) had not happened. Once the row closed, nothing anywhere
remembered that those 44 were owed. Issue #3694.

ONE COLUMN, JSON. A list of ``{trigger, recheck, detail, results, done_at}``,
written by ``close_verification`` in the same statement that closes the row and
appended to by ``append_recheck_result`` — which never reopens it. A child table
was the alternative; a row owes a handful at most, every reader wants them with
the row, and the store's own docstring already rejected a child table for attempt
history against the New-Store Gate.

NO CHECK CONSTRAINT, for the reason the verdict migration (``20260926061637``)
gives: SQLite cannot ALTER a CHECK.

BACKFILL ONLY WHAT IS TYPED. A row closed between the typed-gaps release and this
column carries its event-gated gaps in its stored evidence document, as structure
(``gaps[].kind == "event_gated"`` with ``trigger``/``recheck``/``detail``); leaving
its column NULL would forgive exactly the measurements this migration exists to
keep. So those are copied across, unfinished, in the shape ``close_verification``
writes. An UNTYPED document (pre-typed ``scope_limits`` prose) is left NULL: there
the kind was never chosen, and inferring it would be inventing structure the prose
never had. Plain SQL, so the migration imports nothing from the code it predates;
every JSON read goes through a ``json_valid`` CASE and a ``fullkey`` lookup, so a
malformed document or entry is skipped, never an error that aborts the migration.

APPENDED LAST, with no inline comment, for the two-build-path parity test (see
the verdict migration's docstring for why the order and the absence of comments
are load-bearing).

Self-contained + idempotent: the table guard returns early on a fresh install
(``create_all_tables`` builds the canonical shape), the ADD is PRAGMA guarded on its
own column, and the backfill touches only rows whose ``rechecks`` is still NULL. No
commit: the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


#: A closed row's evidence, or an empty object when it is not valid JSON.
_DOC = "CASE WHEN json_valid(evidence) THEN evidence ELSE '{}' END"

#: The event-gated gaps of that document that carry all three fields as text.
_EVENT_GAPS = (
    f"FROM json_each({_DOC}, '$.gaps') AS g "
    f"WHERE json_extract({_DOC}, g.fullkey || '.kind') = 'event_gated' "
    f"AND json_type({_DOC}, g.fullkey || '.trigger') = 'text' "
    f"AND json_type({_DOC}, g.fullkey || '.recheck') = 'text' "
    f"AND json_type({_DOC}, g.fullkey || '.detail') = 'text'"
)


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "pr_verifications", "rechecks", "TEXT")
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pr_verifications'"
    )
    if await cursor.fetchone() is None:
        return
    await db.execute(
        "UPDATE pr_verifications SET rechecks = (SELECT json_group_array(json_object("
        f"'trigger', json_extract({_DOC}, g.fullkey || '.trigger'), "
        f"'recheck', json_extract({_DOC}, g.fullkey || '.recheck'), "
        f"'detail', json_extract({_DOC}, g.fullkey || '.detail'), "
        f"'results', json('[]'), 'done_at', NULL)) {_EVENT_GAPS}) "
        f"WHERE status = 'closed' AND rechecks IS NULL "
        f"AND json_type({_DOC}, '$.gaps') = 'array' AND EXISTS (SELECT 1 {_EVENT_GAPS})"
    )


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately: the column holds owed measurements and their results,
    written by sessions that have since ended, and DROP COLUMN would purge them."""
