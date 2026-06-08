"""ThreadTracker — service layer for email thread state management.

Wraps CRUD operations with business logic: thread registration,
reply matching, follow-up scheduling, and state transitions.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING

from genesis.db.crud import email_threads as crud

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class ThreadTracker:
    """Manages email thread lifecycle: register → awaiting_reply → replied/follow_up_sent → closed."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def register(
        self,
        *,
        message_id: str,
        recipient: str,
        owner: str = "outreach",
        owner_ref: str | None = None,
        subject: str | None = None,
        context: dict | None = None,
        follow_up_days: int = 4,
    ) -> str:
        """Register a sent email for reply tracking.

        Args:
            message_id: RFC 2822 Message-ID of the sent email.
            recipient: Recipient email address.
            owner: Subsystem that owns this thread (e.g., "outreach", "general").
            owner_ref: Subsystem-specific reference (e.g., "influencer:theaigrid").
            subject: Email subject line.
            context: Dict of context for reply drafting (serialized to JSON).
            follow_up_days: Days before auto-follow-up (default 4).

        Returns:
            Thread ID.
        """
        context_json = json.dumps(context) if context else None
        thread_id = await crud.register_thread(
            self._db,
            sent_message_id=message_id,
            recipient=recipient,
            owner=owner,
            owner_ref=owner_ref,
            subject=subject,
            context=context_json,
            follow_up_days=follow_up_days,
        )
        logger.info(
            "Registered email thread %s for %s (owner=%s)",
            thread_id, recipient, owner,
        )
        return thread_id

    async def match_reply(
        self,
        *,
        in_reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> dict | None:
        """Match an incoming email to a registered thread.

        Args:
            in_reply_to: In-Reply-To header value.
            references: List of Message-IDs from References header.

        Returns:
            Thread dict if matched, None otherwise.
        """
        thread = await crud.match_reply(
            self._db,
            in_reply_to=in_reply_to,
            references=references,
        )
        if thread and thread.get("context"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                thread["context"] = json.loads(thread["context"])
        return thread

    async def record_reply(
        self,
        *,
        thread_id: str,
        message_id: str,
        sender: str,
        subject: str | None = None,
        body_preview: str | None = None,
    ) -> None:
        """Record a received reply and update thread to 'replied'."""
        await crud.record_reply(
            self._db,
            thread_id=thread_id,
            message_id=message_id,
            sender=sender,
            subject=subject,
            body_preview=body_preview,
        )
        logger.info("Recorded reply on thread %s from %s", thread_id, sender)

    async def mark_follow_up_sent(self, thread_id: str) -> None:
        """Mark that a follow-up was sent for this thread."""
        await crud.update_status(self._db, thread_id, "follow_up_sent")
        logger.info("Marked follow-up sent on thread %s", thread_id)

    async def close(self, thread_id: str) -> None:
        """Close a thread (no further action needed)."""
        await crud.update_status(self._db, thread_id, "closed")
        logger.info("Closed thread %s", thread_id)

    async def get_stale_threads(self) -> list[dict]:
        """Get threads awaiting reply past their follow-up deadline."""
        threads = await crud.get_stale_threads(self._db)
        # Deserialize context JSON
        for t in threads:
            if t.get("context"):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    t["context"] = json.loads(t["context"])
        return threads

    async def get_thread_history(self, thread_id: str) -> list[dict]:
        """Get all messages in a thread for reply context."""
        return await crud.get_thread_messages(self._db, thread_id)

    async def get_thread(self, thread_id: str) -> dict | None:
        """Get a single thread by ID."""
        thread = await crud.get_thread(self._db, thread_id)
        if thread and thread.get("context"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                thread["context"] = json.loads(thread["context"])
        return thread

    async def list_active(self) -> list[dict]:
        """List all non-closed threads."""
        return await crud.list_active_threads(self._db)
