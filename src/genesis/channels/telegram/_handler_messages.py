"""Message handlers for V2 Telegram handlers."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from genesis.cc.exceptions import CCError
from genesis.cc.types import ChannelType
from genesis.channels import stt
from genesis.channels.telegram._handler_helpers import (
    _format_error,
    _reply_formatted,
)
from genesis.channels.telegram.transport.streaming import DraftStreamer, generate_draft_id
from genesis.channels.telegram.transport.update_dedupe import message_key

if TYPE_CHECKING:
    from genesis.channels.telegram._handler_context import HandlerContext

log = logging.getLogger(__name__)

_MAX_VOICE_BYTES = 20 * 1024 * 1024
_MAX_MEDIA_BYTES = 20 * 1024 * 1024  # Telegram getFile() limit
_MEDIA_DIR = Path.home() / "tmp" / "tg_media"
_READABLE_MIMES = (
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf",
)


async def _persist_tg_message(
    ctx: HandlerContext,
    chat_id: int, message_id: int, sender: str, content: str,
    thread_id: str | None = None, reply_to: int | None = None,
    direction: str = "inbound",
) -> None:
    if ctx.db is None:
        return
    try:
        from genesis.db.crud.telegram_messages import store
        await store(
            ctx.db,
            chat_id=chat_id,
            message_id=message_id,
            sender=sender,
            content=content,
            thread_id=int(thread_id) if thread_id else None,
            reply_to_message_id=reply_to,
            direction=direction,
        )
    except Exception:
        log.warning("Failed to persist Telegram message %d", message_id, exc_info=True)


async def _send_typing_safe(ctx: HandlerContext, chat) -> None:
    """Send typing with circuit breaker."""
    if not ctx.typing_breaker_instance.should_send(chat.id):
        return
    try:
        await chat.send_action("typing")
        ctx.typing_breaker_instance.record_success(chat.id)
    except Exception:
        ctx.typing_breaker_instance.record_failure(chat.id)


async def _apply_pending_settings(ctx: HandlerContext, user_id: int, tid: str | None) -> None:
    """Pop and apply any pending /model or /effort settings for this user."""
    pending = ctx.pending_settings.pop(user_id, None)
    if not pending:
        return
    try:
        from genesis.db.crud import cc_sessions
        sess = await cc_sessions.get_active_foreground(
            ctx.loop._db, user_id=f"tg-{user_id}",
            channel=str(ChannelType.TELEGRAM), thread_id=tid,
        )
        if sess:
            await cc_sessions.update_model_effort(
                ctx.loop._db, sess["id"],
                model=pending.get("model"), effort=pending.get("effort"),
            )
        else:
            log.warning("Pending settings for user %s discarded — no active session", user_id)
    except Exception:
        log.error("Failed to apply pending settings for user %s", user_id, exc_info=True)


async def _make_streamer(ctx: HandlerContext, msg) -> DraftStreamer | None:
    """Create a draft streamer if conditions are met."""
    if (
        ctx.draft_streaming_enabled
        and ctx.adapter and ctx.adapter._app
        and msg.chat.type == "private"
    ):
        return DraftStreamer(
            bot=ctx.adapter._app.bot,
            chat_id=msg.chat.id,
            draft_id=generate_draft_id(),
            message_thread_id=msg.message_thread_id,
        )
    return None


async def _handle_text_inner(ctx: HandlerContext, msg, user, tid):
    """Inner implementation for text handling."""
    from genesis.channels.telegram._handler_helpers import _TypingKeepAliveV2

    interrupt_event = asyncio.Event()
    ikey = (user.id, msg.chat.id)
    ctx.active_interrupts[ikey] = interrupt_event

    streamer = await _make_streamer(ctx, msg)
    on_event = ctx.make_on_event(interrupt_event, streamer)
    typing_ka = _TypingKeepAliveV2(msg.chat, ctx.typing_breaker_instance)

    # One-shot status snapshot: if CC takes >60s, persist draft as real message
    async def _status_snapshot() -> None:
        await asyncio.sleep(60)
        if streamer and streamer.any_draft_sent and not interrupt_event.is_set():
            draft_text = streamer._compose_draft()
            if draft_text.strip():
                try:
                    await msg.reply_text(draft_text)
                except Exception:
                    log.debug("Status snapshot send failed", exc_info=True)

    from genesis.util.tasks import tracked_task
    status_task = tracked_task(_status_snapshot(), name="status-snapshot")

    try:
        await _send_typing_safe(ctx, msg.chat)
        typing_ka.start()

        response = await ctx.loop.handle_message_streaming(
            msg.text,
            user_id=f"tg-{user.id}",
            channel=ChannelType.TELEGRAM,
            on_event=on_event,
            thread_id=tid,
        )
        log.info("Response to %s (%d chars)", user.id, len(response or ""))

        await _apply_pending_settings(ctx, user.id, tid)

        sent_msg = None
        if interrupt_event.is_set():
            if streamer:
                await streamer.flush()
                streamer.disable()
            await msg.reply_text("Stopped.")
        else:
            if streamer:
                streamer.disable()
            # Always send text first — voice is additional, never a replacement
            if response:
                response = response.lstrip("\n")
                sent_msg = await _reply_formatted(msg, response)

            # Then send voice if wanted (text already delivered; TTS failure is non-fatal)
            if ctx.want_voice(msg.chat.id, input_was_voice=False) and response and ctx.adapter:
                try:
                    await ctx.voice_helper.synthesize_and_deliver(
                        ctx.adapter,
                        str(msg.chat.id),
                        response,
                        reply_to_message_id=str(msg.message_id),
                    )
                except Exception:
                    log.warning("Voice delivery failed for %s", user.id, exc_info=True)

        if response:
            out_id = sent_msg.message_id if sent_msg else msg.message_id
            await _persist_tg_message(
                ctx, msg.chat.id, out_id, "genesis", response,
                thread_id=tid, direction="outbound",
            )

    except CCError as e:
        log.error("CC error for user %s: %s", user.id, e, exc_info=True)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to user %s", user.id, exc_info=True)
    except (TimeoutError, ConnectionError, OSError):
        if streamer and streamer.accumulated_text:
            log.warning(
                "Connection error for user %s after streaming %d chars — delivering accumulated text",
                user.id, len(streamer.accumulated_text),
            )
            try:
                sent = await _reply_formatted(msg, streamer.accumulated_text)
                if sent:
                    await _persist_tg_message(
                        ctx, msg.chat.id, sent.message_id, "genesis",
                        streamer.accumulated_text,
                        thread_id=tid, direction="outbound",
                    )
            except Exception:
                log.error(
                    "Failed to deliver accumulated text after connection error for user %s",
                    user.id, exc_info=True,
                )
        else:
            error_text = "Connection issue reaching Genesis."
            log.error("Connection/timeout error for user %s", user.id, exc_info=True)
            try:
                sent = await msg.reply_text(error_text)
                await _persist_tg_message(
                    ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                    thread_id=tid, direction="outbound",
                )
            except Exception:
                log.error("Failed to send connection-error reply for user %s", user.id, exc_info=True)
    except Exception as e:
        log.exception("CC request failed for user %s", user.id)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to user %s", user.id, exc_info=True)
    finally:
        await typing_ka.stop()
        status_task.cancel()
        ctx.active_interrupts.pop(ikey, None)


async def _handle_voice_inner(ctx: HandlerContext, msg, user, voice, context, whisper_model_name):
    """Inner implementation for voice handling."""
    from genesis.channels.telegram._handler_helpers import _TypingKeepAliveV2

    tid = ctx.thread_id_from_msg(msg)
    interrupt_event = asyncio.Event()
    ikey = (user.id, msg.chat.id)
    ctx.active_interrupts[ikey] = interrupt_event

    streamer = await _make_streamer(ctx, msg)
    on_event = ctx.make_on_event(interrupt_event, streamer)
    typing_ka = _TypingKeepAliveV2(msg.chat, ctx.typing_breaker_instance)

    # One-shot status snapshot: if CC takes >60s, persist draft as real message
    async def _status_snapshot() -> None:
        await asyncio.sleep(60)
        if streamer and streamer.any_draft_sent and not interrupt_event.is_set():
            draft_text = streamer._compose_draft()
            if draft_text.strip():
                try:
                    await msg.reply_text(draft_text)
                except Exception:
                    log.debug("Status snapshot send failed", exc_info=True)

    from genesis.util.tasks import tracked_task
    status_task = tracked_task(_status_snapshot(), name="voice-status-snapshot")

    try:
        await _send_typing_safe(ctx, msg.chat)

        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()
        text = await stt.transcribe(bytes(audio_bytes), model_name=whisper_model_name)

        if not text:
            await msg.reply_text("(couldn't transcribe audio)")
            return

        await _persist_tg_message(
            ctx, msg.chat.id, msg.message_id, "user", f"[voice] {text}",
            thread_id=tid,
        )

        typing_ka.start()

        response = await ctx.loop.handle_message_streaming(
            text,
            user_id=f"tg-{user.id}",
            channel=ChannelType.TELEGRAM,
            on_event=on_event,
            thread_id=tid,
        )
        log.info("Voice response to %s (%d chars)", user.id, len(response or ""))

        await _apply_pending_settings(ctx, user.id, tid)

        sent_msg = None
        if interrupt_event.is_set():
            if streamer:
                await streamer.flush()
                streamer.disable()
            await msg.reply_text("Stopped.")
        else:
            if streamer:
                streamer.disable()
            # Send response first — voice and transcription echo follow separately
            if response:
                response = response.lstrip("\n")
            sent_msg = await _reply_formatted(msg, response or "(no response)")

            # Then send voice if wanted (text already delivered; TTS failure is non-fatal)
            if ctx.want_voice(msg.chat.id, input_was_voice=True) and response and ctx.adapter:
                try:
                    await ctx.voice_helper.synthesize_and_deliver(
                        ctx.adapter,
                        str(msg.chat.id),
                        response,
                        reply_to_message_id=str(msg.message_id),
                    )
                except Exception:
                    log.warning("Voice delivery failed for %s", user.id, exc_info=True)

            # Transcription echo — best-effort, after response delivery
            try:
                await msg.reply_text(f"\U0001f3a4 {text}")
            except Exception:
                log.debug("Transcription echo failed for %s", user.id, exc_info=True)

        if response:
            out_id = sent_msg.message_id if sent_msg else msg.message_id
            await _persist_tg_message(
                ctx, msg.chat.id, out_id, "genesis", response,
                thread_id=tid, direction="outbound",
            )

    except CCError as e:
        log.error("CC error for voice user %s: %s", user.id, e, exc_info=True)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to voice user %s", user.id, exc_info=True)
    except (TimeoutError, ConnectionError, OSError):
        if streamer and streamer.accumulated_text:
            log.warning(
                "Connection error for voice %s after streaming %d chars — delivering accumulated text",
                user.id, len(streamer.accumulated_text),
            )
            try:
                sent = await _reply_formatted(msg, streamer.accumulated_text)
                if sent:
                    await _persist_tg_message(
                        ctx, msg.chat.id, sent.message_id, "genesis",
                        streamer.accumulated_text,
                        thread_id=tid, direction="outbound",
                    )
            except Exception:
                log.error(
                    "Failed to deliver accumulated text after connection error for voice %s",
                    user.id, exc_info=True,
                )
        else:
            error_text = "Connection issue processing your voice message."
            log.error("Connection/timeout for voice user %s", user.id, exc_info=True)
            try:
                sent = await msg.reply_text(error_text)
                await _persist_tg_message(
                    ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                    thread_id=tid, direction="outbound",
                )
            except Exception:
                log.error(
                    "Failed to send connection-error reply to voice %s",
                    user.id, exc_info=True,
                )
    except Exception as e:
        log.exception("Voice handling failed for user %s", user.id)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to voice user %s", user.id, exc_info=True)
    finally:
        await typing_ka.stop()
        status_task.cancel()
        ctx.active_interrupts.pop(ikey, None)


async def _handle_media_inner(
    ctx: HandlerContext, msg, user, file_path: Path,
    caption: str | None, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Process a downloaded media file through CC."""
    from genesis.channels.telegram._handler_helpers import _TypingKeepAliveV2

    tid = ctx.thread_id_from_msg(msg)
    interrupt_event = asyncio.Event()
    ikey = (user.id, msg.chat.id)
    ctx.active_interrupts[ikey] = interrupt_event

    streamer = await _make_streamer(ctx, msg)
    on_event = ctx.make_on_event(interrupt_event, streamer)
    typing_ka = _TypingKeepAliveV2(msg.chat, ctx.typing_breaker_instance)

    async def _status_snapshot() -> None:
        await asyncio.sleep(60)
        if streamer and streamer.any_draft_sent and not interrupt_event.is_set():
            draft_text = streamer._compose_draft()
            if draft_text.strip():
                try:
                    await msg.reply_text(draft_text)
                except Exception:
                    log.debug("Status snapshot send failed", exc_info=True)

    from genesis.util.tasks import tracked_task
    status_task = tracked_task(_status_snapshot(), name="media-status-snapshot")

    # Build prompt — CC's Read tool will open the file (images, PDFs)
    if caption:
        prompt = f"{caption}\n\n[Attached file: {file_path}]"
    else:
        prompt = f"The user sent a file. Read and analyze it.\n\n[Attached file: {file_path}]"

    label = f"[media] {caption or file_path.name}"
    await _persist_tg_message(ctx, msg.chat.id, msg.message_id, "user", label, thread_id=tid)

    try:
        await _send_typing_safe(ctx, msg.chat)
        typing_ka.start()

        response = await ctx.loop.handle_message_streaming(
            prompt,
            user_id=f"tg-{user.id}",
            channel=ChannelType.TELEGRAM,
            on_event=on_event,
            thread_id=tid,
        )
        log.info("Media response to %s (%d chars)", user.id, len(response or ""))

        await _apply_pending_settings(ctx, user.id, tid)

        sent_msg = None
        if interrupt_event.is_set():
            if streamer:
                await streamer.flush()
                streamer.disable()
            await msg.reply_text("Stopped.")
        else:
            if streamer:
                streamer.disable()
            if response:
                response = response.lstrip("\n")
                sent_msg = await _reply_formatted(msg, response)

        if response:
            out_id = sent_msg.message_id if sent_msg else msg.message_id
            await _persist_tg_message(
                ctx, msg.chat.id, out_id, "genesis", response,
                thread_id=tid, direction="outbound",
            )

    except CCError as e:
        log.error("CC error for media user %s: %s", user.id, e, exc_info=True)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to media user %s", user.id, exc_info=True)
    except (TimeoutError, ConnectionError, OSError):
        if streamer and streamer.accumulated_text:
            log.warning(
                "Connection error for media %s after streaming %d chars — delivering accumulated text",
                user.id, len(streamer.accumulated_text),
            )
            try:
                sent = await _reply_formatted(msg, streamer.accumulated_text)
                if sent:
                    await _persist_tg_message(
                        ctx, msg.chat.id, sent.message_id, "genesis",
                        streamer.accumulated_text,
                        thread_id=tid, direction="outbound",
                    )
            except Exception:
                log.error(
                    "Failed to deliver accumulated text after connection error for media %s",
                    user.id, exc_info=True,
                )
        else:
            error_text = "Connection issue processing your file."
            log.error("Connection/timeout for media user %s", user.id, exc_info=True)
            try:
                sent = await msg.reply_text(error_text)
                await _persist_tg_message(
                    ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                    thread_id=tid, direction="outbound",
                )
            except Exception:
                log.error(
                    "Failed to send connection-error reply to media %s",
                    user.id, exc_info=True,
                )
    except Exception as e:
        log.exception("Media handling failed for user %s", user.id)
        error_text = _format_error(e)
        try:
            sent = await msg.reply_text(error_text)
            await _persist_tg_message(
                ctx, msg.chat.id, sent.message_id, "genesis", error_text,
                thread_id=tid, direction="outbound",
            )
        except Exception:
            log.error("Failed to send error reply to media user %s", user.id, exc_info=True)
    finally:
        await typing_ka.stop()
        status_task.cancel()
        ctx.active_interrupts.pop(ikey, None)
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            log.warning("Failed to clean up temp media file %s", file_path, exc_info=True)


async def handle_text(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    msg = update.message
    if not msg or not msg.text:
        return

    if ctx.dedupe_instance.should_skip(message_key(msg.chat.id, msg.message_id)):
        return

    log.info("Text from %s (%d chars)", user.id, len(msg.text))

    await _persist_tg_message(
        ctx, msg.chat.id, msg.message_id, "user", msg.text,
        thread_id=ctx.thread_id(update),
        reply_to=msg.reply_to_message.message_id if msg.reply_to_message else None,
    )

    if msg.reply_to_message:
        reply_to_id = str(msg.reply_to_message.message_id)

        # Resolve autonomous CLI fallback approvals before generic reply waiters.
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            gate = getattr(rt, "_autonomous_cli_approval_gate", None)
            if gate is not None and await gate.resolve_from_reply(reply_to_id, msg.text):
                log.info("Autonomous CLI approval resolved for delivery %s", reply_to_id)
                return
        except Exception:
            log.warning("Failed to resolve approval reply", exc_info=True)

        # Record engagement if this is a reply to an outreach message
        if ctx.engagement_tracker and ctx.db:
            try:
                from genesis.db.crud.outreach import find_by_delivery_id
                outreach_record = await find_by_delivery_id(ctx.db, reply_to_id)
                if outreach_record:
                    await ctx.engagement_tracker.record_reply(
                        outreach_record["id"], msg.text,
                    )
                    log.info("Engagement recorded for outreach %s", outreach_record["id"])
            except Exception:
                log.warning("Failed to record engagement for reply", exc_info=True)

        # Resolve ReplyWaiter for bidirectional outreach (send-and-wait)
        if ctx.reply_waiter and ctx.reply_waiter.resolve(reply_to_id, msg.text):
            log.info("Outreach reply resolved for delivery %s", reply_to_id)
            return

    # Record implicit engagement: user is active after receiving outreach
    if ctx.engagement_tracker and ctx.db:
        try:
            from genesis.db.crud.outreach import find_recent_unengaged
            recent = await find_recent_unengaged(ctx.db)
            for rec in recent:
                await ctx.engagement_tracker.record_implicit_engagement(rec["id"])
        except Exception:
            log.debug("Implicit engagement check failed", exc_info=True)

    if ctx.adapter and ctx.adapter._watchdog:
        ctx.adapter._watchdog.record_activity()

    tid = ctx.thread_id(update)

    chat_lock = (
        ctx.adapter.get_chat_lock(msg.chat.id, msg.message_thread_id)
        if ctx.adapter else asyncio.Lock()
    )
    async with chat_lock:
        await _handle_text_inner(ctx, msg, user, tid)


async def handle_voice(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    msg = update.message
    voice = msg.voice or msg.audio if msg else None
    if not voice:
        return

    if ctx.dedupe_instance.should_skip(message_key(msg.chat.id, msg.message_id)):
        return

    if voice.file_size and voice.file_size > _MAX_VOICE_BYTES:
        await msg.reply_text(
            f"Voice file too large ({voice.file_size // (1024*1024)}MB). "
            f"Maximum is {_MAX_VOICE_BYTES // (1024*1024)}MB."
        )
        return

    log.info("Voice from %s: %s bytes", user.id, voice.file_size)

    if ctx.adapter and ctx.adapter._watchdog:
        ctx.adapter._watchdog.record_activity()

    chat_lock = (
        ctx.adapter.get_chat_lock(msg.chat.id, msg.message_thread_id)
        if ctx.adapter else asyncio.Lock()
    )
    async with chat_lock:
        await _handle_voice_inner(
            ctx, msg, user, voice, context, ctx.whisper_model,
        )


async def handle_photo(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    msg = update.message
    if not msg or not msg.photo:
        return
    if ctx.dedupe_instance.should_skip(message_key(msg.chat.id, msg.message_id)):
        return

    # Take largest resolution (last in array)
    photo = msg.photo[-1]
    if photo.file_size and photo.file_size > _MAX_MEDIA_BYTES:
        await msg.reply_text(
            f"Photo too large ({photo.file_size // (1024 * 1024)}MB). "
            f"Maximum is {_MAX_MEDIA_BYTES // (1024 * 1024)}MB."
        )
        return

    log.info("Photo from %s: %s bytes", user.id, photo.file_size)

    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    tg_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await tg_file.download_as_bytearray()

    if len(photo_bytes) > _MAX_MEDIA_BYTES:
        await msg.reply_text("Photo too large after download. Maximum is 20MB.")
        return

    file_path = _MEDIA_DIR / f"photo_{msg.chat.id}_{msg.message_id}_{int(time.time())}.jpg"
    await asyncio.to_thread(file_path.write_bytes, photo_bytes)

    if ctx.adapter and ctx.adapter._watchdog:
        ctx.adapter._watchdog.record_activity()

    chat_lock = (
        ctx.adapter.get_chat_lock(msg.chat.id, msg.message_thread_id)
        if ctx.adapter else asyncio.Lock()
    )
    try:
        async with chat_lock:
            await _handle_media_inner(ctx, msg, user, file_path, msg.caption, context)
    finally:
        # Belt-and-suspenders: inner handler has its own cleanup, but this
        # catches CancelledError or any exception before inner handler entry
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            log.warning("Failed to clean up temp photo %s", file_path, exc_info=True)


async def handle_document(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    msg = update.message
    doc = msg.document if msg else None
    if not doc:
        return
    if ctx.dedupe_instance.should_skip(message_key(msg.chat.id, msg.message_id)):
        return

    mime = (doc.mime_type or "").lower()
    if mime not in _READABLE_MIMES:
        await msg.reply_text(
            f"I can read images (JPEG/PNG/GIF/WebP) and PDFs. "
            f"This file type ({mime or 'unknown'}) isn't supported yet."
        )
        return

    if doc.file_size and doc.file_size > _MAX_MEDIA_BYTES:
        await msg.reply_text(
            f"File too large ({doc.file_size // (1024 * 1024)}MB). "
            f"Maximum is {_MAX_MEDIA_BYTES // (1024 * 1024)}MB."
        )
        return

    log.info("Document from %s: %s (%s, %s bytes)", user.id, doc.file_name, mime, doc.file_size)

    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ext = Path(doc.file_name).suffix if doc.file_name else ".bin"
    except (ValueError, TypeError):
        ext = ".bin"
    tg_file = await context.bot.get_file(doc.file_id)
    doc_bytes = await tg_file.download_as_bytearray()

    if len(doc_bytes) > _MAX_MEDIA_BYTES:
        await msg.reply_text("File too large after download. Maximum is 20MB.")
        return

    file_path = _MEDIA_DIR / f"doc_{msg.chat.id}_{msg.message_id}_{int(time.time())}{ext}"
    await asyncio.to_thread(file_path.write_bytes, doc_bytes)

    if ctx.adapter and ctx.adapter._watchdog:
        ctx.adapter._watchdog.record_activity()

    chat_lock = (
        ctx.adapter.get_chat_lock(msg.chat.id, msg.message_thread_id)
        if ctx.adapter else asyncio.Lock()
    )
    try:
        async with chat_lock:
            await _handle_media_inner(ctx, msg, user, file_path, msg.caption, context)
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            log.warning("Failed to clean up temp doc %s", file_path, exc_info=True)
