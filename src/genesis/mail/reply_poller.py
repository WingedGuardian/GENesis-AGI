"""ReplyPoller — lightweight IMAP checker for email thread replies.

Runs every 4 hours. Does NOT use LLM calls — pure header matching
against registered threads. Marks emails as read immediately after
fetch to avoid collision with the weekly deep triage monitor.

Non-matching emails are left for the weekly MailMonitor.
"""

from __future__ import annotations

import email
import email.header
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING

from genesis.mail.parser import parse_email
from genesis.mail.types import RawEmail

if TYPE_CHECKING:
    from genesis.mail.imap_client import IMAPClient
    from genesis.mail.threads import ThreadTracker

logger = logging.getLogger(__name__)

# Type for reply handler callbacks
ReplyCallback = Callable[
    [dict, "ParsedReply"],
    Coroutine,
]


class ParsedReply:
    """Minimal parsed reply — only what's needed for thread matching."""

    __slots__ = (
        "message_id", "in_reply_to", "references", "sender",
        "subject", "body_preview", "imap_uid",
    )

    def __init__(
        self,
        *,
        message_id: str,
        in_reply_to: str | None,
        references: list[str],
        sender: str,
        subject: str,
        body_preview: str,
        imap_uid: int,
    ) -> None:
        self.message_id = message_id
        self.in_reply_to = in_reply_to
        self.references = references
        self.sender = sender
        self.subject = subject
        self.body_preview = body_preview
        self.imap_uid = imap_uid


def _extract_reply_headers(raw: RawEmail) -> ParsedReply | None:
    """Extract threading headers from raw email bytes."""
    try:
        msg = email.message_from_bytes(raw.raw_bytes)
    except Exception:
        logger.warning("Failed to parse email UID %d", raw.uid, exc_info=True)
        return None

    parsed = parse_email(raw.raw_bytes, uid=raw.uid)

    in_reply_to = _clean_header(msg.get("In-Reply-To", ""))
    references_raw = _clean_header(msg.get("References", ""))
    references = references_raw.split() if references_raw else []

    if not in_reply_to and not references:
        return None  # Not a reply — skip

    return ParsedReply(
        message_id=parsed.message_id,
        in_reply_to=in_reply_to or None,
        references=references,
        sender=parsed.sender,
        subject=parsed.subject,
        body_preview=parsed.body[:500] if parsed.body else "",
        imap_uid=raw.uid,
    )


def _clean_header(value: str) -> str:
    """Decode and clean an email header value."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded).strip()


class ReplyPoller:
    """Polls IMAP for replies to registered email threads.

    Designed to run alongside the MailMonitor on the same scheduler.
    Key difference: marks emails as read IMMEDIATELY after fetch to
    minimize the IMAP collision window with the weekly monitor.
    """

    def __init__(
        self,
        *,
        imap_client: IMAPClient,
        thread_tracker: ThreadTracker,
        on_reply: ReplyCallback | None = None,
        on_stale_thread: ReplyCallback | None = None,
        max_fetch: int = 20,
    ) -> None:
        self._imap = imap_client
        self._tracker = thread_tracker
        self._on_reply = on_reply
        self._on_stale_thread = on_stale_thread
        self._max_fetch = max_fetch

    async def poll(self) -> dict:
        """Run one poll cycle.

        Returns:
            Summary dict with counts: fetched, matched, unmatched, errors.
        """
        stats = {"fetched": 0, "matched": 0, "unmatched": 0, "follow_ups": 0, "errors": 0}

        # 1. Fetch unread emails
        raw_emails = await self._imap.fetch_unread(max_count=self._max_fetch)
        stats["fetched"] = len(raw_emails)

        if not raw_emails:
            logger.debug("Reply poller: no unread emails")
            # Still check for stale threads even with no new emails
            await self._check_follow_ups(stats)
            return stats

        # 2. Extract reply headers and match against threads.
        #    Only mark MATCHED emails as read — unmatched emails stay
        #    unread so the weekly MailMonitor can process them.
        matched_uids: list[int] = []

        for raw in raw_emails:
            try:
                reply = _extract_reply_headers(raw)
                if reply is None:
                    stats["unmatched"] += 1
                    continue

                thread = await self._tracker.match_reply(
                    in_reply_to=reply.in_reply_to,
                    references=reply.references,
                )
                if thread is None:
                    stats["unmatched"] += 1
                    continue

                # Match found — mark read and record
                matched_uids.append(raw.uid)
                stats["matched"] += 1
                await self._tracker.record_reply(
                    thread_id=thread["id"],
                    message_id=reply.message_id,
                    sender=reply.sender,
                    subject=reply.subject,
                    body_preview=reply.body_preview,
                )
                logger.info(
                    "Reply matched: thread=%s from=%s subject=%r",
                    thread["id"], reply.sender, reply.subject,
                )

                # Dispatch to reply handler
                if self._on_reply:
                    try:
                        await self._on_reply(thread, reply)
                    except Exception:
                        logger.error(
                            "Reply handler failed for thread %s",
                            thread["id"], exc_info=True,
                        )
                        stats["errors"] += 1

            except Exception:
                logger.error(
                    "Error processing email UID %d", raw.uid, exc_info=True,
                )
                stats["errors"] += 1

        # 3. Mark only matched emails as read
        if matched_uids:
            await self._imap.mark_read(matched_uids)

        # 4. Check for stale threads (follow-up scheduling)
        await self._check_follow_ups(stats)

        logger.info(
            "Reply poller: fetched=%d matched=%d unmatched=%d follow_ups=%d errors=%d",
            stats["fetched"], stats["matched"], stats["unmatched"],
            stats["follow_ups"], stats["errors"],
        )
        return stats

    async def _check_follow_ups(self, stats: dict) -> None:
        """Check for threads past their follow-up deadline."""
        stale = await self._tracker.get_stale_threads()
        for thread in stale:
            stats["follow_ups"] += 1
            if self._on_stale_thread:
                try:
                    await self._on_stale_thread(thread, None)  # type: ignore[arg-type]
                except Exception:
                    logger.error(
                        "Follow-up handler failed for thread %s",
                        thread["id"], exc_info=True,
                    )
                    stats["errors"] += 1
