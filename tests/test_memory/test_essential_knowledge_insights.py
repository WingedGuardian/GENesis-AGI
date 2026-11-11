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
async def test_shown_text_is_clipped_to_250_chars(db):
    # A single word longer than the limit is the one case cut mid-word.
    await _mk(db, "long", "learning", "x" * 400)

    assert await _recent_decisions(db) == ["x" * 249 + "…"]


_PROSE = " ".join(f"word{i}" for i in range(200))  # ~1,190 chars, synthetic


@pytest.mark.asyncio
async def test_long_insight_ends_at_a_word_boundary(db):
    await _mk(db, "long", "learning", _PROSE)

    [shown] = await _recent_decisions(db)
    assert shown.endswith("…")
    assert len(shown) <= 250
    assert set(shown[:-1].split()) <= set(_PROSE.split())


@pytest.mark.asyncio
async def test_rows_differing_only_past_the_display_cut_show_once(db):
    """Two rows that share their first 250 chars but differ later would print
    as two IDENTICAL lines; only the newer is shown."""
    shared = "a " * 150  # 300 chars of common prefix
    await _mk(db, "a", "learning", shared + "ending one", minutes_ago=0)
    await _mk(db, "b", "learning", shared + "ending two", minutes_ago=1)
    await _mk(db, "c", "learning", "something else", minutes_ago=2)

    out = await _recent_decisions(db)
    assert len(out) == 2
    assert len(set(out)) == len(out), "no two shown lines may look identical"
    assert out[1] == "something else"


@pytest.mark.asyncio
async def test_rows_differing_only_in_the_word_the_clip_drops_show_once(db):
    """The word crossing the 250-char boundary is dropped by the clip, so rows
    that differ only inside it print identically even though their raw
    250-char windows differ; they must not take five slots and starve an
    older distinct row."""
    for i in range(5):
        text = "context " * 30 + f"variant{i}xxxxxxxxxxxx extra details"
        await _mk(db, f"v{i}", "learning", text, minutes_ago=i)
    await _mk(db, "o", "learning", "an older distinct insight", minutes_ago=10)

    out = await _recent_decisions(db)
    assert len(set(out)) == len(out), "no two shown lines may look identical"
    assert out[-1] == "an older distinct insight"


@pytest.mark.asyncio
async def test_a_whole_short_row_and_a_longer_one_with_its_opening_show_once(db):
    """A <=250-char row is shown whole, a longer row with the same opening is
    shown clipped with "…"; they must not print as two near-identical lines."""
    short = ("word " * 50).strip()  # 249 chars
    await _mk(db, "a", "learning", short + " and then much more text follows", minutes_ago=0)
    await _mk(db, "b", "learning", short + " ", minutes_ago=1)

    assert len(await _recent_decisions(db)) == 1


@pytest.mark.asyncio
async def test_rows_differing_within_the_display_window_are_both_kept(db):
    shared = "a " * 50  # 100 chars, well inside the 250 shown
    await _mk(db, "a", "learning", shared + "ending one", minutes_ago=0)
    await _mk(db, "b", "learning", shared + "ending two", minutes_ago=1)

    assert len(await _recent_decisions(db)) == 2


@pytest.mark.asyncio
async def test_rows_identical_in_the_first_1000_chars_collapse(db):
    shared = "b " * 600  # 1,200 chars
    await _mk(db, "a", "learning", shared + "tail one", minutes_ago=0)
    await _mk(db, "b", "learning", shared + "tail two", minutes_ago=1)

    assert len(await _recent_decisions(db)) == 1


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
