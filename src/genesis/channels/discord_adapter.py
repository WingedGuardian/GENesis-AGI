"""Discord webhook channel adapter — posts to Discord via webhook URLs.

Stateless per-send (like EmailAdapter). Uses HTTP POST to Discord webhook
endpoints — no gateway connection needed, coexists with the CC Discord plugin.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from genesis.channels.base import ChannelAdapter, ChannelNotConfiguredError

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
        default_webhook: Webhook URL for the DEFAULT channel. Not a catch-all —
            see ``_resolve_webhook``: a named channel with no webhook is refused
            rather than redirected here.
        default_channel: Name of the configured default recipient (e.g.
            ``"dev-discussion"``). Sending to this name — or to no name — uses
            ``default_webhook``; any other unconfigured name raises.
        config_hint: Callable mapping a channel name → the name of the setting
            that would configure it, used only in that refusal message. Supplied
            by the wiring module, NOT known here, and deliberately a callable so
            the WHOLE naming rule (prefix + case + separator mangling) stays in
            one place instead of splitting across two files.
            Two reasons it lives at the origin: ``scripts/check_external_io.py``
            treats a webhook-env literal as an egress door and requires the file
            to be ALLOWLISTED, and its own docstring states this adapter is
            covered via its URL's ORIGIN in ``runtime/init/outreach.py`` — so
            keeping the rule there satisfies that design rather than widening the
            allowlist. None → the refusal lists the configured names instead.
    """

    def __init__(
        self,
        webhooks: dict[str, str],
        default_webhook: str,
        *,
        default_channel: str = "",
        config_hint: Callable[[str], str] | None = None,
    ) -> None:
        self._webhooks = webhooks
        self._default = default_webhook
        self._default_channel = default_channel
        self._config_hint = config_hint

    def _resolve_webhook(self, channel_id: str) -> str:
        """Webhook URL for ``channel_id`` — raises rather than redirecting.

        A NAMED channel with no webhook used to fall back to ``default_webhook``
        and report success, so a caller that asked for ``announcements`` could
        not tell it had posted to the default channel instead: nothing errored,
        and the log line below records the REQUESTED name. That is the trap PR
        #1854 exists to remove, and it applied to a third of the names that PR
        lets a caller request — ``bug-reports``, ``feature-requests`` and
        ``troubleshooting`` had no webhook on the install where it was measured,
        so all three would have reported success while posting elsewhere.

        The default recipient keeps the fallback deliberately: "the default
        channel" IS whatever the default webhook points at, so it need not also
        appear in the per-channel map. An empty ``channel_id`` is that same case.
        """
        url = self._webhooks.get(channel_id)
        if url:
            return url
        if not channel_id or channel_id == self._default_channel:
            return self._default
        if self._config_hint is not None:
            remedy = f"Configure {self._config_hint(channel_id)}"
        else:
            known = ", ".join(sorted(self._webhooks)) or "(none)"
            remedy = f"Configured channels: {known}"
        raise ChannelNotConfiguredError(
            f"No Discord webhook configured for channel {channel_id!r} — refusing "
            f"to post to the default channel instead. {remedy}, or send to "
            f"{self._default_channel or 'the default channel'}."
        )

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
            channel_id: Webhook name (e.g., ``"dev-discussion"``). Resolved by
                ``_resolve_webhook``, which RAISES for a named channel with no
                webhook rather than silently posting to the default one.
            text: Message body. Auto-chunked at newline boundaries if
                it exceeds Discord's 2000-char limit.
            message_thread_id: Ignored for webhooks.
        """
        webhook_url = self._resolve_webhook(channel_id)
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

    async def send_poll(
        self,
        channel_id: str,
        question: str,
        answers: list[str],
        *,
        duration_hours: int = 168,
        allow_multiselect: bool = False,
    ) -> str:
        """Create a Discord poll via webhook. Returns the message ID.

        Args:
            channel_id: Webhook name. Resolved by ``_resolve_webhook`` — same
                refusal as ``send_message``; a poll posted to the wrong channel
                is as undetectable as a message, and collects the wrong votes.
            question: Poll question (max 300 chars).
            answers: List of answer strings (max 10, each max 55 chars).
            duration_hours: Poll duration in hours (max 768, default 7 days).
            allow_multiselect: Whether users can select multiple answers.
        """
        webhook_url = self._resolve_webhook(channel_id)
        url = f"{webhook_url}?wait=true"

        payload: dict[str, Any] = {
            "poll": {
                "question": {"text": question[:300]},
                "answers": [
                    {"poll_media": {"text": a[:55]}} for a in answers[:10]
                ],
                "duration": min(duration_hours, 768),
                "allow_multiselect": allow_multiselect,
            }
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            msg_id = data.get("id", "")

        logger.info(
            "Discord poll created in %s (msg_id=%s, question=%.50s)",
            channel_id, msg_id, question,
        )
        return msg_id

    async def send_typing(self, channel_id: str) -> None:
        """No-op — webhooks don't support typing indicators."""

    def get_capabilities(self) -> dict:
        return {
            "markdown": True,
            "buttons": False,
            "reactions": False,
            "voice": False,
            "documents": False,
            "polls": True,
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
