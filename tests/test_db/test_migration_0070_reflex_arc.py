"""Migration 0070 — create the reflex-arc P0 tables.

Verifies full column sets of all three tables, the indexes, idempotency,
the lifecycle/verdict CHECK matrices, the fingerprint UNIQUE constraint,
fresh-canonical parity with ``_tables.py``, and ``down``.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")

_EXPECTED_SIGNAL_COLUMNS = {
    "id",
    "fingerprint",
    "class_key",
    "task_name",
    "subsystem",
    "error_type",
    "last_error_message",
    "traceback_tail",
    "status",
    "occurrence_count",
    "first_seen_at",
    "last_seen_at",
    "reopen_count",
    "reopened_at",
    "muted_until",
    "active_diagnosis_id",
    "diagnose_request_id",
    "fix_request_id",
    "task_id",
    "pr_url",
    "outcome_label",
    "created_at",
    "updated_at",
}

_EXPECTED_DIAGNOSIS_COLUMNS = {
    "id",
    "signal_id",
    "session_id",
    "status",
    "artifact_json",
    "artifact_text",
    "root_cause",
    "fix_plan_summary",
    "blast_radius",
    "stated_confidence",
    "predicted_success_p",
    "prediction_features",
    "model_used",
    "created_at",
    "completed_at",
}

_EXPECTED_VERDICT_COLUMNS = {
    "id",
    "signal_id",
    "diagnosis_id",
    "verdict_point",
    "verdict",
    "resolved_by",
    "approval_request_id",
    "context_snapshot",
    "created_at",
}


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _insert_signal(db: aiosqlite.Connection, **overrides) -> None:
    row = {
        "id": "sig1",
        "fingerprint": "abcd1234abcd1234",
        "class_key": "KeyErrorxmemory",
        "task_name": "mem-sync",
        "subsystem": "memory",
        "error_type": "KeyError",
        "status": "new",
        "first_seen_at": "2026-07-21T00:00:00+00:00",
        "last_seen_at": "2026-07-21T00:00:00+00:00",
        "created_at": "2026-07-21T00:00:00+00:00",
        "updated_at": "2026-07-21T00:00:00+00:00",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    await db.execute(f"INSERT INTO reflex_signals ({cols}) VALUES ({marks})", list(row.values()))


async def _insert_verdict(db: aiosqlite.Connection, **overrides) -> None:
    row = {
        "id": "v1",
        "signal_id": "sig1",
        "verdict_point": "diagnose_card",
        "verdict": "execute",
        "resolved_by": "telegram:button:1",
        "context_snapshot": "{}",
        "created_at": "2026-07-21T00:00:00+00:00",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    await db.execute(f"INSERT INTO reflex_verdicts ({cols}) VALUES ({marks})", list(row.values()))


@pytest.mark.asyncio
async def test_up_creates_all_tables_with_full_column_sets(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        assert await _columns(db, "reflex_signals") == _EXPECTED_SIGNAL_COLUMNS
        assert await _columns(db, "reflex_diagnoses") == _EXPECTED_DIAGNOSIS_COLUMNS
        assert await _columns(db, "reflex_verdicts") == _EXPECTED_VERDICT_COLUMNS


@pytest.mark.asyncio
async def test_up_creates_indexes(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_reflex%'"
        )
        names = {row[0] for row in await cur.fetchall()}
    assert names == {
        "idx_reflex_signals_status",
        "idx_reflex_signals_class",
        "idx_reflex_diagnoses_signal",
        "idx_reflex_verdicts_signal",
    }


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        await M70.up(db)  # second run must not raise
        await _insert_signal(db)


@pytest.mark.asyncio
async def test_fingerprint_unique(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        await _insert_signal(db)
        with pytest.raises(aiosqlite.IntegrityError):
            await _insert_signal(db, id="sig2")  # same fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "bogus"},
    ],
)
async def test_signal_check_rejects(tmp_path, overrides):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        with pytest.raises(aiosqlite.IntegrityError):
            await _insert_signal(db, **overrides)


@pytest.mark.asyncio
async def test_signal_lifecycle_statuses_accepted(tmp_path):
    statuses = [
        "new",
        "carded_diagnose",
        "diagnosing",
        "diagnose_failed",
        "diagnosed",
        "carded_fix",
        "fix_dispatched",
        "fix_failed",
        "pr_open",
        "merged",
        "resolved",
        "dismissed_notbug",
        "dismissed_wontfix",
        "card_expired",
    ]
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        for i, status in enumerate(statuses):
            await _insert_signal(db, id=f"s{i}", fingerprint=f"fp{i:014d}", status=status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"verdict_point": "bogus"},
        {"verdict": "bogus"},
    ],
)
async def test_verdict_check_rejects(tmp_path, overrides):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        await _insert_signal(db)
        with pytest.raises(aiosqlite.IntegrityError):
            await _insert_verdict(db, **overrides)


@pytest.mark.asyncio
async def test_fresh_canonical_parity(tmp_path):
    """_tables.py and the migration must build identical column sets."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["reflex_signals"])
        await db.execute(TABLES["reflex_diagnoses"])
        await db.execute(TABLES["reflex_verdicts"])
        fresh = {
            t: await _columns(db, t)
            for t in ("reflex_signals", "reflex_diagnoses", "reflex_verdicts")
        }
    async with aiosqlite.connect(str(tmp_path / "m.db")) as db:
        await M70.up(db)
        migrated = {
            t: await _columns(db, t)
            for t in ("reflex_signals", "reflex_diagnoses", "reflex_verdicts")
        }
    assert fresh == migrated
    assert fresh["reflex_signals"] == _EXPECTED_SIGNAL_COLUMNS
    assert fresh["reflex_diagnoses"] == _EXPECTED_DIAGNOSIS_COLUMNS
    assert fresh["reflex_verdicts"] == _EXPECTED_VERDICT_COLUMNS


@pytest.mark.asyncio
async def test_down_drops_all_tables(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M70.up(db)
        await M70.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('reflex_signals','reflex_diagnoses','reflex_verdicts')"
        )
        assert await cur.fetchall() == []
