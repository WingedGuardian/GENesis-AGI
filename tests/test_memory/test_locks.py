"""Per-memory-id lock registry — memory/_locks.py.

The lock serializes delete vs re-embed/reconcile-requeue of the SAME memory so a
concurrent delete cannot resurrect a vector. These tests pin the three
properties the callers rely on: identity (same id → same lock), isolation
(different ids never contend), and mutual exclusion.
"""

from __future__ import annotations

import asyncio

import pytest

from genesis.memory import _locks
from genesis.memory._locks import memory_id_lock

pytestmark = pytest.mark.asyncio


async def test_same_id_returns_same_lock():
    assert memory_id_lock("m1") is memory_id_lock("m1")


async def test_different_ids_are_independent():
    a, b = memory_id_lock("a"), memory_id_lock("b")
    assert a is not b
    async with a:
        # A held lock on 'a' must NOT block a different id 'b'.
        assert not b.locked()
        async with b:
            assert b.locked()


async def test_mutual_exclusion_serializes_same_id():
    """Two coroutines contending for one id run strictly one-at-a-time."""
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with memory_id_lock("shared"):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0)  # yield — a broken lock would interleave here
            order.append(f"{tag}-exit")

    await asyncio.gather(worker("A"), worker("B"))

    # Whichever ran first, its enter/exit are adjacent — never interleaved.
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


async def test_registry_does_not_leak_unheld_locks():
    """A lock with no live holder is GC'd, so the registry can't grow unbounded."""
    import gc

    async with memory_id_lock("ephemeral"):
        pass
    gc.collect()
    # WeakValueDictionary drops the entry once the last strong ref is gone.
    assert "ephemeral" not in dict(_locks._locks)
