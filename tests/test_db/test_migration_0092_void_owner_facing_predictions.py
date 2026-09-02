"""Migration 0092 — void historical owner-facing outreach predictions.

The outreach ledger hook wrote reply_received/positive_engagement predictions for every
delivered send, including owner-facing pings (Telegram/voice), which never get an external
reply and so pin the recomputed reply-rate calibration near 0%. The migration sets
status='void' for outreach_send predictions whose originating outreach_history.channel is
owner-facing (telegram/voice); calibration_cells recompute from resolved rows only, so
voided rows drop out automatically. External-channel predictions (email/discord) are
untouched. Idempotent; table-absent-safe.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M90 = importlib.import_module("genesis.db.migrations.0092_void_owner_facing_outreach_predictions")


async def _build(conn: aiosqlite.Connection) -> None:
    # Minimal shapes — just the columns the migration reads/writes (cf. 0056's approach).
    await conn.execute("CREATE TABLE outreach_history (id TEXT PRIMARY KEY, channel TEXT NOT NULL)")
    await conn.execute(
        "CREATE TABLE ledger_predictions ("
        "  id TEXT PRIMARY KEY, action_class TEXT NOT NULL, subject_ref_id TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open', resolved_at TEXT,"
        "  outcome_value INTEGER, resolver TEXT, brier REAL, evidence_ref TEXT)"
    )
    # Derived calibration surfaces (minimal shapes) — the migration invalidates the
    # outreach_send rows and leaves other action_classes untouched.
    await conn.execute(
        "CREATE TABLE calibration_cells (domain TEXT, action_class TEXT, metric TEXT, "
        "provenance TEXT, window_days INTEGER, n INTEGER, brier REAL)"
    )
    await conn.execute(
        "CREATE TABLE calibration_cell_history (id TEXT PRIMARY KEY, domain TEXT, "
        "action_class TEXT, metric TEXT, snapshot_at TEXT)"
    )
    await conn.execute(
        "INSERT INTO calibration_cells VALUES "
        "('outreach.notification','outreach_send','reply_received','all',0,100,0.9),"
        "('task.user','task_execution','completed','all',0,50,0.1)"
    )
    await conn.execute(
        "INSERT INTO calibration_cell_history VALUES "
        "('h1','outreach.notification','outreach_send','reply_received','2026-08-01T00:00:00Z'),"
        "('h2','task.user','task_execution','completed','2026-08-01T00:00:00Z')"
    )
    for oid, ch in [
        ("oh-tg", "telegram"),
        ("oh-voice", "voice"),
        ("oh-email", "email"),
        ("oh-discord", "discord"),
    ]:
        await conn.execute("INSERT INTO outreach_history VALUES (?, ?)", (oid, ch))
    rows = [
        # (id, action_class, subject_ref_id, status, resolved_at, outcome_value, resolver, brier)
        ("lp-tg-open", "outreach_send", "oh-tg", "open", None, None, None, None),
        # a GRADED owner-facing row: voiding must ALSO clear outcome_value/resolver/brier
        # (else a void row carries a stale grade — the invariant the CRUD writer forbids).
        (
            "lp-tg-resolved",
            "outreach_send",
            "oh-tg",
            "resolved",
            "2026-08-01T00:00:00Z",
            0,
            "mechanical_absence",
            0.04,
        ),
        ("lp-voice", "outreach_send", "oh-voice", "open", None, None, None, None),
        (
            "lp-email",
            "outreach_send",
            "oh-email",
            "open",
            None,
            None,
            None,
            None,
        ),  # external → keep
        # external graded row → fully untouched (grade preserved).
        (
            "lp-discord",
            "outreach_send",
            "oh-discord",
            "resolved",
            "2026-08-02T00:00:00Z",
            1,
            "mechanical",
            0.01,
        ),
        (
            "lp-already-void",
            "outreach_send",
            "oh-tg",
            "void",
            None,
            None,
            None,
            None,
        ),  # idempotent no-op
    ]
    for r in rows:
        await conn.execute(
            "INSERT INTO ledger_predictions "
            "(id, action_class, subject_ref_id, status, resolved_at, outcome_value, resolver, brier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    # A graded row carries an evidence_ref; voiding must clear the owner-facing one and
    # preserve the external one.
    await conn.execute(
        "UPDATE ledger_predictions SET evidence_ref='outreach_history:oh-tg' WHERE id='lp-tg-resolved'"
    )
    await conn.execute(
        "UPDATE ledger_predictions SET evidence_ref='outreach_history:oh-discord' WHERE id='lp-discord'"
    )
    await conn.commit()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await _build(conn)
        yield conn


async def _status(db, pid):
    cur = await db.execute(
        "SELECT status, resolved_at, outcome_value, resolver, brier, evidence_ref "
        "FROM ledger_predictions WHERE id=?",
        (pid,),
    )
    return await cur.fetchone()


@pytest.mark.asyncio
async def test_voids_owner_facing_keeps_external(db):
    await M90.up(db)
    # Owner-facing (telegram/voice) → void.
    assert (await _status(db, "lp-tg-open"))["status"] == "void"
    assert (await _status(db, "lp-voice"))["status"] == "void"
    r = await _status(db, "lp-tg-resolved")
    assert r["status"] == "void"
    assert r["resolved_at"] == "2026-08-01T00:00:00Z"  # existing resolved_at preserved (COALESCE)
    # A voided-from-resolved row CLEARS its grade AND evidence_ref, so it satisfies the
    # ledger invariant "outcome_value non-None iff status='resolved'" (no void row carrying
    # a stale grade or a dangling evidence pointer).
    assert r["outcome_value"] is None and r["resolver"] is None and r["brier"] is None
    assert r["evidence_ref"] is None
    # An open row being voided gets a resolved_at stamp (was NULL).
    assert (await _status(db, "lp-tg-open"))["resolved_at"] is not None
    # External channels (email/discord) → fully untouched, grade + evidence preserved.
    assert (await _status(db, "lp-email"))["status"] == "open"
    ext = await _status(db, "lp-discord")
    assert (
        ext["status"] == "resolved"
        and ext["outcome_value"] == 1
        and ext["resolver"] == "mechanical"
        and ext["evidence_ref"] == "outreach_history:oh-discord"
    )


@pytest.mark.asyncio
async def test_invalidates_derived_outreach_calibration(db):
    # The migration also purges the DERIVED calibration data for action_class='outreach_send'
    # (cells + trend history) so the fix is immediate, not deferred to the next grader
    # recompute. Other action_classes are left intact.
    await M90.up(db)
    cur = await db.execute("SELECT action_class FROM calibration_cells ORDER BY action_class")
    assert [r["action_class"] for r in await cur.fetchall()] == ["task_execution"]
    cur = await db.execute("SELECT id FROM calibration_cell_history ORDER BY id")
    assert [r["id"] for r in await cur.fetchall()] == [
        "h2"
    ]  # only the non-outreach snapshot remains


@pytest.mark.asyncio
async def test_idempotent(db):
    await M90.up(db)
    cur = await db.execute("SELECT id, status, resolved_at FROM ledger_predictions ORDER BY id")
    first = [tuple(r) for r in await cur.fetchall()]
    await M90.up(db)  # WHERE status != 'void' → second run is a no-op
    cur = await db.execute("SELECT id, status, resolved_at FROM ledger_predictions ORDER BY id")
    assert [tuple(r) for r in await cur.fetchall()] == first


@pytest.mark.asyncio
async def test_absent_tables_safe(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "empty.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await M90.up(conn)  # neither table exists → must not raise
