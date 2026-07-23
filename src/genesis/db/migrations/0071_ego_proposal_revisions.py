"""Add the ego_proposal_revisions table — prior-value audit trail for versioned
proposal revision (ego proposal-lifecycle redesign, PR-4, dark schema).

One row per superseded proposal revision: when the PR-5 reconcile stage revises
a pending proposal in place, it snapshots the prior (content, rationale,
confidence, execution_plan, expected_outputs) here before overwriting, so age
never launders lineage and calibration can grade per revision. Surrogate id PK,
no FK (mirrors calibration_cell_history; SQLite FK enforcement is PRAGMA-gated,
so proposal_id is a documented soft reference).

Dark in PR-4 — the table exists but has no writer until PR-5. The paired dark
columns on ego_proposals (revision_num, revalidate_at, last_validated_at) are
NOT added here: they are unindexed additive columns handled idempotently by
_migrate_add_columns on the base path every boot. Adding a bare ADD COLUMN here
too would double-add after that runs and raise (the boot-crash class documented
in tests/test_db/test_schema_base_path_parity.py), so this migration is
table-only.

Fresh/test DBs get the table from the canonical CREATE TABLE in
db/schema/_tables.py; this numbered migration covers the existing-DB upgrade
path. Idempotent: CREATE TABLE IF NOT EXISTS. No commit — the runner owns the
transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS ego_proposal_revisions (
            id               TEXT PRIMARY KEY,
            proposal_id      TEXT NOT NULL,
            revision_num     INTEGER NOT NULL,
            content          TEXT,
            rationale        TEXT,
            confidence       REAL,
            execution_plan   TEXT,
            expected_outputs TEXT,
            revised_at       TEXT NOT NULL,
            revised_by       TEXT,
            reason           TEXT
        )"""
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS ego_proposal_revisions")
