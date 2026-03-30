"""Tests for ReplyWaiter — send-and-wait outreach infrastructure."""

import asyncio

import pytest

from genesis.outreach.reply_waiter import ReplyWaiter


@pytest.mark.asyncio
async def test_register_and_resolve():
    waiter = ReplyWaiter()
    future = waiter.register("msg-123")
    assert not future.done()

    resolved = waiter.resolve("msg-123", "user reply text")
    assert resolved is True
    assert await future == "user reply text"


@pytest.mark.asyncio
async def test_wait_for_reply_timeout():
    waiter = ReplyWaiter()
    result = await waiter.wait_for_reply("msg-999", timeout_s=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_unknown_returns_false():
    waiter = ReplyWaiter()
    assert waiter.resolve("nonexistent", "text") is False


@pytest.mark.asyncio
async def test_cancel():
    waiter = ReplyWaiter()
    future = waiter.register("msg-456")
    waiter.cancel("msg-456")
    assert future.cancelled()


@pytest.mark.asyncio
async def test_multiple_concurrent_waiters():
    waiter = ReplyWaiter()
    f1 = waiter.register("msg-1")
    f2 = waiter.register("msg-2")

    waiter.resolve("msg-2", "reply two")
    waiter.resolve("msg-1", "reply one")

    assert await f1 == "reply one"
    assert await f2 == "reply two"


@pytest.mark.asyncio
async def test_double_resolve_returns_false():
    waiter = ReplyWaiter()
    waiter.register("msg-789")
    assert waiter.resolve("msg-789", "first") is True
    assert waiter.resolve("msg-789", "second") is False


@pytest.mark.asyncio
async def test_wait_for_reply_success():
    waiter = ReplyWaiter()

    async def resolve_later():
        await asyncio.sleep(0.05)
        waiter.resolve("msg-async", "delayed reply")

    from genesis.util.tasks import tracked_task

    tracked_task(resolve_later(), name="test-resolve-later")
    result = await waiter.wait_for_reply("msg-async", timeout_s=2.0)
    assert result == "delayed reply"


@pytest.mark.asyncio
async def test_cancel_nonexistent_is_safe():
    waiter = ReplyWaiter()
    waiter.cancel("does-not-exist")  # Should not raise
