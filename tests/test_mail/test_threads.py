"""Tests for email thread tracking: CRUD, ThreadTracker, and ReplyPoller."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import email_threads as crud
from genesis.mail.reply_poller import ReplyPoller, _extract_reply_headers
from genesis.mail.threads import ThreadTracker
from genesis.mail.types import RawEmail

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a test DB with email_threads and email_thread_messages tables."""
    db_path = tmp_path / "test_threads.db"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE email_threads (
                id                TEXT PRIMARY KEY,
                sent_message_id   TEXT NOT NULL,
                owner             TEXT NOT NULL DEFAULT 'outreach',
                owner_ref         TEXT,
                recipient         TEXT NOT NULL,
                subject           TEXT,
                context           TEXT,
                status            TEXT NOT NULL DEFAULT 'awaiting_reply' CHECK (
                    status IN ('awaiting_reply', 'replied', 'follow_up_sent', 'closed')
                ),
                follow_up_after   TEXT,
                follow_up_sent_at TEXT,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE email_thread_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id       TEXT NOT NULL REFERENCES email_threads(id),
                message_id      TEXT NOT NULL,
                direction       TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
                sender          TEXT,
                subject         TEXT,
                body_preview    TEXT,
                received_at     TEXT NOT NULL,
                UNIQUE(message_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX idx_email_threads_message_id ON email_threads(sent_message_id)"
        )
        await conn.commit()
        yield conn


@pytest_asyncio.fixture
async def tracker(db):
    return ThreadTracker(db)


# ---------------------------------------------------------------------------
# CRUD Tests
# ---------------------------------------------------------------------------


class TestCRUD:
    @pytest.mark.asyncio
    async def test_register_thread(self, db):
        tid = await crud.register_thread(
            db,
            sent_message_id="<abc@gmail.com>",
            recipient="person@example.com",
            owner="outreach",
            owner_ref="influencer:test",
            subject="Hello",
        )
        assert tid
        thread = await crud.get_thread(db, tid)
        assert thread is not None
        assert thread["sent_message_id"] == "<abc@gmail.com>"
        assert thread["recipient"] == "person@example.com"
        assert thread["status"] == "awaiting_reply"
        assert thread["follow_up_after"] is not None

    @pytest.mark.asyncio
    async def test_match_reply_by_in_reply_to(self, db):
        msg_id = "<sent123@gmail.com>"
        await crud.register_thread(
            db, sent_message_id=msg_id, recipient="r@test.com",
        )
        thread = await crud.match_reply(db, in_reply_to=msg_id)
        assert thread is not None
        assert thread["sent_message_id"] == msg_id

    @pytest.mark.asyncio
    async def test_match_reply_by_references(self, db):
        msg_id = "<sent456@gmail.com>"
        await crud.register_thread(
            db, sent_message_id=msg_id, recipient="r@test.com",
        )
        thread = await crud.match_reply(
            db, references=["<other@test.com>", msg_id],
        )
        assert thread is not None

    @pytest.mark.asyncio
    async def test_match_reply_no_match(self, db):
        await crud.register_thread(
            db, sent_message_id="<known@gmail.com>", recipient="r@test.com",
        )
        thread = await crud.match_reply(db, in_reply_to="<unknown@test.com>")
        assert thread is None

    @pytest.mark.asyncio
    async def test_match_reply_ignores_closed(self, db):
        tid = await crud.register_thread(
            db, sent_message_id="<closed@gmail.com>", recipient="r@test.com",
        )
        await crud.update_status(db, tid, "closed")
        thread = await crud.match_reply(db, in_reply_to="<closed@gmail.com>")
        assert thread is None

    @pytest.mark.asyncio
    async def test_record_reply_updates_status(self, db):
        tid = await crud.register_thread(
            db, sent_message_id="<s@gmail.com>", recipient="r@test.com",
        )
        await crud.record_reply(
            db,
            thread_id=tid,
            message_id="<reply@test.com>",
            sender="person@test.com",
            subject="Re: Hello",
            body_preview="Thanks!",
        )
        thread = await crud.get_thread(db, tid)
        assert thread["status"] == "replied"

        msgs = await crud.get_thread_messages(db, tid)
        assert len(msgs) == 2  # sent + received
        received = [m for m in msgs if m["direction"] == "received"]
        assert len(received) == 1
        assert received[0]["sender"] == "person@test.com"

    @pytest.mark.asyncio
    async def test_get_stale_threads(self, db):
        # Thread with follow_up_after in the past
        tid = await crud.register_thread(
            db,
            sent_message_id="<stale@gmail.com>",
            recipient="r@test.com",
            follow_up_days=0,  # follow_up_after = now
        )
        stale = await crud.get_stale_threads(db)
        assert len(stale) >= 1
        assert any(t["id"] == tid for t in stale)

    @pytest.mark.asyncio
    async def test_follow_up_sent_updates_timestamp(self, db):
        tid = await crud.register_thread(
            db, sent_message_id="<fu@gmail.com>", recipient="r@test.com",
        )
        await crud.update_status(db, tid, "follow_up_sent")
        thread = await crud.get_thread(db, tid)
        assert thread["status"] == "follow_up_sent"
        assert thread["follow_up_sent_at"] is not None


# ---------------------------------------------------------------------------
# ThreadTracker Tests
# ---------------------------------------------------------------------------


class TestThreadTracker:
    @pytest.mark.asyncio
    async def test_register_with_context(self, tracker):
        tid = await tracker.register(
            message_id="<ctx@gmail.com>",
            recipient="person@test.com",
            context={"target": "TheAIGRID", "pitch_type": "influencer"},
        )
        thread = await tracker.get_thread(tid)
        assert thread is not None
        assert isinstance(thread["context"], dict)
        assert thread["context"]["target"] == "TheAIGRID"

    @pytest.mark.asyncio
    async def test_match_and_record_reply(self, tracker):
        tid = await tracker.register(
            message_id="<match@gmail.com>",
            recipient="person@test.com",
        )
        thread = await tracker.match_reply(in_reply_to="<match@gmail.com>")
        assert thread is not None
        assert thread["id"] == tid

        await tracker.record_reply(
            thread_id=tid,
            message_id="<reply@test.com>",
            sender="person@test.com",
        )
        updated = await tracker.get_thread(tid)
        assert updated["status"] == "replied"

    @pytest.mark.asyncio
    async def test_close_thread(self, tracker):
        tid = await tracker.register(
            message_id="<close@gmail.com>",
            recipient="person@test.com",
        )
        await tracker.close(tid)
        thread = await tracker.get_thread(tid)
        assert thread["status"] == "closed"

    @pytest.mark.asyncio
    async def test_list_active_excludes_closed(self, tracker):
        tid1 = await tracker.register(
            message_id="<active@gmail.com>", recipient="a@test.com",
        )
        tid2 = await tracker.register(
            message_id="<closing@gmail.com>", recipient="b@test.com",
        )
        await tracker.close(tid2)

        active = await tracker.list_active()
        ids = [t["id"] for t in active]
        assert tid1 in ids
        assert tid2 not in ids


# ---------------------------------------------------------------------------
# Reply Header Extraction Tests
# ---------------------------------------------------------------------------


class TestReplyHeaderExtraction:
    def _make_raw_email(
        self, *, in_reply_to: str = "", references: str = "",
        from_addr: str = "test@example.com", subject: str = "Re: Test",
    ) -> RawEmail:
        """Build a minimal RFC 2822 email."""
        headers = [
            "From: " + from_addr,
            "Subject: " + subject,
            "Message-ID: <reply-123@example.com>",
            "Date: Sun, 07 Jun 2026 12:00:00 +0000",
        ]
        if in_reply_to:
            headers.append("In-Reply-To: " + in_reply_to)
        if references:
            headers.append("References: " + references)

        body = "Thanks for reaching out!"
        raw = "\r\n".join(headers) + "\r\n\r\n" + body
        return RawEmail(uid=1, raw_bytes=raw.encode())

    def test_extracts_in_reply_to(self):
        raw = self._make_raw_email(in_reply_to="<original@gmail.com>")
        reply = _extract_reply_headers(raw)
        assert reply is not None
        assert reply.in_reply_to == "<original@gmail.com>"

    def test_extracts_references(self):
        raw = self._make_raw_email(
            references="<ref1@gmail.com> <ref2@gmail.com>",
        )
        reply = _extract_reply_headers(raw)
        assert reply is not None
        assert len(reply.references) == 2

    def test_skips_non_reply(self):
        raw = self._make_raw_email()  # no In-Reply-To or References
        reply = _extract_reply_headers(raw)
        assert reply is None

    def test_extracts_body_preview(self):
        raw = self._make_raw_email(in_reply_to="<x@gmail.com>")
        reply = _extract_reply_headers(raw)
        assert reply is not None
        assert "Thanks" in reply.body_preview


# ---------------------------------------------------------------------------
# ReplyPoller Integration Tests
# ---------------------------------------------------------------------------


class TestReplyPoller:
    @pytest.mark.asyncio
    async def test_poll_matches_reply(self, tracker):
        # Register a thread
        await tracker.register(
            message_id="<sent-poll@gmail.com>",
            recipient="person@test.com",
            context={"test": True},
        )

        # Build a reply email that references our sent message
        reply_email = self._build_reply_raw("<sent-poll@gmail.com>")

        mock_imap = AsyncMock()
        mock_imap.fetch_unread.return_value = [reply_email]
        mock_imap.mark_read.return_value = None

        on_reply = AsyncMock()

        poller = ReplyPoller(
            imap_client=mock_imap,
            thread_tracker=tracker,
            on_reply=on_reply,
        )

        stats = await poller.poll()
        assert stats["fetched"] == 1
        assert stats["matched"] == 1
        assert stats["unmatched"] == 0

        # Verify only matched UIDs were marked read
        mock_imap.mark_read.assert_called_once_with([99])

        # Verify on_reply callback was called
        on_reply.assert_called_once()
        call_args = on_reply.call_args
        thread_arg = call_args[0][0]
        assert thread_arg["sent_message_id"] == "<sent-poll@gmail.com>"

    @pytest.mark.asyncio
    async def test_poll_unmatched_email(self, tracker):
        reply_email = self._build_reply_raw("<unknown@gmail.com>")

        mock_imap = AsyncMock()
        mock_imap.fetch_unread.return_value = [reply_email]
        mock_imap.mark_read.return_value = None

        poller = ReplyPoller(
            imap_client=mock_imap, thread_tracker=tracker,
        )

        stats = await poller.poll()
        assert stats["unmatched"] == 1
        assert stats["matched"] == 0

        # Unmatched emails should NOT be marked read (left for weekly monitor)
        mock_imap.mark_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_checks_follow_ups(self, tracker):
        # Register a thread with immediate follow-up
        await tracker.register(
            message_id="<followup@gmail.com>",
            recipient="person@test.com",
            follow_up_days=0,
        )

        mock_imap = AsyncMock()
        mock_imap.fetch_unread.return_value = []

        on_stale = AsyncMock()
        poller = ReplyPoller(
            imap_client=mock_imap,
            thread_tracker=tracker,
            on_stale_thread=on_stale,
        )

        stats = await poller.poll()
        assert stats["follow_ups"] >= 1
        on_stale.assert_called()

    @staticmethod
    def _build_reply_raw(in_reply_to: str) -> RawEmail:
        headers = [
            "From: person@example.com",
            "Subject: Re: Test",
            f"In-Reply-To: {in_reply_to}",
            "Message-ID: <reply-test@example.com>",
            "Date: Sun, 07 Jun 2026 12:00:00 +0000",
        ]
        body = "This looks interesting!"
        raw = "\r\n".join(headers) + "\r\n\r\n" + body
        return RawEmail(uid=99, raw_bytes=raw.encode())
