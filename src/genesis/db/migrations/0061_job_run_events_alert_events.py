"""Add job_run_events + alert_events — the WS-2 sensor fabric (M9/M10).

WS-2 PR-0 repairs the observability substrate the Cognitive Ledger grades
against, before the ledger itself is built.

- ``job_run_events`` (M9): per-run scheduled-job history. Today ``job_health``
  is cumulative-only (one UPSERT row per ``job_name``), so there is no per-run
  time series and no era attribution — a job that failed for a week then
  recovered looks identical to one that never failed. The ledger's
  ``scheduled_job`` predictions (``runs_clean_day`` / ``runtime_ms_le``) grade
  from this table. Writes are debounced at the source
  (``runtime/_job_health.py``): a success row only when ≥1h since the job's
  last success; a failure row on streak onset + hourly heartbeat during a
  sustained outage — so a stuck 60s poll costs ~24 rows/day, not ~1440.

- ``alert_events`` (M10): a persisted alert/incident store. Today the only
  alert memory is the in-memory ``_alert_history`` dict
  (``mcp/health/__init__.py``), a per-process one-generation resolved tracker
  that does not survive restart and cannot be read as history. The awareness
  tick reconciles a durable open-set here: one open row (``resolved_at IS
  NULL``) per currently-firing alert; resolution stamps ``resolved_at``. The
  partial UNIQUE index makes the reconcile idempotent under the cross-process
  race between the runtime and the health-MCP process.

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS job_run_events (
            id             TEXT PRIMARY KEY,          -- uuid4 hex
            job_name       TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            run_started_at TEXT,                      -- NULL unless record_job_start ran
            duration_ms    INTEGER,                   -- NULL unless run_started_at present
            error          TEXT,                      -- failure detail (NULL on success)
            recorded_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_events (
            id           TEXT PRIMARY KEY,            -- uuid4 hex
            alert_id     TEXT NOT NULL,               -- stable alert key (e.g. 'creds:corrupt')
            source       TEXT NOT NULL,               -- component that raised it
            severity     TEXT NOT NULL,               -- CRITICAL / WARNING / ... (computed)
            message      TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at  TEXT                         -- NULL = still open
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jre_job_time ON job_run_events(job_name, recorded_at)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_jre_recorded ON job_run_events(recorded_at)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jre_status ON job_run_events(status, recorded_at)"
    )
    # one open row per alert_id — makes the open-set reconcile idempotent even
    # under the runtime/health-MCP cross-process race
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ae_open_alert "
        "ON alert_events(alert_id) WHERE resolved_at IS NULL"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_ae_created ON alert_events(created_at)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ae_alert ON alert_events(alert_id, created_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS job_run_events")
    await db.execute("DROP TABLE IF EXISTS alert_events")
