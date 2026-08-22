"""WS-3: essential_knowledge L1 readers exclude external/unknown-origin rows.

The essential_knowledge.md file is the always-loaded L1 context. Its two
observation readers (_recent_decisions, _active_session_pivots) must never
surface external_untrusted or NULL-origin (unknown) content — that would put
attacker-controlled text into every session's L1. Own-session content stamps
`owner`/`first_party` and is kept.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.memory.essential_knowledge import (
    _active_session_pivots,
    _recent_decisions,
)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _mk(db, oid, typ, origin, *, source="s", content=None, when=None):
    when = when or datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, "
        "created_at, resolved, origin_class) VALUES (?,?,?,?,?,?,0,?)",
        (oid, source, typ, content or f"content-{oid}", "low", when, origin),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_recent_decisions_excludes_external_and_null(db):
    await _mk(db, "d_owner", "insight", "owner")
    await _mk(db, "d_fp", "insight", "first_party")
    await _mk(db, "d_ext", "insight", "external_untrusted")
    await _mk(db, "d_null", "insight", None)

    out = await _recent_decisions(db)
    joined = "\n".join(out)
    assert "content-d_owner" in joined
    assert "content-d_fp" in joined
    assert "content-d_ext" not in joined
    assert "content-d_null" not in joined


@pytest.mark.asyncio
async def test_active_session_pivots_excludes_external_and_null(db):
    # Each pivot source must be distinct (the reader GROUPs BY source).
    await _mk(
        db,
        "p_owner",
        "conversation_pivot",
        "owner",
        source="session:owner-sess",
        content="Conversation pivot: topic. Trigger: OWNER_TRIGGER",
    )
    await _mk(
        db,
        "p_ext",
        "conversation_pivot",
        "external_untrusted",
        source="session:ext-sess",
        content="Conversation pivot: topic. Trigger: EXT_TRIGGER",
    )
    await _mk(
        db,
        "p_null",
        "conversation_pivot",
        None,
        source="session:null-sess",
        content="Conversation pivot: topic. Trigger: NULL_TRIGGER",
    )

    out = await _active_session_pivots(db)
    joined = "\n".join(out)
    assert "OWNER_TRIGGER" in joined
    assert "EXT_TRIGGER" not in joined
    assert "NULL_TRIGGER" not in joined
