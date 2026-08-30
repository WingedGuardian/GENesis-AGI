"""The discarded-count contract the queues snapshot PRODUCES.

Scope note: this pins the producer only. The frontend regression — the actual
2026-08-30 bug — is pinned in tests/test_dashboard/test_webui_js_integrity.py,
because `queues.py` was already correct and these tests pass unchanged on the
buggy revision.

`queues()` returns TWO different things for discarded work: `discarded_count`
(the true depth, an unbounded COUNT) and `discarded_items` (a LIMIT-20 review
sample). Three dashboard sites render a count from this payload, and they must
read `discarded_count`.

Origin (2026-08-30): the overview panel and the attention strip both rendered
`discarded_items.length`, so a queue holding 148 discarded rows reported
"20 discarded items" — the truncation limit read as the total. Nothing caught
it because nothing asserted the two fields diverge.

This pins the BACKEND half of that contract: the field must exist, must be the
unbounded count, and must be independent of the sample cap. If `discarded_count`
is ever dropped from the payload, the frontend's `discardedTotal` fallback
silently reverts to the sample length. (`mcp/health/errors.py` also alerts on
`discarded_count > 100`, so this is not the *only* consumer — but it is the one
the dashboard renders.)
"""

import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

q = importlib.import_module("genesis.observability.snapshots.queues")

# Deliberately > the LIMIT 20 sample cap, so count and sample MUST diverge.
_DISCARDED = 25


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _seed(db, *, discarded: int = 0, expired: int = 0) -> None:
    # Seed relative to now: absolute dates become wall-clock time bombs.
    _seeded_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(discarded + expired):
        await db.execute(
            "INSERT INTO deferred_work_queue "
            "(id, work_type, call_site_id, payload_json, deferred_at, "
            " deferred_reason, attempts, error_message, status, "
            " created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"seed-{i}",
                "entity_adjudication",
                "entity_adjudication",
                "{}",
                _seeded_at.isoformat(),
                "fuzzy norm_name match at entity creation",
                5,
                "exhausted 5 attempts",
                "discarded" if i < discarded else "expired",
                _seeded_at.isoformat(),
                # timedelta, not f"00:{i:02d}" — that produced "00:60:00" above
                # 59 rows, which fromisoformat rejects inside queues.py's loop
                # where the except swallows it, yielding a baffling empty sample.
                (_seeded_at + timedelta(minutes=i)).isoformat(),
            ),
        )
    await db.commit()


async def test_discarded_count_is_the_true_depth_not_the_sample_length(db):
    """The bug, pinned: count must exceed the capped sample."""
    await _seed(db, discarded=_DISCARDED)

    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == _DISCARDED, (
        f"discarded_count must be the unbounded COUNT, got {result['discarded_count']}"
    )
    # The sample is deliberately capped; this is correct, not a bug.
    assert len(result["discarded_items"]) == 20


async def test_discarded_count_includes_expired_like_the_sample_query(db):
    """Both halves filter status IN ('discarded','expired') — keep them aligned.

    If the count and sample ever filtered differently, the frontend's
    `discarded_count || items.length` fallback would swap between two
    populations depending on which happened to be truthy.
    """
    await _seed(db, discarded=3, expired=4)

    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == 7
    assert len(result["discarded_items"]) == 7


async def test_discarded_count_is_zero_on_an_empty_queue(db):
    """Empty state: a fresh install must report 0, not a missing key."""
    result = await q.queues(db, None, None, None)

    assert result["discarded_count"] == 0
    assert result["discarded_items"] == []
