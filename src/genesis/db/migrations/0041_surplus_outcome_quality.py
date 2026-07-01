"""Add outcome_quality column to surplus_tasks (verified-correctness verdict).

``outcome_quality`` records whether an insight-producing surplus task's output
was actually *useful* — not just whether it *ran*. A terminal ``status`` of
``completed`` only means "the work ran to completion"; it says nothing about
whether the produced insight survived the intake quality gate. This column
captures that second axis so the Outcome Bus carries discriminative negatives.

Values (set only for ``surplus.types.INSIGHT_PRODUCING_TASK_TYPES``):
  - ``useful`` : intake routed >=1 finding to knowledge/observations.
  - ``hollow`` : intake ran but routed everything to discard (ran, produced
                 nothing of value) -> harvested as a VERIFICATION_FAILED negative.
  - ``NULL``   : action task, legacy row, intake infra-failure, or empty/
                 too-short output. NULL keeps the existing positive-only
                 behaviour (no retroactive flip; the harvester treats NULL like
                 a plain completion).

Additive and backward-compatible: existing rows backfill to NULL. The harvester
(feedback/harvest.py) keeps emitting the EXECUTION_OUTCOME positive for every
completed task unchanged, and only ADDS a VERIFICATION_FAILED row when
``outcome_quality = 'hollow'``.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Table may not exist if migrations run on a fresh DB before schema init.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='surplus_tasks'"
    )
    if not await cursor.fetchone():
        return

    # Only add column if it doesn't already exist (fresh DBs get it from the
    # canonical CREATE TABLE in db/schema/_tables.py).
    col_cursor = await db.execute("PRAGMA table_info(surplus_tasks)")
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "outcome_quality" not in cols:
        await db.execute(
            "ALTER TABLE surplus_tasks "
            "ADD COLUMN outcome_quality TEXT "
            "CHECK (outcome_quality IN ('useful', 'hollow'))"
        )
