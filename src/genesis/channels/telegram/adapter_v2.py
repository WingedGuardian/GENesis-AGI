"""Telegram channel adapter V2 — resilient adapter with transport layer.

Improvements over V1:
- Per-chat asyncio.Lock for message sequencing (prevents edit races)
- Polling watchdog (90s stall → force restart)
- Update deduplication (skip replays after reconnection)
- Typing circuit breaker (no infinite 401 loops)
- send_document() support
- Safe send with pre/post-connect error classification
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from genesis.channels.base import ChannelAdapter
from genesis.channels.telegram.handlers_v2 import make_handlers_v2
from genesis.channels.telegram.transport.offset_store import read_offset, write_offset
from genesis.channels.telegram.transport.polling import PollingWatchdog
from genesis.channels.telegram.transport.send import (
    safe_send_document,
    safe_send_message,
    safe_send_voice,
)
from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker
from genesis.channels.telegram.transport.update_dedupe import TelegramUpdateDedupe

if TYPE_CHECKING:
    from genesis.cc.conversation import ConversationLoop
    from genesis.channels.tts_config import TTSConfigLoader
    from genesis.providers.tts import TTSProvider

log = logging.getLogger(__name__)


class TelegramAdapterV2(ChannelAdapter):
    """Telegram bot adapter V2 — resilient polling with transport layer."""

    def __init__(
        self,
        token: str,
        conversation_loop: ConversationLoop,
        allowed_users: set[int] | None = None,
        whisper_model: str = "whisper-large-v3",
        tts_provider: TTSProvider | None = None,
        config_loader: TTSConfigLoader | None = None,
        reply_waiter: object | None = None,
        engagement_tracker: object | None = None,
    ):
        self.token = token
        self.conversation_loop = conversation_loop
        self.allowed_users = allowed_users or set()
        self.whisper_model = whisper_model
        self.tts_provider = tts_provider
        self._reply_waiter = reply_waiter
        self._engagement_tracker = engagement_tracker
        self._app = None

        # Transport layer components
        self._dedupe = TelegramUpdateDedupe()
        self._typing_breaker = TypingCircuitBreaker()
        self._watchdog: PollingWatchdog | None = None
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._last_offset_write: float = 0.0

        if tts_provider:
            from genesis.channels.voice import VoiceDeliveryHelper

            self._voice_helper = VoiceDeliveryHelper(tts_provider, config_loader)
        else:
            self._voice_helper = None

    def get_chat_lock(self, chat_id: int | str, thread_id: int | None = None) -> asyncio.Lock:
        """Return per-chat lock for message sequencing."""
        key = f"{chat_id}:{thread_id}" if thread_id else str(chat_id)
        if key not in self._chat_locks:
            self._chat_locks[key] = asyncio.Lock()
        return self._chat_locks[key]

    _OFFSET_PERSIST_INTERVAL_S = 30.0  # Debounce: persist at most once per 30s

    def _persist_offset(self, *, force: bool = False) -> None:
        """Save the current polling offset to disk for restart recovery.

        Debounced to avoid blocking I/O on every update. Called with
        force=True on stop() to ensure the final offset is persisted.
        """
        import time

        if not self._app or not self._app.updater:
            return
        now = time.monotonic()
        if not force and (now - self._last_offset_write) < self._OFFSET_PERSIST_INTERVAL_S:
            return
        offset = getattr(self._app.updater, "_last_update_id", 0)
        if offset > 0:
            bot_id = str(self._app.bot.id)
            write_offset(bot_id, offset)
            self._last_offset_write = now

    async def start(self) -> None:
        handlers = make_handlers_v2(
            self.conversation_loop,
            self.allowed_users,
            self.whisper_model,
            voice_helper=self._voice_helper,
            adapter=self,
            reply_waiter=self._reply_waiter,
            engagement_tracker=self._engagement_tracker,
            db=getattr(self.conversation_loop, "_db", None),
            typing_breaker=self._typing_breaker,
            dedupe=self._dedupe,
        )

        self._app = (
            ApplicationBuilder()
            .token(self.token)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .connect_timeout(10.0)
            .build()
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", handlers["start"]))
        self._app.add_handler(CommandHandler("new", handlers["new"]))
        self._app.add_handler(CommandHandler("status", handlers["status"]))
        self._app.add_handler(CommandHandler("usage", handlers["usage"]))
        self._app.add_handler(CommandHandler("tts", handlers["tts"]))
        self._app.add_handler(CommandHandler("stop", handlers["stop"]))
        self._app.add_handler(CommandHandler("model", handlers["model"]))
        self._app.add_handler(CommandHandler("effort", handlers["effort"]))
        self._app.add_handler(CommandHandler("help", handlers["help"]))
        self._app.add_handler(CommandHandler("pause", handlers["pause"]))

        # Message handlers
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handlers["text"])
        )
        self._app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, handlers["voice"])
        )
        self._app.add_handler(
            MessageHandler(filters.PHOTO, handlers["photo"])
        )
        self._app.add_handler(
            MessageHandler(filters.Document.ALL, handlers["document"])
        )

        # Watchdog activity tracker — fires on ALL updates (not just user messages)
        # so idle periods don't trigger false stall alarms
        self._watchdog = PollingWatchdog(on_stall=self._handle_polling_stall)

        async def _record_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            self._watchdog.record_activity()
            self._persist_offset()

        self._app.add_handler(TypeHandler(Update, _record_any_update), group=-1)

        log.info("Starting Telegram bot V2 (polling)...")
        await self._app.initialize()
        await self._app.start()

        # Restore persisted offset to skip replaying already-processed updates
        bot_id = str(self._app.bot.id)
        stored_offset = read_offset(bot_id)
        if stored_offset is not None:
            self._app.updater._last_update_id = stored_offset
            log.info("Restored polling offset %d for bot %s", stored_offset, bot_id)

        await self._app.updater.start_polling(drop_pending_updates=False)
        self._watchdog.start()

    async def stop(self) -> None:
        if self._watchdog:
            await self._watchdog.stop()
        if self._app:
            # Persist offset before stopping so next restart skips replays
            self._persist_offset(force=True)
            log.info("Stopping Telegram bot V2...")
            for step_name, coro in [
                ("updater", self._app.updater.stop()),
                ("app", self._app.stop()),
                ("shutdown", self._app.shutdown()),
            ]:
                try:
                    await coro
                except Exception:
                    log.exception("Failed to stop Telegram %s", step_name)

    async def send_message(
        self, channel_id: str, text: str, *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> str:
        if not self._app:
            raise RuntimeError("Adapter not started")

        msg = await safe_send_message(
            self._app.bot,
            int(channel_id),
            text,
            parse_mode=parse_mode or "HTML",
            message_thread_id=message_thread_id,
        )
        if msg is None:
            raise RuntimeError("Failed to send message after retries")

        # Persist outreach/system messages
        try:
            db = getattr(self.conversation_loop, "_db", None)
            if db is not None:
                from genesis.db.crud.telegram_messages import store

                await store(
                    db,
                    chat_id=int(channel_id),
                    message_id=msg.message_id,
                    sender="genesis",
                    content=text,
                    thread_id=message_thread_id,
                )
        except Exception:
            log.warning("Failed to persist outreach message %s", msg.message_id, exc_info=True)

        return str(msg.message_id)

    async def send_typing(self, channel_id: str) -> None:
        if not self._app:
            return
        chat_id = int(channel_id)
        if not self._typing_breaker.should_send(chat_id):
            return
        try:
            await self._app.bot.send_chat_action(chat_id=chat_id, action="typing")
            self._typing_breaker.record_success(chat_id)
        except Exception:
            self._typing_breaker.record_failure(chat_id)

    def get_capabilities(self) -> dict:
        return {
            "markdown": True,
            "buttons": True,
            "reactions": False,
            "voice": True,
            "documents": True,
            "max_length": 4096,
        }

    async def send_voice(
        self,
        channel_id: str,
        audio_bytes: bytes,
        reply_to_message_id: str | None = None,
    ) -> str:
        if not self._app:
            raise RuntimeError("Adapter not started")
        msg = await safe_send_voice(
            self._app.bot,
            int(channel_id),
            io.BytesIO(audio_bytes),
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
        )
        if msg is None:
            raise RuntimeError("Failed to send voice after retries")
        return str(msg.message_id)

    async def send_document(
        self,
        channel_id: str,
        document: bytes | str,
        *,
        caption: str | None = None,
        filename: str | None = None,
        message_thread_id: int | None = None,
    ) -> str:
        """Send a document/file to a chat."""
        if not self._app:
            raise RuntimeError("Adapter not started")

        if isinstance(document, bytes):
            doc_input = io.BytesIO(document)
            if filename:
                doc_input.name = filename
        else:
            doc_input = document  # file_id or URL

        msg = await safe_send_document(
            self._app.bot,
            int(channel_id),
            doc_input,
            caption=caption,
            message_thread_id=message_thread_id,
        )
        if msg is None:
            raise RuntimeError("Failed to send document after retries")
        return str(msg.message_id)

    async def get_engagement_signals(self, delivery_id: str) -> dict:
        """Query recorded engagement state for an outreach delivery."""
        db = getattr(self.conversation_loop, "_db", None)
        if db is None:
            return {"signal": "neutral", "details": {}}
        try:
            from genesis.db.crud.outreach import find_by_delivery_id
            row = await find_by_delivery_id(db, delivery_id)
            if not row:
                return {"signal": "neutral", "details": {}}
            outcome = row.get("engagement_outcome")
            if outcome in ("useful",):
                return {"signal": "engaged", "details": {"outcome": outcome, "response": row.get("user_response")}}
            if outcome == "ignored":
                return {"signal": "ignored", "details": {"outcome": outcome}}
            if outcome == "ambivalent":
                return {"signal": "ambivalent", "details": {"outcome": outcome}}
            return {"signal": "neutral", "details": {"outcome": outcome}}
        except Exception:
            log.warning("Failed to query engagement signals for %s", delivery_id, exc_info=True)
            return {"signal": "neutral", "details": {}}

    async def _handle_polling_stall(self) -> None:
        """Called by watchdog when polling stalls — force restart polling."""
        log.warning("Polling stall detected — restarting updater")
        if not self._app or not self._app.updater:
            return
        try:
            await self._app.updater.stop()
        except RuntimeError as exc:
            if "not running" in str(exc).lower():
                # Updater is already cleanly stopped (e.g. after a network error
                # killed start_polling on a prior recovery). Safe to restart.
                log.info("Updater already stopped — proceeding to start_polling()")
            else:
                log.exception("Failed to stop updater — skipping restart")
                return
        except Exception:
            log.exception("Failed to stop updater — skipping restart")
            return
        try:
            await self._app.updater.start_polling(drop_pending_updates=False)
            log.info("Polling restarted successfully after stall")
        except Exception:
            log.exception(
                "Failed to restart polling — will retry on next stall cycle"
            )
