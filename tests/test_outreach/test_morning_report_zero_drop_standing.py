"""A persisting zero-drop fault climbs to the morning report's Standing Items.

Standing Items lists observations surfaced 3 or more times (``get_standing``).
A zero-drop alert row is surfaced in "What I Noticed" once, and the detector
replaces it with a new row whenever its text changes. Before the carry-over,
each replacement started at a count of 0 and was shown as NEW again, so a fault
that lasted weeks never reached Standing Items.

These tests drive the real detector writer into the real report (generate,
then confirm_delivery) across several simulated report days, and read the
sections of the context the report was drafted from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.content.types import DraftResult, FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach.morning_report import MorningReportGenerator
from genesis.session_awareness import zero_drop_worker as w

_MARK = "detector is BLIND"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def gen(db):
    health = AsyncMock()
    health.snapshot.return_value = {"timestamp": "2026-03-12T07:00:00Z"}
    drafter = AsyncMock()
    drafter.draft.return_value = DraftResult(
        content=FormattedContent(
            text="Good morning.", target=FormatTarget.GENERIC, truncated=False, original_length=13
        ),
        model_used="test",
        raw_draft="Good morning.",
    )
    return MorningReportGenerator(health, db, drafter)


def _section(context: str, title: str) -> str:
    for block in context.split("\n## "):
        if block.startswith(title):
            return block
    return ""


async def _report(gen: MorningReportGenerator, *, deliver: bool = True) -> tuple[bool, bool]:
    """One report: whether the fault is in What I Noticed, and in Standing Items."""
    await gen.generate()
    context = gen._drafter.draft.call_args.args[0].context
    if deliver:
        await gen.confirm_delivery()
    return (
        _MARK in _section(context, "What I Noticed"),
        _MARK in _section(context, "Standing Items"),
    )


async def test_a_persisting_blind_fault_reaches_standing_items(db, gen):
    causes = ["gh auth expired", "gh rate limited", "gh auth expired again", "gh timeout"]
    days = []
    for cause in causes:
        # The detector re-mints the row between reports: a new cause each day.
        await w._maintain_blind_alert(db, degraded={"prs": cause}, frozen=[])
        days.append(await _report(gen))

    assert [noticed for noticed, _ in days] == [True, False, False, False], (
        "a carried replacement was shown as NEW again"
    )
    # Surfaced in reports 1, 2 and 3, so it stands from report 4.
    assert [standing for _, standing in days] == [False, False, False, True]


async def test_an_unchanged_blind_fault_also_reaches_standing_items(db, gen):
    """No replacement at all: the same open row persists across reports."""
    days = []
    for _ in range(4):
        await w._maintain_blind_alert(db, degraded={"prs": "gh auth expired"}, frozen=[])
        days.append(await _report(gen))
    assert [standing for _, standing in days] == [False, False, False, True]


async def test_a_resolved_blind_fault_does_not_reach_standing_items(db, gen):
    """The fault clears after two reports and returns later: a new problem,
    counted from zero, and shown as new again."""
    days = []
    for degraded in (
        {"prs": "gh auth expired"},
        {"prs": "gh auth expired"},
        {},
        {"prs": "gh rate limited"},
        {"prs": "gh rate limited"},
    ):
        await w._maintain_blind_alert(db, degraded=degraded, frozen=[])
        days.append(await _report(gen))
    assert [noticed for noticed, _ in days] == [True, False, False, True, False]
    assert [standing for _, standing in days] == [False] * 5


async def test_an_undelivered_report_does_not_count(db, gen):
    """Only a confirmed delivery counts a surfacing."""
    await w._maintain_blind_alert(db, degraded={"prs": "gh auth expired"}, frozen=[])
    await _report(gen)
    for _ in range(3):
        await _report(gen, deliver=False)
    cursor = await db.execute(
        "SELECT surfaced_count FROM observations WHERE source = ? AND resolved = 0",
        (w.BLIND_SOURCE,),
    )
    assert (await cursor.fetchone())["surfaced_count"] == 1


async def test_a_row_opened_after_the_report_was_written_is_not_counted(db, gen):
    """The persisting set is read when the report is generated, so a row that
    became open and surfaced afterwards (a page, a dashboard mark-read) is not
    counted for a report that never stated it."""
    from genesis.db.crud import observations

    await gen.generate()
    await w._maintain_blind_alert(db, degraded={"prs": "gh auth expired"}, frozen=[])
    cursor = await db.execute("SELECT id FROM observations WHERE source = ?", (w.BLIND_SOURCE,))
    row_id = (await cursor.fetchone())["id"]
    await observations.mark_surfaced(db, [row_id], "2026-03-12T10:00:00+00:00")
    await gen.confirm_delivery()
    cursor = await db.execute("SELECT surfaced_count FROM observations WHERE id = ?", (row_id,))
    assert (await cursor.fetchone())["surfaced_count"] == 1


async def _seed_standing(db, *, created_at: str, surfaced_at: str) -> None:
    from genesis.db.crud import outreach

    # Two report rows at the same time, so the previous-report lookup returns
    # it whether it reads the newest row or the one before.
    for i in range(2):
        await outreach.create(
            db,
            id=f"report-{i}",
            signal_type="morning_report",
            topic="Morning Report",
            category="digest",
            salience_score=0.0,
            channel="telegram",
            message_content="x",
            created_at="2026-03-11T11:00:00+00:00",
        )
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at, "
        "surfaced_at, surfaced_count) VALUES ('o1', ?, ?, 'The zero-drop detector is BLIND "
        "on: prs.', 'high', ?, ?, 3)",
        (w.BLIND_SOURCE, w.ALERT_TYPE, created_at, surfaced_at),
    )
    await db.commit()


async def test_a_replacement_written_since_the_last_report_is_not_unchanged(db, gen):
    """A carried row keeps its predecessor's FIRST surfaced time. Read alone,
    that marks new text [unchanged], and the drafter compresses it away."""
    await _seed_standing(
        db, created_at="2026-03-12T08:00:00+00:00", surfaced_at="2026-03-09T11:00:00+00:00"
    )
    standing = await gen._get_standing_items()
    assert _MARK in standing
    assert "[unchanged]" not in standing, standing


async def test_a_row_untouched_since_the_last_report_is_still_unchanged(db, gen):
    """Control: the tag still marks a row written and surfaced before the last report."""
    await _seed_standing(
        db, created_at="2026-03-09T08:00:00+00:00", surfaced_at="2026-03-09T11:00:00+00:00"
    )
    assert "[unchanged]" in await gen._get_standing_items()
