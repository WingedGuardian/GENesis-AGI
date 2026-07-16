"""WS-2 M7 injected-failure — user_model_delta stream staleness alarm.

The reflection user-impact path writes deltas to observations(type=
'user_model_delta'); the stream can go silent (it flatlined ~Mar–Jul 2026)
without anything noticing. This un-blinds that: a >14d gap raises a non-paging
'high' infrastructure_alert that auto-resolves when a fresh delta lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness import loop as _loop
from genesis.awareness.loop import (
    _check_user_model_staleness,
    _resolve_user_model_staleness,
)
from genesis.db.crud import observations as obs
from genesis.db.schema._tables import TABLES


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["observations"])
    await db.commit()
    return db


async def _seed_delta(db, *, days_ago: int) -> None:
    await obs.create(
        db,
        id=uuid.uuid4().hex,
        source="reflection",
        type="user_model_delta",
        content="synthetic delta",
        priority="low",
        created_at=(datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
    )


async def _alerts(db) -> list[dict]:
    async with db.execute(
        "SELECT id, priority, resolved FROM observations "
        "WHERE source = 'user_model_staleness_monitor' AND type = 'infrastructure_alert'"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


@pytest.fixture(autouse=True)
def _reset_cooldown():
    _loop._last_user_model_stale_alert_at = 0.0
    yield
    _loop._last_user_model_stale_alert_at = 0.0


async def test_stale_stream_raises_high_alert():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=30)  # well past the 14d threshold
        await _check_user_model_staleness(db)
        alerts = await _alerts(db)
        assert len(alerts) == 1
        assert alerts[0]["priority"] == "high"  # non-paging by design
        assert alerts[0]["resolved"] == 0
    finally:
        await db.close()


async def test_never_any_delta_raises_alert():
    db = await _setup()
    try:
        await _check_user_model_staleness(db)  # empty observations table
        assert len(await _alerts(db)) == 1
    finally:
        await db.close()


async def test_fresh_stream_no_alert():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=3)  # within the window
        await _check_user_model_staleness(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


async def test_fresh_delta_resolves_prior_alert():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=30)
        await _check_user_model_staleness(db)
        before = await _alerts(db)
        assert len(before) == 1 and before[0]["resolved"] == 0

        # A fresh delta arrives; the next check must resolve the standing alert.
        await _seed_delta(db, days_ago=0)
        await _check_user_model_staleness(db)
        alerts = await _alerts(db)
        assert all(a["resolved"] == 1 for a in alerts), "fresh delta must resolve staleness alert"
    finally:
        await db.close()


async def test_none_db_and_resolve_are_noops():
    await _check_user_model_staleness(None)  # must not raise
    await _resolve_user_model_staleness(None)  # must not raise


pytestmark = pytest.mark.asyncio
