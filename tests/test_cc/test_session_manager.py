"""Tests for SessionManager."""

from unittest.mock import AsyncMock

import pytest

from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCModel, ChannelType, EffortLevel, SessionType
from genesis.db.crud import cc_sessions


@pytest.fixture
def mock_invoker():
    return AsyncMock()


@pytest.fixture
async def manager(db, mock_invoker):
    return SessionManager(db=db, invoker=mock_invoker, day_boundary_hour=0)


async def test_create_foreground(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1", channel=ChannelType.TELEGRAM,
    )
    assert sess is not None
    assert sess["session_type"] == "foreground"
    assert sess["status"] == "active"


async def test_get_existing_foreground(db, manager):
    s1 = await manager.get_or_create_foreground(
        user_id="u1", channel=ChannelType.TELEGRAM,
    )
    s2 = await manager.get_or_create_foreground(
        user_id="u1", channel=ChannelType.TELEGRAM,
    )
    assert s1["id"] == s2["id"]


async def test_create_background(db, manager):
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
        effort=EffortLevel.HIGH,
    )
    assert sess["session_type"] == "background_reflection"
    assert sess["model"] == "sonnet"


async def test_checkpoint(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1", channel=ChannelType.TELEGRAM,
    )
    await manager.checkpoint(sess["id"])
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["status"] == "checkpointed"


async def test_complete(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1", channel=ChannelType.TELEGRAM,
    )
    await manager.complete(sess["id"])
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["status"] == "completed"


async def test_cleanup_stale(db, manager):
    """Per-type cleanup: expire stale light reflections but preserve foreground."""
    # Expirable: light reflection (stale)
    await cc_sessions.create(
        db,
        id="stale-bg",
        session_type="background_reflection",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="bridge",
        started_at="2026-03-07T06:00:00",
        last_activity_at="2026-03-07T06:00:00",
        source_tag="reflection_light",
    )
    # Protected: foreground session (never auto-expired)
    await cc_sessions.create(
        db,
        id="stale-fg",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="telegram",
        started_at="2026-03-07T06:00:00",
        last_activity_at="2026-03-07T06:00:00",
    )
    count = await manager.cleanup_stale(max_idle_minutes=60)
    assert count >= 1
    bg_row = await cc_sessions.get_by_id(db, "stale-bg")
    assert bg_row["status"] == "expired"
    fg_row = await cc_sessions.get_by_id(db, "stale-fg")
    assert fg_row["status"] == "active"  # Foreground never auto-expired
