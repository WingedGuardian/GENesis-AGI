"""Tests for SessionManager lifecycle hooks."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.session_manager import SessionManager


@pytest.fixture
def db():
    """Mock DB that returns a dict for get_by_id."""
    mock = AsyncMock()
    return mock


@pytest.fixture
def manager(db, monkeypatch):
    mgr = SessionManager(db=db)
    # Mock CRUD so create_background/complete/fail don't hit real DB
    monkeypatch.setattr(
        "genesis.cc.session_manager.cc_sessions.create", AsyncMock(),
    )
    monkeypatch.setattr(
        "genesis.cc.session_manager.cc_sessions.get_by_id",
        AsyncMock(return_value={"id": "sess-1", "session_type": "background"}),
    )
    monkeypatch.setattr(
        "genesis.cc.session_manager.cc_sessions.update_status", AsyncMock(),
    )
    return mgr


@pytest.mark.asyncio
async def test_on_start_fires_for_background(manager):
    hook = AsyncMock()
    manager.add_on_start(hook)

    await manager.create_background(
        session_type=MagicMock(value="bg_reflection"),
        model=MagicMock(value="sonnet"),
        source_tag="reflection_deep",
    )

    hook.assert_called_once()
    args = hook.call_args[0]
    assert len(args[0]) == 36  # UUID session_id
    assert "reflection_deep" in args[2]  # source_tag


@pytest.mark.asyncio
async def test_on_end_fires_on_complete(manager):
    hook = AsyncMock()
    manager.add_on_end(hook)

    await manager.complete("sess-1")

    hook.assert_called_once_with("sess-1")


@pytest.mark.asyncio
async def test_on_end_fires_on_fail(manager):
    hook = AsyncMock()
    manager.add_on_end(hook)

    await manager.fail("sess-1", reason="test failure")

    hook.assert_called_once_with("sess-1")


@pytest.mark.asyncio
async def test_hook_error_does_not_propagate(manager):
    """A failing hook must not break the session lifecycle."""
    bad_hook = AsyncMock(side_effect=RuntimeError("hook broke"))
    manager.add_on_end(bad_hook)

    # Should not raise
    await manager.complete("sess-1")
    bad_hook.assert_called_once()


@pytest.mark.asyncio
async def test_no_hooks_by_default(manager):
    """No hooks registered = no errors on lifecycle events."""
    await manager.create_background(
        session_type=MagicMock(value="bg_reflection"),
        model=MagicMock(value="sonnet"),
    )
    await manager.complete("sess-1")
    await manager.fail("sess-1", reason="test")
    # No assertions needed — just verifying no exceptions


@pytest.mark.asyncio
async def test_foreground_does_not_fire_start_hook(manager, monkeypatch):
    """Foreground sessions should NOT fire on_start hooks."""
    monkeypatch.setattr(
        "genesis.cc.session_manager.cc_sessions.get_active_foreground",
        AsyncMock(return_value=None),
    )
    hook = AsyncMock()
    manager.add_on_start(hook)

    await manager.get_or_create_foreground(
        user_id="tg-123", channel="telegram",
    )

    hook.assert_not_called()
