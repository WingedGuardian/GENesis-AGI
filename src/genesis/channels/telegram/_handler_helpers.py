"""Helper functions for V2 Telegram handlers."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from telegram.error import BadRequest

if TYPE_CHECKING:
    from telegram import Message

    from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker

log = logging.getLogger(__name__)

_TG_MAX_LEN = 4096


def _format_error(error: Exception) -> str:
    """Format a CC exception into a user-facing Telegram message."""
    from genesis.cc.exceptions import (
        CCError,
        CCMCPError,
        CCProcessError,
        CCQuotaExhaustedError,
        CCRateLimitError,
        CCSessionError,
        CCTimeoutError,
    )

    if isinstance(error, CCTimeoutError):
        return "Genesis is taking too long — try a simpler request or try again later."
    if isinstance(error, CCQuotaExhaustedError):
        return "CC usage limit reached — operating in contingency mode."
    if isinstance(error, CCRateLimitError):
        return f"Rate limit reached — please wait a moment. ({error})"
    if isinstance(error, CCMCPError):
        server = f" ({error.server_name})" if error.server_name else ""
        return f"Tool server error{server} — try again."
    if isinstance(error, CCSessionError):
        return "Session expired — auto-recovering, please resend your message."
    if isinstance(error, CCProcessError):
        return "Something went wrong with Genesis — try again."
    if isinstance(error, CCError):
        return f"Genesis error: {error}"
    if isinstance(error, BadRequest):
        return f"Telegram error: {error.message}"
    return "Sorry, something went wrong."


def _split_for_telegram(text: str, limit: int = _TG_MAX_LEN) -> list[str]:
    """Split text into chunks that fit Telegram's message limit.

    Reuses the code-block-aware splitter from ResponseFormatter.
    """
    if len(text) <= limit:
        return [text]
    from genesis.cc.formatter import ResponseFormatter
    return ResponseFormatter()._split_preserving_code(text, limit)


async def _send_formatted(chat, text: str, **kwargs):
    """Send text with markdown→HTML, falling back to plain on BadRequest.

    Splits long messages into chunks that fit Telegram's 4096-char limit.
    """
    from genesis.channels.telegram.markup import md_to_telegram_html
    chunks = _split_for_telegram(text)
    last_msg = None
    for chunk in chunks:
        try:
            last_msg = await chat.send_message(
                md_to_telegram_html(chunk), parse_mode="HTML", **kwargs,
            )
        except BadRequest as exc:
            log.warning("HTML send failed (%s), falling back to plain text", exc.message)
            last_msg = await chat.send_message(chunk, **kwargs)
    return last_msg


async def _reply_formatted(message, text: str) -> Message | None:
    """Reply with markdown→HTML, falling back to plain on BadRequest.

    Splits long messages into chunks that fit Telegram's 4096-char limit.
    Returns the last sent Message so callers can capture the outbound message_id.
    """
    from genesis.channels.telegram.markup import md_to_telegram_html
    chunks = _split_for_telegram(text)
    last_msg = None
    for chunk in chunks:
        try:
            last_msg = await message.reply_text(
                md_to_telegram_html(chunk), parse_mode="HTML",
            )
        except BadRequest as exc:
            log.warning("HTML reply failed for chunk (%s), falling back to plain text", exc.message)
            last_msg = await message.reply_text(chunk)
    return last_msg


class _TypingKeepAliveV2:
    """Typing indicator with circuit breaker — no infinite 401 loops."""

    def __init__(self, chat, breaker: TypingCircuitBreaker, *, interval_s: float = 4.0):
        self._chat = chat
        self._breaker = breaker
        self._interval = interval_s
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if not self._breaker.should_send(self._chat.id):
                continue
            try:
                await self._chat.send_action("typing")
                self._breaker.record_success(self._chat.id)
            except Exception:
                self._breaker.record_failure(self._chat.id)

    def start(self) -> None:
        from genesis.util.tasks import tracked_task
        self._task = tracked_task(self._loop(), name="typing-keepalive-v2")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
