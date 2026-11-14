"""Email channel adapter — sends outreach via Gmail SMTP."""

from __future__ import annotations

import asyncio
import email.message
import email.utils
import logging
import smtplib
from typing import Any

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


class EmailDeliveryId(str):
    """The Message-ID a send returns, carrying the recipients the server refused.

    Still a plain ``str`` to every caller of ``ChannelAdapter.send_message``.
    ``smtplib`` returns normally when it accepts at least one recipient and
    reports the rest in a dict; ``refused`` keeps those addresses (normalized),
    so a caller that acts per recipient acts only on the ones the server took.
    """

    refused: frozenset[str]

    def __new__(cls, message_id: str, refused: Any = None) -> EmailDeliveryId:
        obj = super().__new__(cls, message_id)
        keys = refused.keys() if isinstance(refused, dict) else ()
        obj.refused = frozenset(str(k).strip().lower() for k in keys if str(k).strip())
        return obj


class EmailAdapter(ChannelAdapter):
    """SMTP-based email adapter for the outreach pipeline.

    Uses Gmail app passwords (same credential type as the IMAP mail monitor).
    Each send opens a fresh SMTP_SSL connection — stateless, no persistent
    connection to manage.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_address: str,
        *,
        from_name: str = "Genesis",
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._from_name = from_name

    @property
    def from_address(self) -> str:
        """The configured sender address — the pipeline's self-send guard
        compares delivery recipients against this to suppress emails the agent
        would otherwise address to itself."""
        return self._from_address

    async def start(self) -> None:
        """No-op — SMTP is stateless per-send."""

    async def stop(self) -> None:
        """No-op — no persistent connection."""

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Send an email. Returns the Message-ID as delivery ID, as an
        ``EmailDeliveryId`` whose ``refused`` names any recipient the server
        refused on a partly accepted send.

        Args:
            channel_id: Recipient email address.
            text: Message body (plain text).
            message_thread_id: Ignored for email.
            **kwargs: Optional 'subject' (str) for the email subject line.
        """
        subject = kwargs.get("subject", "Message from Genesis")
        recipient = channel_id

        msg = email.message.EmailMessage()
        msg["From"] = f"{self._from_name} <{self._from_address}>"
        msg["To"] = recipient
        msg["Subject"] = subject
        # Real RFC 5322 Message-ID so recipients can thread and our reply-poller can
        # match replies (WS-9a). EmailMessage() sets no Message-ID by default, so the
        # sent mail previously had none and the returned id was a fabricated placeholder.
        domain = self._from_address.rsplit("@", 1)[-1] if "@" in self._from_address else None
        msg["Message-ID"] = email.utils.make_msgid(domain=domain)
        msg.set_content(text)

        def _send_sync() -> Any:
            # Synchronous smtplib runs in a worker thread (asyncio.to_thread) so
            # the SMTP round-trip — connect, TLS, login, send — never blocks the
            # event loop. A blocking send here froze heartbeats/health/awareness
            # while the recovery worker retried failed sends (incident 2026-06-14).
            with smtplib.SMTP_SSL(self._host, self._port, timeout=30) as smtp:
                smtp.login(self._username, self._password)
                # The refused-recipients dict: empty when all were accepted
                # (all refused raises SMTPRecipientsRefused instead).
                return smtp.send_message(msg)

        try:
            refused = await asyncio.to_thread(_send_sync)
        except smtplib.SMTPException:
            logger.exception("Failed to send email to %s", recipient)
            raise

        delivery_id = EmailDeliveryId(msg["Message-ID"], refused)
        if delivery_id.refused:
            logger.warning(
                "Email to %s partly refused by the server: %s",
                recipient,
                sorted(delivery_id.refused),
            )
        logger.info("Email sent to %s (Message-ID: %s)", recipient, delivery_id)
        return delivery_id

    async def send_typing(self, channel_id: str) -> None:
        """No-op — email has no typing indicator."""

    def get_capabilities(self) -> dict:
        return {
            "markdown": False,
            "buttons": False,
            "reactions": False,
            "voice": False,
            "documents": False,
            "max_length": 50_000,
        }

    async def get_engagement_signals(self, delivery_id: str) -> dict:
        """Email has no built-in read receipts — always returns neutral."""
        return {"signal": "neutral", "details": {}}
