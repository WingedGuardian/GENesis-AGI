"""Tests for the DM session-recovery context builder (real scroll-up recap).

Origin: 2026-08-18 — the user returned to a 5-day-old TARS thread and Genesis
couldn't see "option 3" because the recap truncated each message to its FIRST
300 chars while the numbered options sat at char ~3,850 of a 4,463-char reply.
The recap is now byte-budgeted and TAIL-biased (conclusions/option lists live
at the end of long analytical replies).
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.cc.conversation import ConversationLoop
from genesis.db.crud.telegram_messages import store
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _loop(db) -> ConversationLoop:
    handler = ConversationLoop.__new__(ConversationLoop)
    handler._db = db
    return handler


LONG_REPLY = (
    "Here is the full TARS breakdown with a lot of detail. "
    + ("Analysis paragraph. " * 180)  # ~3,600 chars of body
    + "\nThree real options:\n"
    "1. Free prompt pack\n"
    "2. Paid community\n"
    "3. Reverse-build it OPTION-THREE-MARKER"
)


@pytest.mark.asyncio
async def test_recovery_context_keeps_tail_of_long_reply(db):
    """The END of a long prior reply (where option lists / conclusions live)
    must survive into the recap."""
    await store(
        db,
        chat_id=555000111,
        message_id=1,
        sender="user",
        content="Fetch this video about TARS",
        timestamp="2026-08-13T04:03:00",
    )
    await store(
        db,
        chat_id=555000111,
        message_id=2,
        sender="genesis",
        content=LONG_REPLY,
        timestamp="2026-08-13T04:15:00",
        direction="outbound",
    )
    ctx = await _loop(db)._build_recovery_context("tg-555000111", "telegram", None)
    assert "OPTION-THREE-MARKER" in ctx, "tail of a long reply must survive the recap truncation"


@pytest.mark.asyncio
async def test_recovery_context_respects_total_budget(db):
    """Total recap size stays within the byte budget while the NEWEST
    messages are always represented."""
    for i in range(30):
        await store(
            db,
            chat_id=100,
            message_id=i,
            sender="user",
            content=f"msg-{i:02d} " + ("filler " * 300),  # ~2,100 chars each
            timestamp=f"2026-08-13T04:{i:02d}:00",
        )
    loop = _loop(db)
    ctx = await loop._build_recovery_context("tg-100", "telegram", None)
    budget = ConversationLoop.RECOVERY_CONTEXT_BUDGET
    assert len(ctx) <= budget + 500, f"recap blew the budget: {len(ctx)}"
    assert "msg-29" in ctx, "the newest message must always be present"


@pytest.mark.asyncio
async def test_recovery_context_empty_cases(db):
    """No messages → empty string; non-telegram user ids → empty string."""
    loop = _loop(db)
    assert await loop._build_recovery_context("tg-999", "telegram", None) == ""
    assert await loop._build_recovery_context("dashboard-1", "dashboard", None) == ""


def test_conversation_identity_block():
    """Fresh telegram sessions get an identity block naming their own chat_id
    and the scoped scroll-up call — without it, "scoped to this chat" is
    unimplementable (the model has no way to know its chat id)."""
    block = ConversationLoop._conversation_identity_block(
        "tg-555000111", "telegram", None,
    )
    assert "555000111" in block
    assert "conversation_history" in block
    assert "chat_id=555000111" in block
    # Non-telegram and malformed ids produce no block.
    assert ConversationLoop._conversation_identity_block(
        "dashboard-1", "dashboard", None,
    ) == ""
    assert ConversationLoop._conversation_identity_block(
        "tg-notanumber", "telegram", None,
    ) == ""


@pytest.mark.asyncio
async def test_recovery_context_tail_keeps_oversized_newest_message(db):
    """A single newest message LARGER than the whole budget must still be
    represented — by its TAIL (audit F2: the truncation branch itself must be
    exercised; a head-keep regression must fail here)."""
    await store(
        db, chat_id=100, message_id=1, sender="genesis",
        content=("HEAD-MARKER " + "x" * 8000 + " END-MARKER"),
        timestamp="2026-08-13T04:00:00", direction="outbound",
    )
    ctx = await _loop(db)._build_recovery_context("tg-100", "telegram", None)
    assert "END-MARKER" in ctx, "oversized newest message must keep its tail"
    assert "HEAD-MARKER" not in ctx, "head-biased truncation regression"
    assert "…" in ctx, "truncation must be marked"


@pytest.mark.asyncio
async def test_recovery_context_budget_is_byte_based(db):
    """The recap budget is BYTES, not characters. A multibyte-heavy transcript
    under a char budget balloons up to ~3-4x the intended size; the byte budget
    caps the real payload. The tail cut must stay on a character boundary, so the
    recap remains valid UTF-8 (no U+FFFD replacement chars)."""
    # '緑' is 3 UTF-8 bytes; 1500 of them ≈ 4,500 bytes per message.
    for i in range(30):
        await store(
            db,
            chat_id=100,
            message_id=i,
            sender="user",
            content=f"m{i:02d} " + ("緑" * 1500),
            timestamp=f"2026-08-13T04:{i:02d}:00",
        )
    ctx = await _loop(db)._build_recovery_context("tg-100", "telegram", None)
    budget = ConversationLoop.RECOVERY_CONTEXT_BUDGET
    # Exact bound: separators ("\n" between lines) are charged too, so the real
    # recap size never exceeds the budget.
    assert len(ctx.encode()) <= budget, (
        f"recap blew the BYTE budget: {len(ctx.encode())} bytes for a {budget}-byte cap"
    )
    assert "�" not in ctx, "tail cut split a multibyte char (invalid UTF-8)"
    assert "m29" in ctx, "the newest message must always be present"


def test_conversation_identity_block_group_chat_id():
    """Group/topic chats have NEGATIVE chat ids and must produce a correct
    block (audit finding: sender-id conflation would name the wrong chat)."""
    block = ConversationLoop._conversation_identity_block(
        "-100999888777", "telegram", "109",
    )
    assert "chat_id=-100999888777" in block
    assert "thread_id=109" in block


def test_conversation_identity_block_thread_scoped_suggestion():
    """Codex P2 lock: in a forum-topic session the SUGGESTED scroll-up call
    must be thread-scoped, or following it pulls unrelated topics."""
    block = ConversationLoop._conversation_identity_block(
        "-100999888777", "telegram", "109",
    )
    assert "chat_id=-100999888777, thread_id=109, limit=50" in block
    dm_block = ConversationLoop._conversation_identity_block(
        "555000111", "telegram", None,
    )
    assert "chat_id=555000111, limit=50" in dm_block
