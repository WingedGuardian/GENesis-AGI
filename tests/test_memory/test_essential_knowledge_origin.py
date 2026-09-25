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


async def _insert_session(db, sid, channel, topic):
    # Read the clock HERE, not at import: a module-level capture makes the
    # margin the whole suite runtime instead of one test.
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, effort, status, source_tag, channel, topic, "
        " started_at, last_activity_at) "
        "VALUES (?, 'foreground', 'opus', 'high', 'active', 'foreground', ?, ?, ?, ?)",
        (sid, channel, topic, now, now),
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
async def test_unstamped_null_channel_rows_still_reach_l1(db):
    """Rows predate channel stamping and are owner terminal sessions;
    excluding them would empty L1 rather than secure it."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session(db, "s-unstamped", None, "unstamped topic")
    assert "unstamped topic" in await _recent_session_topics(db)


def test_allowlist_is_derived_from_the_predicate_not_hardcoded():
    """A second literal channel list would drift the next time one is added.
    This asserts the query set IS the predicate set, for every member."""
    from genesis.cc.types import ChannelType, is_owner_attended_channel
    from genesis.memory.essential_knowledge import _owner_attended_channel_values

    allowed = set(_owner_attended_channel_values())
    expected = {c.value for c in ChannelType if is_owner_attended_channel(c)}
    assert allowed == expected
    assert ChannelType.AGENT.value not in allowed


def test_the_allowlist_TRACKS_the_predicate_when_it_changes(monkeypatch):
    """The assertion above cannot fail for a hardcoded list, so it is not the
    one that pins the claim.

    It computes ``expected`` the same way the implementation does, so any
    implementation returning today's membership satisfies it -- a literal
    ``("terminal", "telegram")`` passes, which a mutation run confirmed. What
    the derivation actually buys is that the set MOVES when the predicate
    moves, so that is what this asserts.

    Note the drift being prevented is fail-CLOSED, not fail-open: an allowlist
    cannot admit a member nobody listed. The risk a literal carries is the
    opposite one -- a newly owner-attended channel silently losing its L1
    context, with nothing failing to say so.
    """
    import genesis.cc.types as cc_types
    from genesis.memory.essential_knowledge import _owner_attended_channel_values

    real = cc_types.is_owner_attended_channel
    monkeypatch.setattr(
        cc_types,
        "is_owner_attended_channel",
        lambda c: True if c is cc_types.ChannelType.VOICE else real(c),
    )
    assert cc_types.ChannelType.VOICE.value in set(_owner_attended_channel_values())
# ── origin_class branch of the L1 filter ───────────────────────────────────
#
# The channel tests above never set origin_class, so deleting the entire
# origin clause left them all green. These cover that branch, in both
# directions, and pin its POLARITY: an allowlist taken from the canonical
# SAFE_SURFACING_ORIGINS, not a hand-written list of things to exclude.


async def _insert_session_with_origin(db, sid, channel, topic, origin_class):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, effort, status, source_tag, channel, topic, "
        " origin_class, started_at, last_activity_at) "
        "VALUES (?, 'foreground', 'opus', 'high', 'active', 'foreground', "
        "        ?, ?, ?, ?, ?)",
        (sid, channel, topic, origin_class, now, now),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_external_untrusted_origin_is_excluded_even_with_null_channel(db):
    """The NULL-channel branch is the one an UNSTAMPED row uses -- written
    continuously by register_from_filesystem, not a historical artifact. A row explicitly
    stamped external must not ride in on it."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session_with_origin(db, "s-ext", None, "external topic", "external_untrusted")
    assert "external topic" not in await _recent_session_topics(db)


@pytest.mark.asyncio
async def test_an_unrecognised_origin_class_is_also_excluded(db):
    """POLARITY. An exclude-list admits whatever nobody thought of; this query
    feeds every session, so the default must be to refuse."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session_with_origin(
        db, "s-future", None, "future topic", "some_origin_added_later"
    )
    assert "future topic" not in await _recent_session_topics(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["owner", "first_party"])
async def test_control_safe_origins_still_reach_l1(db, origin):
    """Without this, a filter that excluded every stamped row would pass."""
    from genesis.memory.essential_knowledge import _recent_session_topics

    await _insert_session_with_origin(db, f"s-{origin}", "terminal", f"topic-{origin}", origin)
    assert f"topic-{origin}" in await _recent_session_topics(db)


def test_safe_origin_set_IS_the_canonical_constant():
    """Identity, not equality, and against the CANONICAL constant.

    Two separate defects are closed here. The earlier version imported
    ``provenance._SAFE_ORIGINS`` and asserted equality against it -- which
    pinned exactly the coupling ``_safe_origin_values`` documents as wrong
    (that set mirrors ``immunity.is_blockable``, a different policy that merely
    agrees today), and would have pointed a maintainer at L1 to re-widen it for
    a blockability change. The canonical relationship is already pinned in
    tests/test_db/test_observations.py.

    And equality could not fail for a HARDCODED tuple: a mutation replacing the
    body with ``("owner", "first_party")`` left the suite green. ``is`` asserts
    the function RETURNS the constant rather than something equal to it, which
    a literal cannot satisfy.
    """
    from genesis.db.crud.observations import SAFE_SURFACING_ORIGINS
    from genesis.memory.essential_knowledge import _safe_origin_values

    assert _safe_origin_values() is SAFE_SURFACING_ORIGINS
    assert "external_untrusted" not in _safe_origin_values()
