"""Direct unit tests for the email_threads crud read helpers.

Focus on the SECURITY-critical predicates the email gate relies on:
``has_inbound`` (is a send cold outreach?) and ``recipient_in_thread``
(is this recipient an established reply-sender?). The non-match-on-NULL/blank
sender semantics are verified here without the full gate machinery, per the
PR #786 review WARNING.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import email_threads as et

pytestmark = pytest.mark.asyncio


async def _register(db, *, recipient="alice@example.com") -> str:
    return await et.register_thread(
        db, sent_message_id="<msg-1@local>", recipient=recipient,
        owner="outreach", subject="hello",
    )


# ── has_inbound ──────────────────────────────────────────────────────────

async def test_has_inbound_false_for_cold_thread(db):
    """A freshly registered thread has only the sent message → not inbound."""
    thread_id = await _register(db)
    assert await et.has_inbound(db, thread_id) is False


async def test_has_inbound_true_after_reply(db):
    """Once a received message lands, the thread is no longer cold."""
    thread_id = await _register(db)
    await et.record_reply(
        db, thread_id=thread_id, message_id="<reply-1@remote>",
        sender="alice@example.com",
    )
    assert await et.has_inbound(db, thread_id) is True


async def test_has_inbound_true_even_when_sender_unparsed(db):
    """An inbound exists even if its sender could not be parsed (NULL)."""
    thread_id = await _register(db)
    await db.execute(
        "INSERT INTO email_thread_messages "
        "(thread_id, message_id, direction, sender, subject, body_preview, received_at) "
        "VALUES (?, ?, 'received', NULL, NULL, NULL, ?)",
        (thread_id, "<reply-null@remote>", "2026-06-25T00:00:00+00:00"),
    )
    await db.commit()
    assert await et.has_inbound(db, thread_id) is True


# ── recipient_in_thread (SECURITY scope guard) ───────────────────────────

async def test_recipient_in_thread_matches_actual_sender(db):
    thread_id = await _register(db)
    await et.record_reply(
        db, thread_id=thread_id, message_id="<reply-1@remote>",
        sender="alice@example.com",
    )
    assert await et.recipient_in_thread(db, thread_id, "alice@example.com") is True


async def test_recipient_in_thread_rejects_other_recipient(db):
    thread_id = await _register(db)
    await et.record_reply(
        db, thread_id=thread_id, message_id="<reply-1@remote>",
        sender="alice@example.com",
    )
    assert await et.recipient_in_thread(db, thread_id, "mallory@evil.com") is False


async def test_recipient_in_thread_does_not_match_null_sender(db):
    """A received row with NULL sender must NOT grant scope to a blank recipient.

    The safe failure for the SECURITY guard is to trip and hold, not wave a
    single unparsed-sender row through as unbounded recipient scope.
    """
    thread_id = await _register(db)
    await db.execute(
        "INSERT INTO email_thread_messages "
        "(thread_id, message_id, direction, sender, subject, body_preview, received_at) "
        "VALUES (?, ?, 'received', NULL, NULL, NULL, ?)",
        (thread_id, "<reply-null@remote>", "2026-06-25T00:00:00+00:00"),
    )
    await db.commit()
    assert await et.recipient_in_thread(db, thread_id, "") is False
    assert await et.recipient_in_thread(db, thread_id, "alice@example.com") is False


async def test_recipient_in_thread_ignores_sent_direction(db):
    """The recipient on the outbound message is not a reply-sender."""
    thread_id = await _register(db, recipient="alice@example.com")
    # No reply recorded; only the 'sent' row exists with sender=recipient.
    assert await et.recipient_in_thread(db, thread_id, "alice@example.com") is False
