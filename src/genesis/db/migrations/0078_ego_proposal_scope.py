"""Add ``ego_proposals.scope`` / ``scope_revision`` + ``ego_proposal_revisions.scope``.

The scope stamp (``operate`` | ``develop``, NULL = unjudged) is the structural
carrier of the operate-vs-develop boundary for the Genesis (COO) ego: the realist
and reconcile LLM stages judge each proposal's scope, code persists it pinned to
``revision_num`` (``scope_revision``), and deterministic chokepoints (create,
revise, dispatch-claim, digest) enforce it. This replaces the earlier regex-marker
gate — semantic classification belongs to the model, structural enforcement to
code.

Columns are nullable/dark until the emitting/enforcing code ships; adding them
here is inert. ``scope_revision`` lets a re-revise detect a stale stamp (scope
judged against an older revision than the current content).

Backfill: existing PENDING genesis-ego proposals that already passed the realist
(``realist_verdict='pass'``) are stamped ``scope='operate',
scope_revision=revision_num`` — the realist rubric already rejects develop work,
so a passed genesis-ego pending row is operate by construction, and grandfathering
it avoids the enforce layer dropping legitimate in-flight work as "unjudged".
User-ego rows are never scoped (scope is genesis-ego-only) and are left NULL.
(This install currently has zero genesis-ego pending rows; other installs may
differ — the backfill is written for the general case.)

Self-contained + idempotent: each ADD is PRAGMA-guarded (applies via the base
path in ``_migrations.py::_migrate_add_columns`` OR the standalone numbered
runner; whichever ran first, the guard skips). Backfill is idempotent (only
touches ``scope IS NULL`` rows). No commit — the runner owns the transaction.

(Numbered 0078: 0077 is the highest present; the runner applies by per-id
tracking and only duplicate prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not await cursor.fetchone():
        return  # fresh DB — CREATE TABLE already carries the column
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "ego_proposals", "scope", "TEXT")
    await _add_column(db, "ego_proposals", "scope_revision", "INTEGER")
    await _add_column(db, "ego_proposal_revisions", "scope", "TEXT")

    # Grandfather passed genesis-ego pending rows as operate (see module docstring).
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        await db.execute(
            "UPDATE ego_proposals SET scope = 'operate', "
            "scope_revision = COALESCE(revision_num, 1) "
            "WHERE scope IS NULL AND status = 'pending' "
            "AND ego_source = 'genesis_ego_cycle' "
            "AND realist_verdict = 'pass'"
        )


async def down(db: aiosqlite.Connection) -> None:
    for table, col in (
        ("ego_proposals", "scope"),
        ("ego_proposals", "scope_revision"),
        ("ego_proposal_revisions", "scope"),
    ):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if not await cursor.fetchone():
            continue
        cursor = await db.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cursor.fetchall()}
        if col in cols:
            await db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
