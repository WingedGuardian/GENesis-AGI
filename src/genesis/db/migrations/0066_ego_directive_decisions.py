"""Extend ``ego_directives`` with decision rows — durable user rulings.

Root cause (2026-07-17 investigation): a user decision resolved through ANY
proposal path had no durable, ego-visible representation. The four
resolution entry points had asymmetric side effects — only Telegram wrote a
correction memory, and NOTHING wrote an artifact the ego context builders
actually render — so the ego re-litigated settled decisions (the OBA
recurrence) despite explicit user rulings.

``kind`` distinguishes the two row types sharing this table:

- ``'directive'`` (default) — existing behavior: user guidance the ego
  factors in and may mark completed.
- ``'decision'`` — a user RULING captured at proposal-rejection time (or via
  the ``ego_decision`` MCP tool). Constraints, not signals: rendered in an
  always-on context section, deduped/reaffirmed on repeat, superseded only
  by the user. ``resolve_directive`` refuses to touch them (the ego cannot
  "complete" a decision).

``source_proposal_id`` links a decision to the rejection that created it.
``reaffirm_count``/``last_reaffirmed_at`` track repeat rulings on the same
theme instead of duplicating rows.

Fresh/test DBs get the columns from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: PRAGMA-guarded ADD COLUMN. No commit — the runner
owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_directives'"
    )
    if not await cursor.fetchone():
        return
    col_cursor = await db.execute("PRAGMA table_info(ego_directives)")
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "kind" not in cols:
        await db.execute(
            "ALTER TABLE ego_directives "
            "ADD COLUMN kind TEXT NOT NULL DEFAULT 'directive' "
            "CHECK (kind IN ('directive', 'decision'))"
        )
    if "source_proposal_id" not in cols:
        await db.execute("ALTER TABLE ego_directives ADD COLUMN source_proposal_id TEXT")
    if "reaffirm_count" not in cols:
        await db.execute(
            "ALTER TABLE ego_directives ADD COLUMN reaffirm_count INTEGER NOT NULL DEFAULT 0"
        )
    if "last_reaffirmed_at" not in cols:
        await db.execute("ALTER TABLE ego_directives ADD COLUMN last_reaffirmed_at TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_directives_kind_status ON ego_directives(kind, status)"
    )
