"""Add entity_adjudications — the entity-node merge-vs-distinct decision ledger.

One row per fuzzy entity PAIR that the ``entity_adjudication`` drainer judged.
This is NOT ``entity_resolution_audit`` (that trails memory-pair dedup); this
ledger records whether two ENTITY NODES are the same real-world thing.

Design notes:
- ``pair_key`` is order-independent (sorted entity-id pair joined by '|') with a
  UNIQUE constraint — the dedup key the producer (``enqueue_adjudication``) never
  had, so ``(A,B)`` and ``(B,A)`` collapse to one verdict.
- ``verdict``: 'distinct' (keep both), 'merge' (applied — loser tombstoned via
  ``merge_entity``), 'proposed_merge' (propose_only shadow: recorded, NOT applied
  — applied later on the flip to live), 'stale' (a proposal that no longer holds
  because one side merged/renamed/went away since it was recorded).
- ``norm_*``/``updated_*`` snapshot the two entities at decision time; the
  propose_only→live apply pass uses them as a staleness guard (re-adjudicate,
  never blindly apply, if identity drifted).
- ``provider`` is the deciding LLM provider, or 'mechanical' for a digit-guard
  distinct verdict (zero LLM cost).

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_adjudications (
            id           TEXT PRIMARY KEY,
            pair_key     TEXT NOT NULL UNIQUE,
            entity_a     TEXT NOT NULL,
            entity_b     TEXT NOT NULL,
            loser_id     TEXT,
            survivor_id  TEXT,
            verdict      TEXT NOT NULL CHECK (verdict IN (
                'merge','distinct','proposed_merge','stale'
            )),
            reasoning    TEXT,
            provider     TEXT,
            mode         TEXT,
            norm_a       TEXT,
            norm_b       TEXT,
            updated_a    TEXT,
            updated_b    TEXT,
            created_at   TEXT NOT NULL,
            applied_at   TEXT
        )
        """
    )
    # Hot query: the propose_only→live backlog scan (verdict='proposed_merge').
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_adjud_verdict ON entity_adjudications(verdict)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS entity_adjudications")
