"""Tests for the best-effort default-executor gauge (PR-2c).

The gauge pokes private CPython executor internals, so these lock in the
contract that matters to callers: a well-formed dict once the executor exists,
and ``None`` (never an exception) for every "can't measure" path.
"""

from __future__ import annotations

import asyncio

from genesis.util.loop_diag import default_executor_pending

# asyncio_mode=auto: async tests run automatically; the sync test below runs sync
# (a module-level asyncio mark would wrongly wrap it and assert-fail on "no loop").


async def test_returns_none_before_executor_created():
    # No to_thread has run on this loop yet → loop._default_executor is None.
    loop = asyncio.get_running_loop()
    assert loop._default_executor is None
    assert default_executor_pending() is None


async def test_returns_dict_after_to_thread():
    # First to_thread lazily creates the default ThreadPoolExecutor.
    await asyncio.to_thread(lambda: None)
    stats = default_executor_pending()
    assert stats is not None
    assert set(stats) == {"pending", "workers", "max_workers"}
    assert isinstance(stats["pending"], int) and stats["pending"] >= 0
    assert stats["workers"] >= 1
    assert stats["max_workers"] >= 1


def test_returns_none_outside_running_loop():
    # get_running_loop() raises with no loop → blanket guard returns None.
    assert default_executor_pending() is None


async def test_reflects_queued_work(monkeypatch):
    """A backed-up executor reports a non-zero pending depth. Pin the pool to a
    single worker and block it so a second submission has to queue."""
    loop = asyncio.get_running_loop()
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    try:
        release = asyncio.Event()
        started = asyncio.Event()

        def _block():
            # Signal we occupy the sole worker, then spin until released.
            loop.call_soon_threadsafe(started.set)
            while not release.is_set():
                pass

        busy = asyncio.ensure_future(asyncio.to_thread(_block))
        await started.wait()
        # Sole worker is occupied; queue a second call so pending > 0.
        queued = asyncio.ensure_future(asyncio.to_thread(lambda: None))
        await asyncio.sleep(0)  # let the submission enqueue

        stats = default_executor_pending()
        # Release BEFORE asserting: a failed assertion must not leave the busy-spin
        # thread running, or the finally's shutdown(wait=True) stalls the whole run
        # until the global pytest timeout (reviewer catch).
        release.set()
        await busy
        await queued

        assert stats is not None
        assert stats["max_workers"] == 1
        assert stats["pending"] >= 1
    finally:
        executor.shutdown(wait=True)
