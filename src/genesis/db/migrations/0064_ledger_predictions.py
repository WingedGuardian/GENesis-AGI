"""Add ledger_predictions — the WS-2 Cognitive Ledger substrate (P1a).

One row per falsifiable prediction: "proposition P about subject S will be
TRUE by deadline D, with probability c." The three-gate design makes
unmeasurable predictions structurally unwritable:

1. The CRUD writer (``db/crud/ledger_predictions.py``) refuses any metric not
   in the code registry (``genesis/ledger/metrics.py``), any comparator/
   threshold pairing the metric spec disallows, any deadline outside
   ``(now, now + HORIZON_CAP]``, any confidence outside [0.01, 0.99].
2. The table CHECKs below are defense-in-depth against raw-SQL writers.
3. The grader (P2) resolves rows whose metric has vanished from the registry
   to ``unresolvable`` and alarms — schema-vs-code drift is a sensor, never
   a silent skip.

``rationale`` is prose for humans and is NEVER graded. The UNIQUE key makes
writer hooks idempotent under retries/resends. The partial index carries the
grader's hot query (open rows past deadline).

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger_predictions (
            id               TEXT PRIMARY KEY,                     -- uuid4 hex16
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            action_class     TEXT NOT NULL CHECK (action_class IN
                               ('outreach_send','task_execution','scheduled_job',
                                'build_verdict','ego_proposal')),
            subject_ref_type TEXT NOT NULL,      -- 'outreach' | 'task' | 'job_day' | ...
            subject_ref_id   TEXT NOT NULL,      -- outreach_history.id / task_id / '<job>:<YYYY-MM-DD>' / ...
            domain           TEXT NOT NULL,      -- dotted coarse domain: 'outreach.<category>', 'task.<type>', 'job.<name>'
            metric           TEXT NOT NULL,      -- MUST exist in the code registry; grader refuses unknown metrics
            comparator       TEXT NOT NULL DEFAULT 'is_true'
                               CHECK (comparator IN ('is_true','le','ge')),
            threshold        REAL,               -- required iff comparator != 'is_true'
            confidence       REAL NOT NULL CHECK (confidence >= 0.01 AND confidence <= 0.99),
            deadline_at      TEXT NOT NULL,      -- ISO-8601 UTC; writer enforces now < deadline <= now + horizon cap
            provenance       TEXT NOT NULL CHECK (provenance IN ('stated','policy_prior')),
            predictor        TEXT NOT NULL,      -- component id: 'outreach_pipeline','task_executor',...
            source_session   TEXT,
            rationale        TEXT,               -- optional prose; NEVER graded
            status           TEXT NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open','resolved','fuzzy_pending',
                                                 'fuzzy_resolved','void','unresolvable')),
            outcome_value    INTEGER CHECK (outcome_value IN (0,1)),
            resolved_at      TEXT,
            resolver         TEXT CHECK (resolver IN ('mechanical','mechanical_absence',
                                                      'llm_fallback','user')),
            evidence_ref     TEXT,               -- 'table:rowid' of the grading evidence
            brier            REAL,               -- (confidence - outcome_value)^2, set at grade time
            metadata         TEXT,               -- JSON
            CHECK ((comparator = 'is_true') = (threshold IS NULL)),
            UNIQUE (action_class, subject_ref_id, metric)
        )
        """
    )
    # the grader's hot query: open rows whose deadline has passed
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_open_deadline "
        "ON ledger_predictions(deadline_at) WHERE status = 'open'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_domain "
        "ON ledger_predictions(domain, action_class, metric)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_lp_status ON ledger_predictions(status)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_subject "
        "ON ledger_predictions(subject_ref_type, subject_ref_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS ledger_predictions")
