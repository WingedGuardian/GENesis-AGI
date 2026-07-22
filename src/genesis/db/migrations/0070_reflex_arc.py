"""Add reflex-arc P0 tables — signals, diagnoses, verdicts (taste corpus).

The reflex arc's afferent store (spec:
``docs/superpowers/specs/2026-07-21-reflex-arc-design.md``):

- ``reflex_signals`` — one row per failure fingerprint (task.failed events,
  upsert-deduped with an occurrence count), carrying the full Tier-0
  lifecycle in ``status``. Later-phase columns (fix ids, outcome_label)
  ship now so the lifecycle CHECK never needs a rebuild migration.
- ``reflex_diagnoses`` — one row per Tier-0 diagnose session round
  (written from PR2 onward).
- ``reflex_verdicts`` — the taste corpus: every human verdict as a
  self-contained (context, judgment) example. Never pruned by design
  (spec §9); non-human rows (timeout expiry) filter via ``resolved_by``.

Fresh/test DBs get the tables from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. No commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS reflex_signals (
            id                  TEXT PRIMARY KEY,
            fingerprint         TEXT NOT NULL UNIQUE,
            class_key           TEXT NOT NULL,
            task_name           TEXT NOT NULL,
            subsystem           TEXT NOT NULL,
            error_type          TEXT NOT NULL,
            last_error_message  TEXT,
            traceback_tail      TEXT,
            status              TEXT NOT NULL DEFAULT 'new'
                                  CHECK (status IN (
                                    'new','carded_diagnose','diagnosing','diagnose_failed',
                                    'diagnosed','carded_fix','fix_dispatched','fix_failed',
                                    'pr_open','merged','resolved',
                                    'dismissed_notbug','dismissed_wontfix','card_expired')),
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            reopen_count        INTEGER NOT NULL DEFAULT 0,
            reopened_at         TEXT,
            muted_until         TEXT,
            active_diagnosis_id TEXT,
            diagnose_request_id TEXT,
            fix_request_id      TEXT,
            task_id             TEXT,
            pr_url              TEXT,
            outcome_label       TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS reflex_diagnoses (
            id                  TEXT PRIMARY KEY,
            signal_id           TEXT NOT NULL,
            session_id          TEXT,
            status              TEXT NOT NULL DEFAULT 'running'
                                  CHECK (status IN ('running','completed','failed','unparseable')),
            artifact_json       TEXT,
            artifact_text       TEXT,
            root_cause          TEXT,
            fix_plan_summary    TEXT,
            blast_radius        TEXT,
            stated_confidence   REAL,
            predicted_success_p REAL,
            prediction_features TEXT,
            model_used          TEXT,
            created_at          TEXT NOT NULL,
            completed_at        TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS reflex_verdicts (
            id                  TEXT PRIMARY KEY,
            signal_id           TEXT NOT NULL,
            diagnosis_id        TEXT,
            verdict_point       TEXT NOT NULL
                                  CHECK (verdict_point IN
                                    ('diagnose_card','fix_card','pr_merge','promotion')),
            verdict             TEXT NOT NULL
                                  CHECK (verdict IN (
                                    'execute','dismiss_notbug','dismiss_wontfix',
                                    'expired','merged','closed_unmerged')),
            resolved_by         TEXT NOT NULL,
            approval_request_id TEXT,
            context_snapshot    TEXT NOT NULL,
            created_at          TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflex_signals_status ON reflex_signals(status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflex_signals_class ON reflex_signals(class_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflex_diagnoses_signal "
        "ON reflex_diagnoses(signal_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflex_verdicts_signal "
        "ON reflex_verdicts(signal_id, created_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS reflex_verdicts")
    await db.execute("DROP TABLE IF EXISTS reflex_diagnoses")
    await db.execute("DROP TABLE IF EXISTS reflex_signals")
