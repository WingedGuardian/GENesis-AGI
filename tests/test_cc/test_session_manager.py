"""Tests for SessionManager."""

import json
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
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    assert sess is not None
    assert sess["session_type"] == "foreground"
    assert sess["status"] == "active"


async def test_get_existing_foreground(db, manager):
    s1 = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    s2 = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    assert s1["id"] == s2["id"]


async def test_get_or_create_flips_checkpointed_to_active(db, manager):
    """A reaped (checkpointed) foreground session is revived to 'active' on
    reuse, preserving its id so --resume continues (D3 resume safety)."""
    s1 = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    await cc_sessions.checkpoint_dark(db, s1["id"], checkpointed_at="2026-07-22T12:00:00+00:00")
    assert (await cc_sessions.get_by_id(db, s1["id"]))["status"] == "checkpointed"
    s2 = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    assert s2["id"] == s1["id"]
    assert s2["status"] == "active"
    assert (await cc_sessions.get_by_id(db, s1["id"]))["status"] == "active"


async def test_create_background(db, manager):
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
        effort=EffortLevel.HIGH,
    )
    assert sess["session_type"] == "background_reflection"
    assert sess["model"] == "sonnet"


async def test_create_background_dispatch_mode(db, manager):
    """dispatch_mode is stored in metadata JSON alongside skill_tags."""
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
        dispatch_mode="cli",
    )
    meta = json.loads(sess["metadata"])
    assert meta["dispatch_mode"] == "cli"


async def test_create_background_dispatch_mode_with_skill_tags(db, manager):
    """Both dispatch_mode and skill_tags coexist in metadata."""
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
        skill_tags=["deep-reflection"],
        dispatch_mode="cli",
    )
    meta = json.loads(sess["metadata"])
    assert meta["dispatch_mode"] == "cli"
    assert meta["skill_tags"] == ["deep-reflection"]


async def test_create_background_no_dispatch_mode(db, manager):
    """Without dispatch_mode, metadata only contains skill_tags (backward compat)."""
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
        skill_tags=["light-reflection"],
    )
    meta = json.loads(sess["metadata"])
    assert "dispatch_mode" not in meta
    assert meta["skill_tags"] == ["light-reflection"]


async def test_create_background_with_profile(db, manager):
    """Profile is stored in metadata at creation time."""
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        dispatch_mode="direct",
        profile="observe",
    )
    meta = json.loads(sess["metadata"])
    assert meta["profile"] == "observe"
    assert meta["dispatch_mode"] == "direct"


async def test_create_background_no_profile(db, manager):
    """Without profile, metadata omits the key (backward compat)."""
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_REFLECTION,
        model=CCModel.SONNET,
    )
    assert sess["metadata"] is None


async def test_checkpoint(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    await manager.checkpoint(sess["id"])
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["status"] == "checkpointed"


async def test_complete(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
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


async def test_cleanup_stale_fires_end_hooks(db, manager):
    """T2-B: the reaper path must fire session end-hooks for swept rows —
    the old crud reap_stale bypassed SessionManager and never did."""
    await cc_sessions.create(
        db,
        id="stale-hook",
        session_type="background_task",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="telegram",
        started_at="2026-03-07T06:00:00",
        last_activity_at="2026-03-07T06:00:00",
        source_tag="direct_session",
    )
    fired: list[str] = []

    async def _hook(session_id: str) -> None:
        fired.append(session_id)

    manager.add_on_end(_hook)
    count = await manager.cleanup_stale(max_idle_minutes=60)
    assert count == 1
    assert fired == ["stale-hook"]
    row = await cc_sessions.get_by_id(db, "stale-hook")
    assert row["status"] == "expired"


async def test_cleanup_stale_respects_cutoff(db, manager):
    """A recently-active background session is NOT swept (6h cron passes
    max_idle_minutes=360; recent activity must survive)."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db,
        id="fresh-bg",
        session_type="background_task",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="telegram",
        started_at=now,
        last_activity_at=now,
        source_tag="direct_session",
    )
    count = await manager.cleanup_stale(max_idle_minutes=360)
    assert count == 0
    row = await cc_sessions.get_by_id(db, "fresh-bg")
    assert row["status"] == "active"


async def test_create_background_stamps_origin(db, manager):
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        origin="external_untrusted",
    )
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["origin_class"] == "external_untrusted"


async def test_create_background_origin_defaults_null(db, manager):
    sess = await manager.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
    )
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["origin_class"] is None


async def test_foreground_origin_stays_null(db, manager):
    sess = await manager.get_or_create_foreground(
        user_id="u1",
        channel=ChannelType.TELEGRAM,
    )
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["origin_class"] is None  # owner-supervised, read first_party
