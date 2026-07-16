"""WS-2 M10 — the awareness-tick alert_events open-set writer.

_persist_health_alerts is the single designated writer that turns the live
(recomputed-each-poll) health alert set into a durable incident log. These tests
drive the firing set by monkeypatching the pure _compute_alerts() and assert the
open-set reconcile: open on fire, resolve on clear, survive a fresh connection,
and never raise into the tick.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.awareness.loop import _persist_health_alerts

_CREATE_ALERT_EVENTS = """
    CREATE TABLE IF NOT EXISTS alert_events (
        id           TEXT PRIMARY KEY,
        alert_id     TEXT NOT NULL,
        source       TEXT NOT NULL,
        severity     TEXT NOT NULL,
        message      TEXT NOT NULL,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at  TEXT
    )
"""
_CREATE_OPEN_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ae_open_alert "
    "ON alert_events(alert_id) WHERE resolved_at IS NULL"
)


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(_CREATE_ALERT_EVENTS)
    await db.execute(_CREATE_OPEN_IDX)
    await db.commit()
    return db


def _patch_compute(monkeypatch, alerts):
    async def _fake():
        return alerts, {a["id"] for a in alerts}

    monkeypatch.setattr("genesis.mcp.health.errors._compute_alerts", _fake)


async def _open_count(db) -> int:
    async with db.execute("SELECT COUNT(*) FROM alert_events WHERE resolved_at IS NULL") as cur:
        return (await cur.fetchone())[0]


async def test_firing_alert_opens_row(monkeypatch):
    db = await _setup()
    try:
        _patch_compute(
            monkeypatch,
            [{"id": "call_site:groq", "severity": "CRITICAL", "message": "down"}],
        )
        await _persist_health_alerts(db)
        async with db.execute(
            "SELECT alert_id, source, severity, resolved_at FROM alert_events"
        ) as cur:
            rows = list(await cur.fetchall())
        assert len(rows) == 1
        assert rows[0]["alert_id"] == "call_site:groq"
        assert rows[0]["source"] == "call_site"  # derived from the id prefix
        assert rows[0]["resolved_at"] is None
    finally:
        await db.close()


async def test_repeated_fire_is_idempotent(monkeypatch):
    db = await _setup()
    try:
        _patch_compute(
            monkeypatch,
            [{"id": "creds:corrupt", "severity": "CRITICAL", "message": "x"}],
        )
        await _persist_health_alerts(db)
        await _persist_health_alerts(db)  # same alert still firing → no 2nd open row
        assert await _open_count(db) == 1
    finally:
        await db.close()


async def test_cleared_alert_is_resolved(monkeypatch):
    db = await _setup()
    try:
        _patch_compute(
            monkeypatch,
            [{"id": "queue:dead_letter", "severity": "WARNING", "message": "y"}],
        )
        await _persist_health_alerts(db)
        assert await _open_count(db) == 1
        # Next tick: nothing firing → the open row must be resolved.
        _patch_compute(monkeypatch, [])
        await _persist_health_alerts(db)
        assert await _open_count(db) == 0
        async with db.execute(
            "SELECT resolved_at FROM alert_events WHERE alert_id = 'queue:dead_letter'"
        ) as cur:
            assert (await cur.fetchone())["resolved_at"] is not None
    finally:
        await db.close()


async def test_refire_after_resolve_opens_new_incident(monkeypatch):
    db = await _setup()
    try:
        alert = [{"id": "creds:corrupt", "severity": "CRITICAL", "message": "x"}]
        _patch_compute(monkeypatch, alert)
        await _persist_health_alerts(db)
        _patch_compute(monkeypatch, [])
        await _persist_health_alerts(db)  # resolve
        _patch_compute(monkeypatch, alert)
        await _persist_health_alerts(db)  # re-fire → distinct incident row
        async with db.execute(
            "SELECT COUNT(*) FROM alert_events WHERE alert_id = 'creds:corrupt'"
        ) as cur:
            assert (await cur.fetchone())[0] == 2
        assert await _open_count(db) == 1
    finally:
        await db.close()


async def test_none_db_is_noop():
    # Must not raise when the DB is unavailable (tick still runs).
    await _persist_health_alerts(None)


async def test_compute_failure_never_raises(monkeypatch):
    db = await _setup()
    try:

        async def _boom():
            raise RuntimeError("health snapshot exploded")

        monkeypatch.setattr("genesis.mcp.health.errors._compute_alerts", _boom)
        # Best-effort: a compute failure must be swallowed, not propagated.
        await _persist_health_alerts(db)
        assert await _open_count(db) == 0
    finally:
        await db.close()


pytestmark = pytest.mark.asyncio
