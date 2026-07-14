"""WS-3 gate-2 substrate: session origin stamping + window aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import cc_sessions

pytestmark = pytest.mark.asyncio


def _iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


async def _mk(db, sid: str, *, origin: str | None, active_minutes_ago: int) -> None:
    await cc_sessions.create(
        db, id=sid, session_type="background_task", model="sonnet",
        started_at=_iso(active_minutes_ago + 5),
        last_activity_at=_iso(active_minutes_ago),
        source_tag="test", origin_class=origin,
    )


async def test_create_persists_origin_class(db):
    await _mk(db, "s-ext", origin="external_untrusted", active_minutes_ago=1)
    row = await cc_sessions.get_by_id(db, "s-ext")
    assert row["origin_class"] == "external_untrusted"


async def test_create_defaults_null(db):
    await _mk(db, "s-none", origin=None, active_minutes_ago=1)
    row = await cc_sessions.get_by_id(db, "s-none")
    assert row["origin_class"] is None


async def test_any_external_session_since(db):
    await _mk(db, "s-fp", origin=None, active_minutes_ago=1)
    assert not await cc_sessions.any_external_session_since(db, since_iso=_iso(60))
    await _mk(db, "s-ext", origin="external_untrusted", active_minutes_ago=10)
    assert await cc_sessions.any_external_session_since(db, since_iso=_iso(60))
    # outside the window -> not counted
    assert not await cc_sessions.any_external_session_since(db, since_iso=_iso(5))


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
    # external session last active outside the 60-min material window
    await _mk(db, "s-old", origin="external_untrusted", active_minutes_ago=180)
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
