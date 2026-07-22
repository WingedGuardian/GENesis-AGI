"""Stale-while-revalidate for the tag co-occurrence index (follow-up ac27b693).

expand_query must NEVER rebuild the index inline on the recall path: a stale
index kicks a single-flight BACKGROUND rebuild and the current call proceeds
with whatever the index holds. These tests pin that contract so a future edit
can't silently restore the multi-second inline scroll on the hot path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from genesis.memory import intent


class _FakeInfo:
    def __init__(self, count: int) -> None:
        self.points_count = count


def _fake_qdrant(count: int, tag_lists: list[list[str]]):
    """A sync QdrantClient double: get_collection returns a count; scroll yields
    one page of points whose payloads carry the given tag lists."""
    client = MagicMock()
    client.get_collection.return_value = _FakeInfo(count)
    points = [MagicMock(payload={"tags": tags}) for tags in tag_lists]
    # scroll returns (points, next_offset); next_offset=None ends pagination.
    client.scroll.return_value = (points, None)
    return client


@pytest.fixture(autouse=True)
def _reset_state():
    intent._reset_tag_index_state()
    yield
    intent._reset_tag_index_state()


@pytest.mark.asyncio
async def test_stale_index_does_not_rebuild_inline():
    """A stale index must NOT scroll inline — expand returns immediately with the
    (still-empty) index, and a background rebuild is scheduled."""
    client = _fake_qdrant(count=1000, tag_lists=[["alpha", "beta"], ["alpha", "gamma"]])

    # Cold index is stale; the call must return without an inline scroll.
    result = await intent.expand_query("alpha", client, ["episodic_memory"])

    # No inline scroll happened during the call itself.
    assert client.scroll.call_count == 0
    # Unexpanded this call (index still empty) — original query preserved.
    assert result == "alpha"
    # A single-flight rebuild is in flight.
    assert intent._rebuild_in_flight is True

    # Let the background rebuild task run; it scrolls off the hot path.
    await asyncio.sleep(0)
    for _ in range(20):
        if not intent._rebuild_in_flight:
            break
        await asyncio.sleep(0.02)
    assert intent._rebuild_in_flight is False
    assert client.scroll.call_count >= 1
    # Index is now populated — a subsequent expand can enrich.
    assert not intent._tag_index.is_stale(1000)


@pytest.mark.asyncio
async def test_single_flight_no_stampede():
    """Concurrent stale recalls must schedule at most ONE rebuild."""
    client = _fake_qdrant(count=500, tag_lists=[["x", "y"]])

    results = await asyncio.gather(
        *[intent.expand_query("x", client, ["episodic_memory"]) for _ in range(5)]
    )
    assert all(r == "x" for r in results)  # all unexpanded (cold index)
    # Only one rebuild claimed the flag; drain it.
    for _ in range(20):
        if not intent._rebuild_in_flight:
            break
        await asyncio.sleep(0.02)
    # Exactly one scroll pass over the single collection (one rebuild ran).
    assert client.scroll.call_count == 1


@pytest.mark.asyncio
async def test_count_check_is_time_gated():
    """get_collection must be polled at most once per interval, not per call."""
    client = _fake_qdrant(count=800, tag_lists=[["a", "b"]])

    await intent.expand_query("a", client, ["episodic_memory"])
    first = client.get_collection.call_count
    assert first >= 1
    # Drain the rebuild so the index is warm.
    for _ in range(20):
        if not intent._rebuild_in_flight:
            break
        await asyncio.sleep(0.02)

    # Immediate second call: within the interval → no new get_collection poll.
    await intent.expand_query("a", client, ["episodic_memory"])
    assert client.get_collection.call_count == first
