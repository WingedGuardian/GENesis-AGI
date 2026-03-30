"""IMAP client for Gmail access — thin wrapper around imaplib."""

from __future__ import annotations

import asyncio
import imaplib
import logging

from genesis.mail.types import RawEmail

logger = logging.getLogger(__name__)

_GMAIL_HOST = "imap.gmail.com"


class IMAPClient:
    """Fetches unread emails from Gmail via IMAP4_SSL.

    Connect-per-call — no connection pooling. Gmail drops idle connections
    after ~29 minutes, and weekly batch runs make pooling pointless.
    """

    def __init__(
        self,
        *,
        address: str,
        app_password: str,
        timeout: int = 30,
    ) -> None:
        self._address = address
        self._password = app_password
        self._timeout = timeout

    async def fetch_unread(self, max_count: int = 50) -> list[RawEmail]:
        """Fetch up to max_count unread emails. Returns empty list on failure."""
        try:
            return await asyncio.to_thread(self._fetch_unread_sync, max_count)
        except Exception:
            logger.error(
                "IMAP fetch failed — check GENESIS_GMAIL_ADDRESS and "
                "GENESIS_GMAIL_APP_PASSWORD in secrets.env",
                exc_info=True,
            )
            return []

    async def mark_read(self, uids: list[int]) -> None:
        """Mark messages as read by adding the \\Seen flag."""
        if not uids:
            return
        try:
            await asyncio.to_thread(self._mark_read_sync, uids)
        except Exception:
            logger.warning("Failed to mark %d emails as read", len(uids), exc_info=True)

    def _fetch_unread_sync(self, max_count: int) -> list[RawEmail]:
        """Synchronous IMAP fetch — runs in thread pool."""
        conn = self._connect()
        try:
            conn.select("INBOX")
            _, data = conn.search(None, "UNSEEN")
            uid_list = data[0].split() if data[0] else []

            if not uid_list:
                return []

            # Cap to max_count (take most recent)
            uid_list = uid_list[-max_count:]

            results: list[RawEmail] = []
            for uid_bytes in uid_list:
                try:
                    _, msg_data = conn.fetch(uid_bytes, "(RFC822)")
                    if msg_data and msg_data[0] is not None:
                        raw_bytes = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                        if isinstance(raw_bytes, bytes):
                            results.append(RawEmail(uid=int(uid_bytes), raw_bytes=raw_bytes))
                except Exception:
                    logger.warning("Failed to fetch UID %s", uid_bytes, exc_info=True)

            return results
        finally:
            self._disconnect(conn)

    def _mark_read_sync(self, uids: list[int]) -> None:
        """Synchronous mark-read — runs in thread pool."""
        conn = self._connect()
        try:
            conn.select("INBOX")
            for uid in uids:
                try:
                    conn.store(str(uid).encode(), "+FLAGS", "\\Seen")
                except Exception:
                    logger.warning("Failed to mark UID %d as read", uid, exc_info=True)
        finally:
            self._disconnect(conn)

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Create and authenticate a new IMAP connection."""
        conn = imaplib.IMAP4_SSL(_GMAIL_HOST, timeout=self._timeout)
        conn.login(self._address, self._password)
        return conn

    @staticmethod
    def _disconnect(conn: imaplib.IMAP4_SSL) -> None:
        """Gracefully close and logout."""
        import contextlib

        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(Exception):
            conn.logout()
