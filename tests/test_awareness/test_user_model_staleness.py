"""WS-2 M7 — user_model_delta stream staleness alarm (flap-resistant).

The reflection user-impact path writes deltas to observations(type=
'user_model_delta'). The stream is sparse BY DESIGN (deltas only on genuine
change), so the alarm must not flap: it fires only when the stream is silent
>= 45d DESPITE recent foreground interaction, at most once per silence episode
(DB-backed dedup, immune to the 3-day infrastructure_alert TTL that used to
re-mint it), at 'medium' priority, and auto-resolves when a fresh delta lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness.loop import (
    _USER_MODEL_MIN_INTERACTION_SESSIONS,
    _USER_MODEL_STALE_DAYS,
    _check_user_model_staleness,
    _resolve_user_model_staleness,
)
from genesis.db.crud import observations as obs
from genesis.db.schema._tables import TABLES


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["observations"])
    await db.execute(TABLES["cc_sessions"])
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


async def _seed_sessions(
    db, *, count: int, days_ago: int, session_type: str = "foreground"
) -> None:
    """Seed `count` cc_sessions of `session_type` started `days_ago`."""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    for _ in range(count):
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at) "
            "VALUES (?, ?, 'opus', ?, ?)",
            (uuid.uuid4().hex, session_type, ts, ts),
        )
    await db.commit()


async def _alerts(db) -> list[dict]:
    async with db.execute(
        "SELECT id, priority, resolved, created_at FROM observations "
        "WHERE source = 'user_model_staleness_monitor' AND type = 'infrastructure_alert' "
        "ORDER BY created_at"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# A silence past the threshold, with plenty of interaction, unless a test says otherwise.
_STALE = _USER_MODEL_STALE_DAYS + 15
_ENOUGH = _USER_MODEL_MIN_INTERACTION_SESSIONS + 2


async def test_stale_with_interaction_raises_medium_alert():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_STALE)
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)
        await _check_user_model_staleness(db)
        alerts = await _alerts(db)
        assert len(alerts) == 1
        assert alerts[0]["priority"] == "medium"  # never urgent
        assert alerts[0]["resolved"] == 0
    finally:
        await db.close()


async def test_stale_without_interaction_stays_quiet():
    """Silence during a genuine user absence is expected, not a defect."""
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_STALE)
        # zero foreground sessions since the delta
        await _check_user_model_staleness(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


async def test_stale_below_min_interaction_stays_quiet():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_STALE)
        await _seed_sessions(db, count=_USER_MODEL_MIN_INTERACTION_SESSIONS - 1, days_ago=1)
        await _check_user_model_staleness(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


async def test_within_threshold_no_alert():
    """A gap shorter than the threshold is honest sparsity, even with activity."""
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_USER_MODEL_STALE_DAYS - 5)
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)
        await _check_user_model_staleness(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


async def test_never_any_delta_with_interaction_raises():
    db = await _setup()
    try:
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)  # active install, no deltas ever
        await _check_user_model_staleness(db)
        assert len(await _alerts(db)) == 1
    finally:
        await db.close()


async def test_never_any_delta_without_interaction_stays_quiet():
    db = await _setup()
    try:
        await _check_user_model_staleness(db)  # empty everything
        assert await _alerts(db) == []
    finally:
        await db.close()


async def test_never_any_delta_fires_once_ever():
    """The never-emitted-delta condition is ONE standing episode: a prior alert
    (even resolved, even old) suppresses re-firing — no ~45d sliding re-mint."""
    db = await _setup()
    try:
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)
        # A prior alert from far in the past, already resolved.
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at, resolved, resolved_at) "
            "VALUES (?, 'user_model_staleness_monitor', 'infrastructure_alert', 'x', 'medium', ?, 1, ?)",
            (
                uuid.uuid4().hex,
                (datetime.now(UTC) - timedelta(days=200)).isoformat(),
                (datetime.now(UTC) - timedelta(days=199)).isoformat(),
            ),
        )
        await db.commit()
        await _check_user_model_staleness(db)  # still no delta ever
        assert len(await _alerts(db)) == 1, (
            "never-delta episode must not re-mint against an old alert"
        )
    finally:
        await db.close()


async def test_episode_dedup_survives_ttl_resolution():
    """THE FLAP FIX: once a silence episode is surfaced, a TTL auto-resolution
    of that alert (while the stream is STILL silent — no new delta) must NOT
    let the next tick mint a second alert for the same episode."""
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_STALE)
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)
        await _check_user_model_staleness(db)
        first = await _alerts(db)
        assert len(first) == 1

        # Simulate the 3-day infrastructure_alert TTL firing while still stale:
        # mark the alert resolved WITHOUT any new delta arriving.
        await db.execute(
            "UPDATE observations SET resolved = 1, resolved_at = ? "
            "WHERE source = 'user_model_staleness_monitor'",
            (datetime.now(UTC).isoformat(),),
        )
        await db.commit()

        # Next tick: still stale, still interacting — but the episode was already
        # surfaced (an alert exists after the last delta), so no re-alert.
        await _check_user_model_staleness(db)
        assert len(await _alerts(db)) == 1, "TTL resolution must not re-mint the same-episode alert"
    finally:
        await db.close()


async def test_fresh_delta_resolves_prior_alert():
    db = await _setup()
    try:
        await _seed_delta(db, days_ago=_STALE)
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)
        await _check_user_model_staleness(db)
        before = await _alerts(db)
        assert len(before) == 1 and before[0]["resolved"] == 0

        await _seed_delta(db, days_ago=0)  # recovery
        await _check_user_model_staleness(db)
        assert all(a["resolved"] == 1 for a in await _alerts(db)), "fresh delta must resolve"
    finally:
        await db.close()


async def test_new_episode_after_recovery_realerts():
    """A PRIOR episode's alert (created before the current last delta) must not
    suppress a genuinely NEW silence episode."""
    db = await _setup()
    try:
        # An old, resolved alert from a previous drought (before the last delta).
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at, resolved, resolved_at) "
            "VALUES (?, 'user_model_staleness_monitor', 'infrastructure_alert', 'old', 'medium', ?, 1, ?)",
            (
                uuid.uuid4().hex,
                (datetime.now(UTC) - timedelta(days=_STALE + 20)).isoformat(),
                (datetime.now(UTC) - timedelta(days=_STALE + 19)).isoformat(),
            ),
        )
        await db.commit()
        # Last delta is more recent than that old alert but still stale now.
        await _seed_delta(db, days_ago=_STALE)
        await _seed_sessions(db, count=_ENOUGH, days_ago=1)

        await _check_user_model_staleness(db)
        unresolved = [a for a in await _alerts(db) if a["resolved"] == 0]
        assert len(unresolved) == 1, "a new episode must alert despite an older resolved alert"
    finally:
        await db.close()


async def test_none_db_and_resolve_are_noops():
    await _check_user_model_staleness(None)  # must not raise
    await _resolve_user_model_staleness(None)  # must not raise


pytestmark = pytest.mark.asyncio
