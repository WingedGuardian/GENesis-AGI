"""Telegram V2 handlers — edit-based streaming, /stop, transport layer integration.

Fixes all 12 known Telegram issues from user session analysis:
1. /stop command → CCInvoker.interrupt() (SIGINT to subprocess)
2. /model /effort handled in handler before CC (no LLM confusion)
3. Edit-based streaming via DraftStreamer (1 updating message)
4. Voice file size pre-check (>20MB rejected)
5. Typing uses circuit breaker (no infinite 401 loops)
6. Update deduplication (skip replays after reconnection)
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from genesis.channels.telegram import _handler_helpers as _helpers
from genesis.channels.telegram._handler_commands import (
    cmd_effort,
    cmd_model,
    cmd_new,
    cmd_pause,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_tts,
    cmd_usage,
)
from genesis.channels.telegram._handler_context import HandlerContext
from genesis.channels.telegram._handler_messages import (
    handle_callback_query,
    handle_document,
    handle_photo,
    handle_text,
    handle_voice,
)

_format_error = _helpers._format_error
_reply_formatted = _helpers._reply_formatted

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.conversation import ConversationLoop
    from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2
    from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker
    from genesis.channels.telegram.transport.update_dedupe import TelegramUpdateDedupe
    from genesis.channels.voice import VoiceDeliveryHelper


def make_handlers_v2(
    loop: ConversationLoop,
    allowed_users: set[int],
    whisper_model: str,
    voice_helper: VoiceDeliveryHelper | None = None,
    adapter: TelegramAdapterV2 | None = None,
    reply_waiter: object | None = None,
    engagement_tracker: object | None = None,
    db: aiosqlite.Connection | None = None,
    typing_breaker: TypingCircuitBreaker | None = None,
    dedupe: TelegramUpdateDedupe | None = None,
    autonomous_cli_gate: object | None = None,
    proposal_workflow: object | None = None,
):
    """Return V2 handler callbacks."""
    draft_streaming_enabled = os.environ.get(
        "GENESIS_TELEGRAM_DRAFT_STREAMING", "1",
    ).lower() not in ("0", "false", "no", "off")

    ctx = HandlerContext(
        loop=loop,
        allowed_users=allowed_users,
        whisper_model=whisper_model,
        voice_helper=voice_helper,
        adapter=adapter,
        reply_waiter=reply_waiter,
        engagement_tracker=engagement_tracker,
        db=db,
        typing_breaker=typing_breaker,
        dedupe=dedupe,
        draft_streaming_enabled=draft_streaming_enabled,
        autonomous_cli_gate=autonomous_cli_gate,
        proposal_workflow=proposal_workflow,
    )

    def _wrap(cmd_fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await cmd_fn(ctx, update, context)
        return wrapper

    return {
        "start": _wrap(cmd_start),
        "help": _wrap(cmd_start),
        "new": _wrap(cmd_new),
        "stop": _wrap(cmd_stop),
        "model": _wrap(cmd_model),
        "effort": _wrap(cmd_effort),
        "status": _wrap(cmd_status),
        "usage": _wrap(cmd_usage),
        "tts": _wrap(cmd_tts),
        "pause": _wrap(cmd_pause),
        "text": _wrap(handle_text),
        "voice": _wrap(handle_voice),
        "photo": _wrap(handle_photo),
        "document": _wrap(handle_document),
        "callback_query": _wrap(handle_callback_query),
    }
