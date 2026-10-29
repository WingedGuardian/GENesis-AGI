"""Key Insights (7d): run reports are not insights, and repeats collapse.

The section is always-loaded L1 context. Two defects filled it with noise:
a scheduled job's per-run status report (the process reaper's observe-only
JSON, written every tick) was selected as an "insight", and the same text
written N times took N of the section's slots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.memory.essential_knowledge import _recent_decisions

REAPER_JSON = (
    '{"dry_run": true, "enforcement": "observation-only", "count": 2, '
    '"processes": [{"pid": 101, "label": "worker", "reason": "stale-detached"}]}'
)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _mk(db, oid, typ, content, *, minutes_ago=0, source="s"):
    when = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, "
        "created_at, resolved, origin_class) VALUES (?,?,?,?,?,?,0,'first_party')",
        (oid, source, typ, content, "low", when),
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typ",
    [
        "process_reaper_would_kill",
        "entity_adjudication",
        "dream_cycle_report",
        "dream_synthesis_drain_report",
    ],
)
async def test_run_report_types_are_not_insights(db, typ):
    await _mk(db, "r1", typ, f"RUN-REPORT-{typ}")
    await _mk(db, "i1", "learning", "a real learning", minutes_ago=1)

    out = await _recent_decisions(db)
    assert f"RUN-REPORT-{typ}" not in "\n".join(out)
    # Positive control: an exclusion that emptied the section would pass the
    # line above too.
    assert "a real learning" in out


@pytest.mark.asyncio
async def test_identical_rows_collapse_to_one(db):
    for i in range(4):
        await _mk(db, f"d{i}", "infrastructure_drift", "units changed", minutes_ago=i)
    await _mk(db, "x", "learning", "something else", minutes_ago=10)

    out = await _recent_decisions(db)
    assert out.count("units changed") == 1
    assert "something else" in out


@pytest.mark.asyncio
async def test_whitespace_and_case_variants_collapse(db):
    await _mk(db, "a", "learning", "Disk  is\nfilling up", minutes_ago=0)
    await _mk(db, "b", "learning", "disk is filling up", minutes_ago=1)

    out = await _recent_decisions(db)
    assert len(out) == 1
    assert out == ["Disk  is\nfilling up"]  # newest wins, shown unaltered


@pytest.mark.asyncio
async def test_different_numbers_are_not_merged(db):
    """A changed count is a real change; normalization must not hide it."""
    await _mk(db, "a", "learning", "3 jobs failing", minutes_ago=0)
    await _mk(db, "b", "learning", "9 jobs failing", minutes_ago=1)

    assert len(await _recent_decisions(db)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, -10])
@pytest.mark.parametrize("has_insight", [False, True])
async def test_nonpositive_limit_returns_no_insights(db, limit, has_insight):
    if has_insight:
        await _mk(db, "eligible", "learning", "a real learning")

    assert await _recent_decisions(db, limit=limit) == []
    assert await _recent_decisions(db, limit=1) == (
        ["a real learning"] if has_insight else []
    )


@pytest.mark.asyncio
async def test_stops_at_limit_distinct_entries(db):
    for i in range(12):
        await _mk(db, f"n{i}", "learning", f"distinct {i}", minutes_ago=i)

    assert len(await _recent_decisions(db)) == 10
    assert await _recent_decisions(db, limit=3) == [
        "distinct 0",
        "distinct 1",
        "distinct 2",
    ]


@pytest.mark.asyncio
async def test_shown_text_is_the_250_char_slice(db):
    await _mk(db, "long", "learning", "x" * 400)

    assert await _recent_decisions(db) == ["x" * 250]


@pytest.mark.asyncio
async def test_a_flood_of_repeats_does_not_starve_distinct_rows(db):
    """The old reader took 10 rows THEN let the caller show 5: twelve newer
    copies of one text pushed every distinct row out of the window."""
    for i in range(12):
        await _mk(db, f"f{i}", "learning", "the same text", minutes_ago=i)
    for i in range(3):
        await _mk(db, f"o{i}", "learning", f"older distinct {i}", minutes_ago=100 + i)

    out = await _recent_decisions(db)
    assert out == [
        "the same text",
        "older distinct 0",
        "older distinct 1",
        "older distinct 2",
    ]
