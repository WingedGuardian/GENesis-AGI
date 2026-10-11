"""Adjudication-policy stamp on entity_adjudications (MW-3 PR-2b).

Adds a nullable ``policy TEXT`` column recording which prompt policy produced
each verdict. Existing rows stay NULL — meaning "judged under the pre-Option-1
sub-item-vs-parent prompt" — and ``settled_pair_keys`` excludes NULL-policy
``distinct`` rows so the reconcile sweep re-opens exactly that class under the
same-referent policy. ``record_verdict`` stamps the current POLICY_VERSION on
every write (insert AND conflict-update), so the re-open is self-limiting.

Additive + idempotent (PRAGMA-guarded). DDL mirrored in ``db/schema/_tables.py``
(the fresh-install path). Individual ``db.execute()`` calls, no commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_adjudications'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(entity_adjudications)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "policy" not in cols:
            await db.execute("ALTER TABLE entity_adjudications ADD COLUMN policy TEXT")
