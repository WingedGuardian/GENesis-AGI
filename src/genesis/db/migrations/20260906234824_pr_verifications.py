"""Per-merged-PR verification obligations — the durable half of issue #1718.

WHY THIS TABLE EXISTS. "Run the end-to-end after merge" was carried by memory
alone: measured 2026-09-04, one of two owner-directed merges had its E2E
forgotten until the owner asked, and BOTH E2Es found something real once run.
The merge-time declaration (#1808) makes the decision visible but — since the
advisory downgrade (#1824) — nothing durable survives the merge. This table is
the chokepoint: the repo-pulse worker opens one row per merged PR, a
documentation-only diff is auto-closed with the reason recorded (a
DETERMINISTIC exemption by path — no model is ever asked whether a prompt
change needs an E2E), and the Wave-3 validator session closes the rest with
evidence.

WHY NOT ``follow_ups`` (the New-Store Gate justification, recorded where the
store is born). These rows are a machine-written ledger for a validator, not
dispatchable work, and ``follow_ups``' semantics actively mislead its
consumers: measured 2026-09-06, ``status='blocked'`` rows reach the ego
dispatch path via ``get_actionable`` and ``status='pending'`` rows reach the
morning report via ``get_pending`` — so every field written there would be a
lie held in check only by 12+ readers each remembering a ``kind`` filter.
A narrow table with one writer is a chokepoint; a shared table with
per-reader exclusion is a convention. Consistency: single writer (the pulse
worker lane), single closer (the validator / ``close_verification``).
Retention: closed rows pruned >180d via ``scripts/prune_repo_pulse.py``
(disk-hygiene timer); OPEN rows are the obligation and are never pruned.
Backup: rides genesis.db.

Additive and idempotent — CREATE TABLE IF NOT EXISTS, no rebuild. Fresh
installs get the identical DDL from ``schema/_tables.py`` (the sibling build
path; ``tests/test_session_awareness/test_pr_verifications.py`` pins the two
in parity).

The (repo, pr_number) UNIQUE index is the dedup: a re-covered enumeration
window re-observing a merged PR is absorbed by INSERT OR IGNORE, never
duplicated — the guard is the schema, not a convention at the call site.
"""

from __future__ import annotations

import aiosqlite

DDL = """
CREATE TABLE IF NOT EXISTS pr_verifications (
    id            TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    pr_title      TEXT,
    merged_at     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK(status IN ('open', 'closed')),
    closed_reason TEXT,
    closed_at     TEXT,
    evidence      TEXT,
    created_at    TEXT NOT NULL
)
"""

INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_prv_repo_pr ON pr_verifications(repo, pr_number)",
    "CREATE INDEX IF NOT EXISTS idx_prv_status ON pr_verifications(status, merged_at)",
)


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(DDL)
    for ddl in INDEXES:
        await db.execute(ddl)
