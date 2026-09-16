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
# ── _recent_session_topics: the third L1 reader ────────────────────────────
#
# The two readers above filter on observation ORIGIN. Session topics are a
# separate path with a separate column (cc_sessions.channel), and it was
# unfiltered: a gateway-channel session topic could reach L1, which is
# injected into every session. These pin the channel filter.


_NOW = datetime.now(UTC).isoformat()


async def _insert_session(db, sid, channel, topic):
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, effort, status, source_tag, channel, topic, "
        " started_at, last_activity_at) "
        "VALUES (?, 'foreground', 'opus', 'high', 'active', 'foreground', ?, ?, ?, ?)",
        (sid, channel, topic, _NOW, _NOW),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_agent_channel_topic_is_excluded_from_l1(db):
    """Content an external agent chose must not become context every session reads."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session(db, "s-agent", "agent", "topic from an outside caller")
    assert "topic from an outside caller" not in await _recent_session_topics(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["web", "whatsapp", "voice", "agent"])
async def test_no_gateway_channel_reaches_l1(db, channel):
    """The whole gateway class, not just the one this change added."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session(db, f"s-{channel}", channel, f"topic-{channel}")
    assert f"topic-{channel}" not in await _recent_session_topics(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["terminal", "telegram"])
async def test_positive_control_owner_channels_still_reach_l1(db, channel):
    """Without this, a filter that excluded EVERYTHING would look correct."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session(db, f"s-{channel}", channel, f"topic-{channel}")
    assert f"topic-{channel}" in await _recent_session_topics(db)


@pytest.mark.asyncio
async def test_legacy_null_channel_rows_still_reach_l1(db):
    """Rows predate channel stamping and are owner terminal sessions;
    excluding them would empty L1 rather than secure it."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session(db, "s-legacy", None, "legacy topic")
    assert "legacy topic" in await _recent_session_topics(db)


def test_allowlist_is_derived_from_the_predicate_not_hardcoded():
    """A second literal channel list would drift the next time one is added.
    This asserts the query set IS the predicate set, for every member."""
    from genesis.cc.types import ChannelType, is_owner_attended_channel
    from genesis.memory.essential_knowledge import _owner_attended_channel_values

    allowed = set(_owner_attended_channel_values())
    expected = {c.value for c in ChannelType if is_owner_attended_channel(c)}
    assert allowed == expected
    assert ChannelType.AGENT.value not in allowed
