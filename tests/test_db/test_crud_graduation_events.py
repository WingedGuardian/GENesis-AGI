"""CRUD tests for ``graduation_events`` — voice graduation quarantine (W0).

Covers the transport contract half the endpoint relies on: INSERT OR IGNORE
dedup on ``event_id`` (at-least-once delivery → effectively-once landing),
JSON round-trip of payload/provenance, and the prune invariant — dispositioned
rows age out at 90d, pending rows are NEVER pruned (they are the W2 drainer's
inbox).
"""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import graduation_events

pytestmark = pytest.mark.asyncio

_ENVELOPE = {
    "event_id": "evt-0001",
    "schema_version": 1,
    "type": "memory_candidate",
    "source": "pe-livingroom",
    "occurred_at": "2026-07-17T21:00:00+00:00",
    "received_at": "2026-07-18T02:00:00+00:00",
    "payload": {"claim": "someone mentioned a trip", "content_class": "user_world"},
    "provenance": {"class": "ambient_overheard", "attribution": {"is_user": False}},
}


async def _insert(db, **overrides) -> bool:
    kwargs = {**_ENVELOPE, **overrides}
    return await graduation_events.insert_event(db, **kwargs)


async def test_insert_lands_pending_row(db):
    assert await _insert(db) is True

    row = await graduation_events.get_by_event_id(db, event_id="evt-0001")
    assert row is not None
    assert row["disposition"] == "pending"
    assert row["memory_id"] is None
    assert row["disposed_at"] is None
    assert json.loads(row["payload"]) == _ENVELOPE["payload"]
    assert json.loads(row["provenance"]) == _ENVELOPE["provenance"]


async def test_replay_is_duplicate_and_row_unchanged(db):
    assert await _insert(db) is True
    first = await graduation_events.get_by_event_id(db, event_id="evt-0001")

    # Replay with different content — the original row must win untouched.
    assert await _insert(db, source="pe-kitchen") is False
    row = await graduation_events.get_by_event_id(db, event_id="evt-0001")
    assert row == first

    cursor = await db.execute("SELECT COUNT(*) FROM graduation_events WHERE event_id = 'evt-0001'")
    assert (await cursor.fetchone())[0] == 1


async def test_prune_never_touches_pending(db):
    # A pending row far older than any retention window.
    assert await _insert(
        db,
        event_id="evt-old-pending",
        occurred_at="2020-01-01T00:00:00+00:00",
        received_at="2020-01-01T00:00:00+00:00",
    )

    removed = await graduation_events.prune_older_than(db, days=1)
    assert removed == 0
    assert await graduation_events.get_by_event_id(db, event_id="evt-old-pending") is not None


async def test_prune_removes_old_dispositioned_keeps_recent(db):
    for event_id, disposed_at in (
        ("evt-landed-old", "2020-01-01T00:00:00+00:00"),
        ("evt-landed-recent", "2100-01-01T00:00:00+00:00"),
    ):
        assert await _insert(db, event_id=event_id)
        await db.execute(
            "UPDATE graduation_events SET disposition = 'landed', disposed_at = ? "
            "WHERE event_id = ?",
            (disposed_at, event_id),
        )
    await db.commit()

    removed = await graduation_events.prune_older_than(db, days=90)
    assert removed == 1
    assert await graduation_events.get_by_event_id(db, event_id="evt-landed-old") is None
    assert await graduation_events.get_by_event_id(db, event_id="evt-landed-recent") is not None
