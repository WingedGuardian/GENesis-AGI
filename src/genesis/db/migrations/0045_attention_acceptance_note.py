"""Add ``attention_events.acceptance_note`` — the reviewer's optional one-line WHY.

The attention shadow-review shifts from *labeling to tune trigger weights* (retired: the
perk decision is an LLM judgment, not a tunable gate) to *reviewing the L1.5 judge*. The
reviewer's REASONING — why a moment was or wasn't worth attention — is the point of that
review, so a labeled row can now carry a short free-text note beside its should/shouldnt/skip.

The runner NEVER writes this column (it is deliberately kept out of ``crud.attention.COLUMNS``,
so the label-preserving upsert can't touch it — human input protected by construction, like
``acceptance_signal``). Fresh/test DBs get it from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB upgrade path.
Idempotent: PRAGMA-guarded ADD COLUMN. No commit — the runner owns the transaction.
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
        if "acceptance_note" not in cols:
            await db.execute("ALTER TABLE attention_events ADD COLUMN acceptance_note TEXT")
