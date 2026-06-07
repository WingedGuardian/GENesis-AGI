"""Discord webhook channel adapter — posts to Discord via webhook URLs.

Stateless per-send (like EmailAdapter). Uses HTTP POST to Discord webhook
endpoints — no gateway connection needed, coexists with the CC Discord plugin.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from genesis.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)

# Discord enforces 2000-char max per message.
_MAX_MESSAGE_LENGTH = 2000


class DiscordWebhookAdapter(ChannelAdapter):
    """Discord webhook adapter for the outreach pipeline.

    Posts messages via webhook URLs (HTTP POST). Each send opens a fresh
    httpx connection — stateless, no persistent connection to manage.
    Coexists with the CC Discord plugin (which uses the gateway).

    Args:
        webhooks: Mapping of channel name → webhook URL.
        default_webhook: Fallback webhook URL for unknown channel names.
    """

    def __init__(
        self,
        webhooks: dict[str, str],
        default_webhook: str,
    ) -> None:
        self._webhooks = webhooks
        self._default = default_webhook

    async def start(self) -> None:
        """No-op — webhooks are stateless per-send."""

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
        """Post a message via Discord webhook. Returns the message ID.

        Args:
            channel_id: Webhook name (e.g., ``"dev-discussion"``). Looked
                up in the webhooks dict; falls back to default_webhook.
            text: Message body. Auto-chunked at newline boundaries if
                it exceeds Discord's 2000-char limit.
            message_thread_id: Ignored for webhooks.
        """
        webhook_url = self._webhooks.get(channel_id, self._default)
        # ?wait=true makes Discord return the created message object
        # (including its ID) instead of 204 No Content.
        url = f"{webhook_url}?wait=true"

        chunks = _chunk_text(text, _MAX_MESSAGE_LENGTH)
        last_msg_id = ""

        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                payload: dict[str, Any] = {"content": chunk}
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                last_msg_id = data.get("id", "")

        logger.info(
            "Discord webhook sent %d chunk(s) to %s (msg_id=%s)",
            len(chunks), channel_id, last_msg_id,
        )
        return last_msg_id

    async def send_typing(self, channel_id: str) -> None:
        """No-op — webhooks don't support typing indicators."""

    def get_capabilities(self) -> dict:
        return {
            "markdown": True,
            "buttons": False,
            "reactions": False,
            "voice": False,
            "documents": False,
            "max_length": _MAX_MESSAGE_LENGTH,
        }

    async def get_engagement_signals(self, delivery_id: str) -> dict:
        """Webhooks have no engagement tracking — always returns neutral."""
        return {"signal": "neutral", "details": {}}


def _chunk_text(text: str, max_length: int) -> list[str]:
    """Split text into chunks at newline boundaries.

    Prefers splitting at ``\\n`` boundaries. Falls back to hard cut
    at ``max_length`` for lines that exceed the limit.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        # Would adding this line exceed the limit?
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
        else:
            # Flush current chunk
            if current:
                chunks.append(current)
            # If a single line exceeds max_length, hard-cut it
            if len(line) > max_length:
                while line:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
                current = ""
            else:
                current = line

    if current:
        chunks.append(current)

    return chunks
