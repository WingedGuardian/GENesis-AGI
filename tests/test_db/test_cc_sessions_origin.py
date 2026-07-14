"""WS-3 gate-2 substrate: session origin stamping + window aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import cc_sessions

pytestmark = pytest.mark.asyncio


def _iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


async def _mk(
    db, sid: str, *, origin: str | None, active_minutes_ago: int,
    status: str = "active",
) -> None:
    await cc_sessions.create(
        db, id=sid, session_type="background_task", model="sonnet",
        started_at=_iso(active_minutes_ago + 5),
        last_activity_at=_iso(active_minutes_ago),
        source_tag="test", status=status, origin_class=origin,
    )


async def test_create_persists_origin_class(db):
    await _mk(db, "s-ext", origin="external_untrusted", active_minutes_ago=1)
    row = await cc_sessions.get_by_id(db, "s-ext")
    assert row["origin_class"] == "external_untrusted"


async def test_create_defaults_null(db):
    await _mk(db, "s-none", origin=None, active_minutes_ago=1)
    row = await cc_sessions.get_by_id(db, "s-none")
    assert row["origin_class"] is None


async def test_any_external_session_overlapping(db):
    now = datetime.now(UTC).isoformat()
    await _mk(db, "s-fp", origin=None, active_minutes_ago=1)
    assert not await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(60), end_iso=now
    )
    # completed external session whose last activity (10m ago) is IN the 60m
    # window -> counted; a 5m window (after its last activity) -> not.
    await _mk(
        db, "s-ext", origin="external_untrusted", active_minutes_ago=10,
        status="completed",
    )
    assert await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(60), end_iso=now
    )
    assert not await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(5), end_iso=now
    )


async def test_long_running_active_external_session_counts(db):
    """Codex P2: a still-ACTIVE external session that STARTED before the
    window must still count — its last_activity_at can be stale (only the
    foreground update_activity path advances it)."""
    now = datetime.now(UTC).isoformat()
    # started + last-active BOTH 3h ago, but status active (long-running)
    await cc_sessions.create(
        db, id="s-longrun", session_type="background_task", model="sonnet",
        started_at=_iso(185), last_activity_at=_iso(180), source_tag="inbox_evaluation",
        status="active", origin_class="external_untrusted",
    )
    assert await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(60), end_iso=now
    )


async def test_completed_external_session_last_active_in_window_counts(db):
    """A background session that STARTED before the window but whose last
    activity fell INSIDE it still overlaps."""
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db, id="s-done", session_type="background_task", model="sonnet",
        started_at=_iso(120), last_activity_at=_iso(30), source_tag="inbox_evaluation",
        status="completed", origin_class="external_untrusted",
    )
    assert await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(60), end_iso=now
    )


async def test_stale_completed_external_session_excluded(db):
    """A background session that both started AND ended before the window
    (not active) does NOT overlap."""
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db, id="s-stale", session_type="background_task", model="sonnet",
        started_at=_iso(200), last_activity_at=_iso(180), source_tag="inbox_evaluation",
        status="completed", origin_class="external_untrusted",
    )
    assert not await cc_sessions.any_external_session_overlapping(
        db, since_iso=_iso(60), end_iso=now
    )


async def test_reflection_window_origin_external(db):
    await _mk(db, "s-ext", origin="external_untrusted", active_minutes_ago=10)
    out = await cc_sessions.reflection_window_origin(
        db, end_iso=datetime.now(UTC).isoformat(),
    )
    assert out == "external_untrusted"


async def test_reflection_window_origin_first_party_when_quiet(db):
    await _mk(db, "s-fp", origin=None, active_minutes_ago=10)
    out = await cc_sessions.reflection_window_origin(
        db, end_iso=datetime.now(UTC).isoformat(),
    )
    assert out == "first_party"


async def test_reflection_window_origin_stale_external_excluded(db):
    # external session that STARTED and ENDED (completed) before the window
    await _mk(
        db, "s-old", origin="external_untrusted", active_minutes_ago=180,
        status="completed",
    )
    out = await cc_sessions.reflection_window_origin(
        db, end_iso=datetime.now(UTC).isoformat(),
    )
    assert out == "first_party"


async def test_reflection_window_origin_never_raises():
    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    out = await cc_sessions.reflection_window_origin(
        _Boom(), end_iso=datetime.now(UTC).isoformat(),
    )
    assert out == "first_party"  # fail-open, documented
