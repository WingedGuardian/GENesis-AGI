"""Slot-semaphore tests — flock contention within one process.

flock locks attach to the open file description, so separate os.open()
calls in the same process genuinely contend — no subprocesses needed.
"""

from __future__ import annotations

import pytest

from genesis.session_awareness.slots import (
    SLOT_COUNT,
    acquire_slot,
    try_acquire_slot,
)


def test_two_slots_then_busy(tmp_path):
    a = try_acquire_slot(tmp_path)
    b = try_acquire_slot(tmp_path)
    assert a is not None and b is not None
    assert {a.index, b.index} == set(range(SLOT_COUNT))
    assert try_acquire_slot(tmp_path) is None
    a.release()
    c = try_acquire_slot(tmp_path)
    assert c is not None and c.index == a.index
    b.release()
    c.release()


def test_release_idempotent(tmp_path):
    a = try_acquire_slot(tmp_path)
    a.release()
    a.release()  # second release is a no-op, not an error


@pytest.mark.asyncio
async def test_acquire_slot_times_out_fast(tmp_path):
    held = [try_acquire_slot(tmp_path) for _ in range(SLOT_COUNT)]
    assert all(held)
    got = await acquire_slot(tmp_path, timeout_s=0.1)
    assert got is None
    for h in held:
        h.release()
    got = await acquire_slot(tmp_path, timeout_s=0.1)
    assert got is not None
    got.release()
