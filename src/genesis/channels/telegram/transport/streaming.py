"""Edit-based streaming for CC subprocess events via Telegram drafts.

Sends a single message and edits it in-place as CC streams text, tool use,
and thinking events. Uses PTB's send_message_draft() for smooth animation.

Adapted from RichardAtCT/claude-code-telegram DraftStreamer for Genesis's
CCInvoker subprocess model (stream-json protocol).

Key behaviors:
- Plain text drafts (no parse_mode) to avoid partial HTML/markdown errors
- Tail-truncation for messages >4096 chars
- Self-disabling on API error (streamer fails silently, request continues)
- Throttled updates (configurable, default 0.5s)
- Tool header + response body composition
"""
from __future__ import annotations

import logging
import secrets
import time

import telegram

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
_MAX_TOOL_LINES = 10
_DEFAULT_THROTTLE_S = 0.5


def generate_draft_id() -> int:
    """Generate a non-zero positive draft ID for Telegram draft animation."""
    return secrets.randbits(30) | 1


# Tool name → emoji mapping for compact display
_TOOL_ICONS: dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "Agent": "\U0001f9e0",
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "\U0001f527")


class DraftStreamer:
    """Accumulates CC stream events and sends periodic drafts to Telegram.

    The draft has two sections:
    1. Tool header — compact lines showing tool activity
    2. Response body — the actual assistant text, streamed token-by-token

    Designed for CC stream-json event types: text, thinking, tool_use, result.
    """

    def __init__(
        self,
        bot: telegram.Bot,
        chat_id: int,
        draft_id: int,
        message_thread_id: int | None = None,
        throttle_s: float = _DEFAULT_THROTTLE_S,
        prefix: str = "",
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id
        self.message_thread_id = message_thread_id
        self.throttle_s = throttle_s
        self._prefix = prefix

        self._tool_lines: list[str] = []
        self._text = ""
        self._thinking = False
        self._last_send_time = 0.0
        self._enabled = True
        self._any_draft_sent = False
        self._last_draft_text = ""
        self._consecutive_failures = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self) -> None:
        """Permanently disable this streamer.

        Call after final message delivery or on error events to prevent
        ephemeral drafts from appearing alongside (or instead of) real messages.
        """
        self._enabled = False

    @property
    def any_draft_sent(self) -> bool:
        """True if at least one draft was successfully sent to Telegram."""
        return self._any_draft_sent

    @property
    def accumulated_text(self) -> str:
        return self._text

    async def on_thinking(self) -> None:
        """Handle CC thinking event — show thinking indicator."""
        if not self._enabled:
            return
        if not self._thinking:
            self._thinking = True
            await self._maybe_send_draft()

    async def on_text(self, text: str) -> None:
        """Handle CC text event — append streamed text."""
        if not self._enabled or not text:
            return
        self._thinking = False
        self._text += text
        await self._maybe_send_draft()

    async def on_tool_use(self, tool_name: str, tool_input: str = "") -> None:
        """Handle CC tool_use event — add tool to header."""
        if not self._enabled:
            return
        icon = _tool_icon(tool_name)
        line = f"{icon} {tool_name}"
        if tool_input:
            # Show first arg (usually filename or command) truncated
            preview = tool_input[:60].replace("\n", " ")
            line += f"  {preview}"
        self._tool_lines.append(line)
        await self._maybe_send_draft()

    async def flush(self) -> None:
        """Force-send current accumulated content as a draft."""
        if not self._enabled:
            return
        await self._send_draft()

    def _compose_draft(self) -> str:
        """Combine tool header and response body into draft text."""
        parts: list[str] = []

        if self._prefix:
            parts.append(self._prefix)

        if self._tool_lines:
            visible = self._tool_lines[-_MAX_TOOL_LINES:]
            overflow = len(self._tool_lines) - _MAX_TOOL_LINES
            if overflow >= 3:
                parts.append(f"... +{overflow} more")
            parts.extend(visible)

        if self._thinking and not self._text:
            parts.append("\n\U0001f914 Thinking...")
        elif self._text:
            if parts:
                parts.append("")  # blank separator
            parts.append(self._text)

        return "\n".join(parts)

    async def _maybe_send_draft(self) -> None:
        """Send draft if throttle interval has elapsed."""
        now = time.monotonic()
        if (now - self._last_send_time) >= self.throttle_s:
            await self._send_draft()

    async def _send_draft(self) -> None:
        """Send the composed draft via send_message_draft."""
        draft_text = self._compose_draft()
        if not draft_text.strip():
            return

        # Content dedup — skip if identical to last draft
        if draft_text == self._last_draft_text:
            return

        # Tail-truncation for Telegram limit
        if len(draft_text) > TELEGRAM_MAX_LENGTH:
            draft_text = "\u2026" + draft_text[-(TELEGRAM_MAX_LENGTH - 1):]

        try:
            kwargs: dict = {
                "chat_id": self.chat_id,
                "text": draft_text,
                "draft_id": self.draft_id,
            }
            if self.message_thread_id is not None:
                kwargs["message_thread_id"] = self.message_thread_id
            await self.bot.send_message_draft(**kwargs)
            self._last_send_time = time.monotonic()
            self._last_draft_text = draft_text
            self._any_draft_sent = True
            self._consecutive_failures = 0
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    "Draft send failed %d times, disabling streamer for chat %s",
                    self._consecutive_failures, self.chat_id, exc_info=True,
                )
                self._enabled = False
            else:
                logger.debug(
                    "Draft send failed (attempt %d/3) for chat %s",
                    self._consecutive_failures, self.chat_id,
                )
