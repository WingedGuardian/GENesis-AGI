"""Essential Knowledge never cuts a word in half.

The block is injected at every SessionStart. Ego focus, Active Context and Key
Insights each used a raw slice, so a line could end "nothing leaves Gen".
Fixtures are synthetic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.memory.essential_knowledge import (
    _ego_focus,
    _recent_session_topics,
    generate_deterministic,
)

_PROSE = " ".join(f"topic{i}" for i in range(120))  # ~830 chars


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _set_focus(db, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO ego_state (key, value) VALUES ('ego_focus_summary', ?)",
        (value,),
    )
    await db.commit()


async def _add_session(db, sid: str, topic: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, effort, status, source_tag, channel, topic, "
        " started_at, last_activity_at) "
        "VALUES (?, 'foreground', 'opus', 'high', 'completed', 'foreground', NULL, ?, ?, ?)",
        (sid, topic, now, now),
    )
    await db.commit()


def _whole_words(shown: str, source: str) -> bool:
    return set(shown.removesuffix("…").split()) <= set(source.split())


@pytest.mark.asyncio
async def test_long_ego_focus_ends_at_a_word_boundary(db):
    await _set_focus(db, _PROSE)

    shown = await _ego_focus(db)
    assert shown is not None
    assert shown.endswith("…")
    assert len(shown) <= 200
    assert _whole_words(shown, _PROSE)


@pytest.mark.asyncio
async def test_short_ego_focus_is_unchanged(db):
    await _set_focus(db, "2 directive(s): review docs; ship notes")

    assert await _ego_focus(db) == "2 directive(s): review docs; ship notes"


@pytest.mark.asyncio
async def test_behavioral_focus_is_still_omitted(db):
    await _set_focus(db, "holding back until the user replies " + _PROSE)

    assert await _ego_focus(db) is None


@pytest.mark.asyncio
async def test_behavioral_phrase_ending_at_the_old_cut_is_still_caught(db):
    """The filter reads the old 200-char window: a word-boundary clip would
    drop a phrase whose last word ends exactly at character 200."""
    focus = "x" * 189 + " quiet mode and more text after it"
    assert focus[:200].endswith("quiet mode")
    await _set_focus(db, focus)

    assert await _ego_focus(db) is None


@pytest.mark.asyncio
async def test_long_session_topic_ends_at_a_word_boundary(db):
    await _add_session(db, "s1", _PROSE)

    [shown] = await _recent_session_topics(db)
    assert shown.endswith("…")
    assert len(shown) <= 200
    assert _whole_words(shown, _PROSE)


@pytest.mark.asyncio
async def test_short_session_topic_is_unchanged(db):
    await _add_session(db, "s1", "Fix the backup timer")

    assert await _recent_session_topics(db) == ["Fix the backup timer"]


@pytest.mark.asyncio
async def test_no_clipped_line_in_the_block_ends_mid_word(db):
    """The E2E check from the spec, run against the real generator: any line
    carrying the clip marker has only whole words before it."""
    await _set_focus(db, _PROSE)
    for i in range(5):
        await _add_session(db, f"s{i}", f"session {i} " + _PROSE)
    for i in range(5):
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, "
            "created_at, resolved, origin_class) "
            "VALUES (?, 's', 'learning', ?, 'low', ?, 0, 'first_party')",
            (f"o{i}", f"insight {i} " + _PROSE, datetime.now(UTC).isoformat()),
        )
    await db.commit()

    block = await generate_deterministic(db)
    clipped = [ln for ln in block.splitlines() if ln.endswith("…")]
    # Focus + 5 topics + 5 insights.
    assert len(clipped) == 11, block
    for line in clipped:
        body = re.sub(r"^- (Ego focus: )?", "", line)
        assert set(body.removesuffix("…").split()) <= set(
            ("session insight " + " ".join(str(i) for i in range(5)) + " " + _PROSE).split()
        ), line


@pytest.mark.asyncio
async def test_clipping_does_not_grow_the_block_past_its_old_bound(db):
    """The SessionStart knowledge part has a 9,800-char budget
    (scripts/genesis_session_context.py _PART_BUDGET). Raising the insight
    read to 1,000 chars must not raise what is SHOWN: every clipped field stays
    at or under its old slice length."""
    await _set_focus(db, "f" + " x" * 2000)
    for i in range(10):
        await _add_session(db, f"s{i}", f"t{i}" + " y" * 2000)
    for i in range(10):
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, "
            "created_at, resolved, origin_class) "
            "VALUES (?, 's', 'learning', ?, 'low', ?, 0, 'first_party')",
            (f"o{i}", f"i{i}" + " z" * 2000, datetime.now(UTC).isoformat()),
        )
    await db.commit()

    block = await generate_deterministic(db)
    lines = block.splitlines()
    focus = [ln for ln in lines if ln.startswith("- Ego focus: ")]
    assert focus and len(focus[0]) <= len("- Ego focus: ") + 200
    topics = [ln for ln in lines if ln.startswith("- t")]
    insights = [ln for ln in lines if ln.startswith("- i")]
    assert len(topics) == 5 and len(insights) == 5
    assert all(len(ln) <= 2 + 200 for ln in topics)
    assert all(len(ln) <= 2 + 250 for ln in insights)
    assert len(block) < 9_800
