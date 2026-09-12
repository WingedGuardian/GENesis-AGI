"""WS-M PR-2: idea-lane decay + inbox_digest Ideas section."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import genesis.db.crud.follow_ups as fu


def _old(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def _mk(db, id, created_at, *, source="surplus_ideation", kind="idea"):
    await fu.create(
        db,
        content=f"c-{id}",
        source=source,
        strategy="surplus_task",
        kind=kind,
        domain="internal",
        dedup_key=id,
        id=id,
    )
    await db.execute("UPDATE follow_ups SET created_at = ? WHERE id = ?", (created_at, id))
    await db.commit()


async def _status(db, id: str) -> str:
    cur = await db.execute("SELECT status FROM follow_ups WHERE id = ?", (id,))
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_decay_stale_ideas_only_old_idea_lane(db):
    await _mk(db, "old", _old(60))
    await _mk(db, "new", _old(5))
    await _mk(db, "marker", _old(90), source="inbox_evaluation", kind="tabled")

    n = await fu.decay_stale_ideas(db, older_than_days=45)

    assert n == 1
    assert await _status(db, "old") == "completed"  # aged out
    assert await _status(db, "new") == "pending"  # too recent
    assert await _status(db, "marker") == "pending"  # different lane, untouched


@pytest.mark.asyncio
async def test_decay_inbox_markers_still_targets_tabled(db):
    """The refactor to a shared _decay_stale keeps the inbox delegate scoped to
    source='inbox_evaluation' AND kind='tabled'."""
    await _mk(db, "marker", _old(70), source="inbox_evaluation", kind="tabled")
    await _mk(db, "idea", _old(70))

    n = await fu.decay_stale_inbox_markers(db, older_than_days=60)

    assert n == 1
    assert await _status(db, "marker") == "completed"
    assert await _status(db, "idea") == "pending"  # idea lane untouched by inbox decay


def test_format_digest_renders_ideas_section():
    from genesis.mcp.health.inbox_digest import _format_digest

    out = _format_digest(
        [],
        [],
        [],
        7,
        ideas=[{"content": "Dynamic Awareness Depth Tuning", "source": "surplus_ideation"}],
    )
    assert "Pending Ideas (1 items)" in out
    assert "Awareness Depth" in out


def test_format_digest_backcompat_without_ideas():
    from genesis.mcp.health.inbox_digest import _format_digest

    out = _format_digest([], [], [], 7)  # positional legacy call, no ideas arg
    assert "No inbox activity" in out
