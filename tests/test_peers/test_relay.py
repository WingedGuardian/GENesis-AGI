"""The owner-relay helpers in peers/relay.py, directly."""

from uuid import uuid4

import aiosqlite
import pytest

from genesis.peers import relay


def test_framing_without_the_grant_keeps_the_authority_line():
    text = relay.framing(False, "interact", can_interact=True)
    assert text == relay.AUTHORITY_LINE + " " + relay.LOOKUP_RULE


@pytest.mark.parametrize(
    ("profile", "can_interact", "extra"),
    [
        ("interact", True, None),
        ("observe", True, relay.OBSERVE_RESEND),
        ("observe", False, relay.OBSERVE_NO_ACTION),
    ],
)
def test_framing_with_the_grant(profile, can_interact, extra):
    text = relay.framing(True, profile, can_interact=can_interact)
    assert text.startswith(relay.RELAY_FRAMING) and text.endswith(relay.LOOKUP_RULE)
    assert relay.AUTHORITY_LINE not in text
    for sentence in (relay.OBSERVE_RESEND, relay.OBSERVE_NO_ACTION):
        assert (sentence in text) is (sentence == extra)


def test_acting_skips_read_only_tools_and_keeps_unlisted_ones():
    names = [
        "Read",
        "mcp__genesis-memory__memory_recall",
        "mcp__genesis-outreach__outreach_send",
        "mcp__other__memory_recall",
        "write__Read",
    ]
    assert relay.acting(names) == ["Read", "memory_recall", "outreach_send"]
    assert relay.acting(None) == []


def test_quoted_caps_and_flattens_the_summary():
    assert relay.quoted(None) == "No summary recorded."
    assert relay.quoted("  one\n two ") == 'Session said: "one two"'
    long = relay.quoted("x" * 500)
    assert len(long) == len('Session said: ""') + relay.SUMMARY_CHARS and long.endswith('..."')


def test_notice_id_is_fixed_per_session():
    sid = str(uuid4())
    assert relay.notice_id(sid) == relay.notice_id(sid) != relay.notice_id(str(uuid4()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "granted"), [("allow", True), ("ask", False), ("deny", False), (None, False)]
)
async def test_granted_reads_only_an_allow(decision, granted):
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE peer_grants (peer_id TEXT, capability TEXT, decision TEXT, "
            "PRIMARY KEY (peer_id, capability))"
        )
        await db.execute("INSERT INTO peer_grants VALUES ('alpha', 'conversation', 'allow')")
        if decision:
            await db.execute(
                "INSERT INTO peer_grants VALUES ('alpha', 'owner_relay', ?)", (decision,)
            )
        assert await relay.granted(db, {"peer_id": "alpha"}) is granted
