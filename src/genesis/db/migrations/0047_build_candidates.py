"""Add build_candidates — the autonomous capability-build lane's decision ledger.

One row per (notepad item, verdict episode): Genesis's verdict on a dropped
capability request (build / dont_build / needs_discussion) plus everything the
lane needs downstream — the pinned build spec, the materialized plan path, the
greenlight approval reference, the user's actual decision (calibration ground
truth), and the build outcome (task, branch, PR, scope-gate result).

Calibration is the point: every verdict row is scored against ``user_decision``
so Stage-2 graduation (auto-submit per verdict class) is a USER call made on
evidence, never an auto-promotion. ``dont_build`` items get a row and a report
line, never a queue entry; ``needs_discussion`` items get BOTH a row and the
normal follow-up.

The partial unique index (one OPEN candidate per ``item_key``) is the rescan
guard: re-evaluating an unchanged notepad can never spawn a second greenlight
card for an item the user hasn't decided yet.

Additive + idempotent; canonical DDL mirrored in ``db/schema/_tables.py`` for
the fresh-DB path. Individual ``db.execute()`` calls (never ``executescript``).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS build_candidates (
            id                  TEXT PRIMARY KEY,
            item_key            TEXT NOT NULL,      -- sha256 of normalized primary URL/title (monitor normalization)
            item_title          TEXT NOT NULL,
            source_file         TEXT NOT NULL,      -- notepad the item was dropped in
            batch_id            TEXT,               -- inbox batch that produced the verdict
            eval_path           TEXT,               -- evaluation output document
            verdict             TEXT NOT NULL CHECK (
                verdict IN ('build', 'dont_build', 'needs_discussion')
            ),
            verdict_reason      TEXT,               -- articulable grounds (required for dont_build)
            confidence          TEXT,               -- as emitted by the eval (low|medium|high)
            build_spec          TEXT,               -- JSON: requirements/steps/success_criteria/risks/intended_paths
            plan_path           TEXT,               -- materialized TASK_INTAKE-format plan
            approval_request_id TEXT,               -- greenlight card (approval_requests.id)
            user_decision       TEXT CHECK (
                user_decision IN ('approved', 'rejected', 'discussed')
            ),                                      -- NULL = still open (undecided)
            decided_at          TEXT,
            task_id             TEXT,               -- task_states row once submitted
            branch              TEXT,
            pr_url              TEXT,
            outcome             TEXT NOT NULL DEFAULT 'pending' CHECK (
                outcome IN ('pending', 'submitted', 'built', 'pr_opened',
                            'scope_blocked', 'build_failed', 'abandoned')
            ),
            scope_gate_result   TEXT,               -- JSON verdict from the diff allowlist gate
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Rescan guard: at most ONE open (undecided) candidate per item.
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_build_candidates_open_item "
        "ON build_candidates(item_key) WHERE user_decision IS NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_build_candidates_outcome "
        "ON build_candidates(outcome)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_build_candidates_created "
        "ON build_candidates(created_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS build_candidates")
