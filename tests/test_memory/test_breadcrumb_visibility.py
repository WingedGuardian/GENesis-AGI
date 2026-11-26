"""The proactive hook's breadcrumbs never offer a memory normal recall hides.

A hint is an invitation to ``memory_expand``, so it follows the same visibility
predicate (``graphstore.invalid_memory_ids_among``): deprecated and expired
(``invalid_at`` passed) memories are dropped before the 2-per-memory cap, so a
visible neighbour takes the freed slot, and the hidden id reaches neither
``related_ids`` nor the recall trace's ``hints``.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory import proactive as P

SEED = "00000000-seed"
OTHER_SEED = "11111111-seed"
LIVE_A = "aaaaaaaa-live"
LIVE_B = "bbbbbbbb-live"
DEPRECATED = "dddddddd-deprecated"
EXPIRED = "eeeeeeee-expired"
LATER = "ffffffff-expires-later"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, invalid_at TEXT, "
        "deprecated INTEGER DEFAULT 0);"
        "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, strength REAL);"
    )
    await conn.executemany(
        "INSERT INTO memory_metadata VALUES (?, ?, ?)",
        [
            (SEED, None, 0),
            (OTHER_SEED, None, 0),
            (LIVE_A, None, 0),
            (LIVE_B, None, 0),
            (DEPRECATED, None, 1),
            (EXPIRED, "2020-01-01T00:00:00+00:00", 0),
            (LATER, "2999-01-01T00:00:00+00:00", 0),
        ],
    )
    try:
        yield conn
    finally:
        await conn.close()


async def _links(db, rows):
    await db.executemany("INSERT INTO memory_links VALUES (?, ?, ?)", rows)


@pytest.mark.parametrize("hidden", [DEPRECATED, EXPIRED])
async def test_a_hidden_neighbour_is_never_offered(db, hidden):
    await _links(db, [(SEED, hidden, 0.95), (SEED, LIVE_A, 0.9), (SEED, LIVE_B, 0.6)])
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)
    # The hidden id frees its slot for the next visible neighbour.
    assert dicts[0]["related_ids"] == ["aaaaaaaa", "bbbbbbbb"]
    assert dicts[0]["_related_full"] == [LIVE_A, LIVE_B]
    assert hidden not in P._shown_hints(dicts)  # recall trace ``hints``


async def test_only_hidden_neighbours_leave_no_breadcrumb(db):
    await _links(db, [(SEED, DEPRECATED, 0.9), (SEED, EXPIRED, 0.8)])
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)
    assert "related_ids" not in dicts[0]
    assert "_related_full" not in dicts[0]
    assert P._shown_hints(dicts) == []


async def test_visible_neighbours_are_unchanged(db):
    # Not yet expired is visible; a delivered memory is still never hinted, and
    # the cap still counts it as before.
    await _links(
        db,
        [
            (SEED, LATER, 0.9),
            (SEED, LIVE_A, 0.8),
            (SEED, LIVE_B, 0.7),
            (OTHER_SEED, SEED, 0.9),
            (OTHER_SEED, LIVE_B, 0.8),
            (OTHER_SEED, LIVE_A, 0.4),
        ],
    )
    dicts = [{"memory_id": SEED}, {"memory_id": OTHER_SEED}]
    await P._breadcrumbs(db, dicts)
    assert dicts[0]["related_ids"] == ["ffffffff", "aaaaaaaa"]
    assert dicts[1]["related_ids"] == ["bbbbbbbb"]
    assert P._shown_hints(dicts) == [LATER, LIVE_A, LIVE_B]


async def test_a_failed_visibility_read_offers_nothing(db, monkeypatch):
    """The error propagates to ``_ro_read`` (shared-connection retry, then the
    caller's best-effort guard); no breadcrumb is written on the way out."""
    from genesis.memory import graphstore

    async def boom(*_a, **_kw):
        raise RuntimeError("visibility read failed")

    monkeypatch.setattr(graphstore, "invalid_memory_ids_among", boom)
    await _links(db, [(SEED, DEPRECATED, 0.9), (SEED, LIVE_A, 0.8)])
    dicts = [{"memory_id": SEED}]
    with pytest.raises(RuntimeError):
        await P._breadcrumbs(db, dicts)
    assert "related_ids" not in dicts[0]
    assert P._shown_hints(dicts) == []
