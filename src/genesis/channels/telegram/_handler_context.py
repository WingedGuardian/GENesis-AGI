"""HandlerContext — shared state for V2 Telegram handlers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite
    from telegram import Update

    from genesis.cc.conversation import ConversationLoop
    from genesis.cc.types import StreamEvent
    from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2
    from genesis.channels.telegram.transport.streaming import DraftStreamer
    from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker
    from genesis.channels.telegram.transport.update_dedupe import TelegramUpdateDedupe
    from genesis.channels.voice import VoiceDeliveryHelper

log = logging.getLogger(__name__)


@dataclass
class HandlerContext:
    """Shared state replacing closure variables in make_handlers_v2."""

    loop: ConversationLoop
    allowed_users: set[int]
    whisper_model: str
    voice_helper: VoiceDeliveryHelper | None = None
    adapter: TelegramAdapterV2 | None = None
    reply_waiter: object | None = None
    engagement_tracker: object | None = None
    db: aiosqlite.Connection | None = None
    typing_breaker: TypingCircuitBreaker | None = None
    dedupe: TelegramUpdateDedupe | None = None
    # Autonomous CLI approval gate — injected by the channels bridge so
    # ``handle_callback_query`` can resolve ``cli_approve:{id}`` buttons
    # and the text-message handler can resolve bare "approve"/"reject"
    # typed into the Approvals topic.  Optional: if unset, approvals fall
    # back to dashboard-only resolution.
    autonomous_cli_gate: object | None = None
    # Thread ID of the "Approvals" supergroup topic — needed so the text
    # handler only resolves bare approve/reject messages *inside* that
    # topic, not general conversation.  Set alongside autonomous_cli_gate.
    approvals_thread_id: int | None = None

    draft_streaming_enabled: bool = True
    typing_breaker_instance: TypingCircuitBreaker = field(init=False)
    dedupe_instance: TelegramUpdateDedupe = field(init=False)
    chat_reply_mode: dict[int, str] = field(default_factory=dict)
    active_interrupts: dict[tuple[int, int], asyncio.Event] = field(default_factory=dict)
    pending_settings: dict[int, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self):
        from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker
        from genesis.channels.telegram.transport.update_dedupe import TelegramUpdateDedupe

        self.typing_breaker_instance = self.typing_breaker or TypingCircuitBreaker()
        self.dedupe_instance = self.dedupe or TelegramUpdateDedupe()
        if not self.draft_streaming_enabled:
            log.info("Draft streaming DISABLED via GENESIS_TELEGRAM_DRAFT_STREAMING")

    def want_voice(self, chat_id: int, input_was_voice: bool) -> bool:
        if not self.voice_helper:
            return False
        mode = self.chat_reply_mode.get(chat_id, "match")
        if mode == "voice":
            return True
        if mode == "text":
            return False
        return input_was_voice

    def authorized(self, user_id: int | None) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def thread_id(self, update: Update) -> str | None:
        msg = update.message or update.effective_message
        if msg and msg.message_thread_id is not None:
            return str(msg.message_thread_id)
        return None

    def thread_id_from_msg(self, msg) -> str | None:
        if msg and msg.message_thread_id is not None:
            return str(msg.message_thread_id)
        return None

    def make_on_event(self, interrupt_event: asyncio.Event, streamer: DraftStreamer | None):
        """Factory for CC event routing callbacks."""
        async def _on_event(event: StreamEvent) -> None:
            if interrupt_event.is_set():
                return
            if streamer and streamer.enabled:
                if event.event_type in ("rate_limit_event", "error"):
                    streamer.disable()
                elif event.event_type == "thinking":
                    await streamer.on_thinking()
                elif event.event_type == "text" and event.text:
                    await streamer.on_text(event.text)
                elif event.event_type == "tool_use":
                    tool_name = event.tool_name or "tool"
                    await streamer.on_tool_use(tool_name)
        return _on_event
