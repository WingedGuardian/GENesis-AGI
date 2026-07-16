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


# ---------------------------------------------------------------------------
# Scoped standalone-text resolution (chat+topic bound)
# ---------------------------------------------------------------------------


async def test_scoped_resolution_matches_same_thread():
    w = ReplyWaiter()
    fut = w.register("d1")
    w.set_context("d1", "123:45")
    assert w.resolve_scoped_pending("ok", thread_key="123:45") == ["d1"]
    assert fut.result() == "ok"


async def test_scoped_resolution_ignores_other_thread():
    """The cross-chat conflation bug: a DM must never resolve a waiter
    whose prompt was delivered to a forum topic."""
    w = ReplyWaiter()
    w.register("d1")
    w.set_context("d1", "123:45")
    assert w.resolve_scoped_pending("ok", thread_key="999:dm") == []
    assert w.pending_count == 1


async def test_scoped_resolution_skips_contextless_waiters():
    w = ReplyWaiter()
    w.register("d1")  # no context recorded
    assert w.resolve_scoped_pending("ok", thread_key="123:45") == []


async def test_scoped_resolution_ambiguous_does_nothing():
    w = ReplyWaiter()
    w.register("d1")
    w.register("d2")
    w.set_context("d1", "123:45")
    w.set_context("d2", "123:45")
    assert w.resolve_scoped_pending("ok", thread_key="123:45") == []
    assert w.pending_count == 2


async def test_scoped_resolution_alias_counts_once():
    """A waiter with a callback-data alias is ONE waiter, not two —
    aliasing must not make the single-pending check ambiguous."""
    w = ReplyWaiter()
    fut = w.register("uuid-key")
    w.set_context("uuid-key", "123:45")
    w.add_alias("msg-77", "uuid-key")
    # ALL keys for the matched future come back (canonical + alias) — any
    # single one may be a UUID the outreach store doesn't know, so the
    # caller needs the full set to correlate the reply to the DB row.
    keys = w.resolve_scoped_pending("go", thread_key="123:45")
    assert set(keys) == {"uuid-key", "msg-77"}
    assert fut.result() == "go"
    # Both keys are gone — no stale entries.
    assert w.pending_count == 0
    assert w.resolve("msg-77", "late") is False


async def test_alias_inherits_context():
    w = ReplyWaiter()
    w.register("uuid-key")
    w.set_context("uuid-key", "123:45")
    w.add_alias("msg-77", "uuid-key")
    assert w._contexts.get("msg-77") == "123:45"


async def test_context_cleared_on_resolve_and_cancel():
    w = ReplyWaiter()
    w.register("d1")
    w.set_context("d1", "123:45")
    w.resolve("d1", "ok")
    assert "d1" not in w._contexts
    w.register("d2")
    w.set_context("d2", "123:45")
    w.cancel("d2")
    assert "d2" not in w._contexts
