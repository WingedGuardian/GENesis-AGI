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

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING

import telegram

from genesis.channels.telegram.transport.send import send_with_client_heal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
_MAX_TOOL_LINES = 10
# Drafts share messages.setTyping's per-chat limit (20 calls / 5 s, 40 / 30 s;
# Telegram "AI features for bots") with the 4 s typing keepalive: 1.5 s keeps
# drafts at 20 / 30 s plus typing's 7.5, under 40.
_DEFAULT_THROTTLE_S = 1.5


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
        stopping: Callable[[], bool] | None = None,
        prefix_effort: str = "",
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id
        self.message_thread_id = message_thread_id
        self.throttle_s = throttle_s
        self._prefix = prefix
        self._prefix_effort = prefix_effort
        # Heal a shutdown-closed send client on draft sends; wired to the
        # adapter's stopping flag by the caller (default: never stopping).
        self._stopping = stopping or (lambda: False)

        self._tool_lines: list[str] = []
        self._text = ""
        self._thinking = False
        self._now = time.monotonic  # a seam for tests that control the clock
        self._last_send_time = 0.0
        self._content_at = self._now()  # when the draft's content last changed
        self._send_lock = asyncio.Lock()  # one draft request in flight at a time
        self._enabled = True
        self._any_draft_sent = False
        self._last_draft_text = ""
        self._consecutive_failures = 0
        self._waiting_s: float | None = None
        self._failure_logged = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def quiet_s(self) -> float:
        """Seconds since the draft was last sent: how long it has gone without an
        update, which is what Telegram's ~30 s draft lifetime counts. Zero before
        the first draft, when there is nothing on screen to keep."""
        if not self._any_draft_sent:
            return 0.0
        return self._now() - self._last_send_time

    def message_sent(self) -> None:
        """A real message just reached the chat. Telegram removes the live draft
        when one arrives ("AI features for bots"), so the next update is sent at
        once, unthrottled and undeduplicated."""
        self._last_send_time = 0.0
        self._last_draft_text = ""

    def disable(self) -> None:
        """Permanently disable this streamer.

        Call after final message delivery or on error events to prevent
        ephemeral drafts from appearing alongside (or instead of) real messages.
        """
        self._enabled = False

    def set_running_model(self, model_label: str) -> None:
        """Name the model Claude Code reports it is running in the prefix (#4850).

        The prefix is built from the session row before the turn starts; the row
        can name a different model than the one that answers.
        """
        if self._prefix and model_label and self._prefix_effort:
            self._prefix = f"[{model_label} / {self._prefix_effort}]"

    def show_header(self, model_label: str, effort: str) -> None:
        """Set the prefix for a run that opens a CC session or hands to a peer.

        It REPLACES any prefix already set (the gap rule's, read from the session
        row before the turn), taking the effort the run was dispatched with, so a
        reply still carries one header. ``set_running_model`` then retitles it
        from the init event.
        """
        if model_label and effort:
            self._prefix = f"[{model_label} / {effort}]"
            self._prefix_effort = effort

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
        self._waiting_s = None
        if not self._thinking:
            self._thinking = True
            self._content_at = self._now()
            await self._maybe_send_draft()

    async def on_text(self, text: str) -> None:
        """Handle CC text event — append streamed text."""
        if not self._enabled or not text:
            return
        self._thinking = False
        self._waiting_s = None
        self._text += text
        self._content_at = self._now()
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
        self._waiting_s = None
        self._content_at = self._now()
        await self._maybe_send_draft()

    async def keepalive(self, *, after_s: float = 0.0) -> None:
        """Re-send the draft once it has gone ``after_s`` without a send, with how
        long its content has gone unchanged.

        Telegram drops a live draft about 30 s after its last update, so a long
        thinking stretch otherwise leaves nothing on screen. It also carries any
        update the throttle dropped. The send is forced: a refresh exists to
        reach Telegram, so the content dedup must not swallow it. The decision is
        made under the send lock, so a send that finished while this one waited
        cancels it.
        """
        async with self._send_lock:
            if not self._enabled or self.quiet_s() < after_s:
                return
            self._waiting_s = self._now() - self._content_at
            await self._send_draft_now(force=True)

    def status_body(self) -> str:
        """The tool activity so far, for a status message: empty when there is
        none. Text is left out, because the final reply carries it."""
        if not self._tool_lines:
            return ""
        visible = self._tool_lines[-_MAX_TOOL_LINES:]
        overflow = len(self._tool_lines) - len(visible)
        lines = [f"... +{overflow} more"] if overflow else []
        return "Working on it:\n" + "\n".join(lines + visible)

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

        if self._waiting_s is not None:
            parts.append(f"\n\u23f3 still working ({int(self._waiting_s)} s)")

        return "\n".join(parts)

    async def _maybe_send_draft(self) -> None:
        """Send draft if throttle interval has elapsed."""
        now = self._now()
        if (now - self._last_send_time) >= self.throttle_s:
            await self._send_draft()

    async def _send_draft(self, *, force: bool = False) -> None:
        """Send the composed draft via send_message_draft, one request at a time:
        a keepalive and a text update in flight together can land out of order
        and leave the older draft on screen."""
        async with self._send_lock:
            await self._send_draft_now(force=force)

    async def _send_draft_now(self, *, force: bool) -> None:
        if not self._enabled:
            return  # disabled (a halt, the final reply) while this send waited
        draft_text = self._compose_draft()
        if not draft_text.strip():
            return

        # Content dedup — skip if identical to last draft
        if draft_text == self._last_draft_text and not force:
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
            await send_with_client_heal(
                self.bot,
                lambda: self.bot.send_message_draft(**kwargs),
                stopping=self._stopping,
            )
            self._last_send_time = self._now()
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
            elif not self._failure_logged:
                # The first failure of a turn is logged with its cause: a flood
                # wait here was invisible while every failure logged at debug.
                self._failure_logged = True
                logger.info(
                    "Draft send failed (attempt %d/3) for chat %s",
                    self._consecutive_failures,
                    self.chat_id,
                    exc_info=True,
                )
            else:
                logger.debug(
                    "Draft send failed (attempt %d/3) for chat %s",
                    self._consecutive_failures, self.chat_id,
                )
