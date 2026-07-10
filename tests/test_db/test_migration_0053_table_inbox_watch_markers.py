"""Migration 0053 — re-classify existing inbox WATCH/BOOKMARK follow-ups.

Verifies pending WATCH/BOOKMARK inbox rows move to the tabled lane, that
ADOPT/ADAPT/EXPLORE rows and already-completed markers are left untouched, and
that up() is idempotent. Mirrors the 0052 single-migration test style.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M53 = importlib.import_module(
    "genesis.db.migrations.0053_table_inbox_watch_markers"
)

_DDL = """
    CREATE TABLE follow_ups (
        id TEXT PRIMARY KEY, source TEXT, content TEXT, status TEXT,
        kind TEXT DEFAULT 'follow_up'
    )
"""


async def _seed(db, id_, *, source, content, status, kind="follow_up"):
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, status, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (id_, source, content, status, kind),
    )


async def _kind(db, id_) -> str:
    cur = await db.execute("SELECT kind FROM follow_ups WHERE id = ?", (id_,))
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_up_tables_all_non_terminal_watch_bookmark(tmp_path):
    """Every non-terminal state (pending/blocked/in_progress/scheduled) moves —
    the exact set decay_stale_inbox_markers reaps, so none is left immortal."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        await _seed(db, "w", source="inbox_evaluation",
                    content="[WATCH] a", status="pending")
        await _seed(db, "b", source="inbox_evaluation",
                    content="[BOOKMARK] c", status="pending")
        await _seed(db, "blk", source="inbox_evaluation",
                    content="[WATCH] blocked", status="blocked")
        await _seed(db, "prog", source="inbox_evaluation",
                    content="[WATCH] wip", status="in_progress")
        await _seed(db, "sched", source="inbox_evaluation",
                    content="[BOOKMARK] later", status="scheduled")
        await db.commit()

        await M53.up(db)

        for id_ in ("w", "b", "blk", "prog", "sched"):
            assert await _kind(db, id_) == "tabled", id_


@pytest.mark.asyncio
async def test_up_leaves_actionable_and_terminal_rows(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        # Actionable lanes — never moved.
        await _seed(db, "adopt", source="inbox_evaluation",
                    content="[ADOPT] a", status="pending")
        await _seed(db, "adapt", source="inbox_evaluation",
                    content="[ADAPT] a", status="pending")
        await _seed(db, "explore", source="inbox_evaluation",
                    content="[EXPLORE] a", status="pending")
        # Terminal WATCH rows — history preserved (purge_completed reaps them).
        await _seed(db, "done", source="inbox_evaluation",
                    content="[WATCH] old", status="completed")
        await _seed(db, "failed", source="inbox_evaluation",
                    content="[WATCH] failed", status="failed")
        # A WATCH-looking row from another source — not an inbox marker.
        await _seed(db, "other", source="manual",
                    content="[WATCH] x", status="pending")
        await db.commit()

        await M53.up(db)

        for id_ in ("adopt", "adapt", "explore", "done", "failed", "other"):
            assert await _kind(db, id_) == "follow_up", id_


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        await _seed(db, "w", source="inbox_evaluation",
                    content="[WATCH] a", status="pending")
        await db.commit()

        await M53.up(db)
        await M53.up(db)  # second run must not raise or double-move
        assert await _kind(db, "w") == "tabled"
