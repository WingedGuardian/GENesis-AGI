"""Email channel adapter — sends outreach via Gmail SMTP."""

from __future__ import annotations

import email.message
import logging
import smtplib
from typing import Any

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


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
        """Send an email. Returns the Message-ID as delivery ID.

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
        msg.set_content(text)

        try:
            with smtplib.SMTP_SSL(self._host, self._port, timeout=30) as smtp:
                smtp.login(self._username, self._password)
                smtp.send_message(msg)
        except smtplib.SMTPException:
            logger.exception("Failed to send email to %s", recipient)
            raise

        message_id = msg["Message-ID"] or f"email-{id(msg)}"
        logger.info("Email sent to %s (Message-ID: %s)", recipient, message_id)
        return message_id

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
