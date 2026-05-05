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


_MODEL_PREFIX_GAP_S = 3600  # Show model/effort prefix after 1h of silence


async def _make_streamer(ctx: HandlerContext, msg, user, tid) -> DraftStreamer | None:
    """Create a draft streamer if conditions are met.

    Seeds the streamer with a model/effort prefix and flushes immediately
    so the user sees what model they're talking to before inference starts.
    Only shows the prefix when >1h has elapsed since the user's last message
    in this private chat (i.e. when coming back cold).
    """
    if not (
        ctx.draft_streaming_enabled
        and ctx.adapter and ctx.adapter._app
        and msg.chat.type == "private"
    ):
        return None

    # Determine whether to show model/effort prefix based on message gap
    prefix = ""
    if ctx.db:
        try:
            from genesis.db.crud import cc_sessions

            # Check time since last inbound message in this chat
            show_prefix = False
            row = await ctx.db.execute(
                """SELECT timestamp FROM telegram_messages
                   WHERE chat_id = ? AND direction = 'inbound'
                   ORDER BY timestamp DESC LIMIT 1 OFFSET 1""",
                (msg.chat.id,),
            )
            prev = await row.fetchone()
            if prev is None:
                # First message ever — show prefix
                show_prefix = True
            else:
                from datetime import UTC, datetime
                prev_ts = datetime.fromisoformat(prev[0])
                now = datetime.now(UTC)
                # Handle naive timestamps
                if prev_ts.tzinfo is None:
                    prev_ts = prev_ts.replace(tzinfo=UTC)
                show_prefix = (now - prev_ts).total_seconds() >= _MODEL_PREFIX_GAP_S

            if show_prefix:
                session = await cc_sessions.get_active_foreground(
                    ctx.db,
                    user_id=f"tg-{user.id}",
                    channel=str(ChannelType.TELEGRAM),
                    thread_id=tid,
                )
                if session:
                    model = (session.get("model") or "sonnet").title()
                    effort = session.get("effort") or "medium"
                    prefix = f"[{model} / {effort}]"
        except Exception:
            log.debug("Could not resolve model/effort prefix", exc_info=True)

    streamer = DraftStreamer(
        bot=ctx.adapter._app.bot,
        chat_id=msg.chat.id,
        draft_id=generate_draft_id(),
        message_thread_id=msg.message_thread_id,
        prefix=prefix,
    )

    # Flush immediately — user sees prefix before inference starts
    if prefix:
        await streamer.flush()

    return streamer


async def _handle_text_inner(ctx: HandlerContext, msg, user, tid):
    """Inner implementation for text handling."""
    from genesis.channels.telegram._handler_helpers import _TypingKeepAliveV2

    interrupt_event = asyncio.Event()
    ikey = (user.id, msg.chat.id)
    ctx.active_interrupts[ikey] = interrupt_event

    streamer = await _make_streamer(ctx, msg, user, tid)
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
                if streamer and streamer._prefix:
                    response = streamer._prefix + "\n" + response
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

    streamer = await _make_streamer(ctx, msg, user, tid)
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
                if streamer and streamer._prefix:
                    response = streamer._prefix + "\n" + response
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

    streamer = await _make_streamer(ctx, msg, user, tid)
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
                if streamer and streamer._prefix:
                    response = streamer._prefix + "\n" + response
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


_BARE_APPROVE_WORDS = frozenset({"approve", "approved", "ok", "yes", "lgtm"})
_BARE_REJECT_WORDS = frozenset({"reject", "rejected", "deny", "denied", "no"})


def _bare_decision(text: str) -> str | None:
    """Return 'approved'/'rejected' iff the entire message is a single
    bare approve/reject word.  Rejects anything that's not an exact
    single-token match so general conversation never triggers."""
    stripped = (text or "").strip().lower()
    if not stripped:
        return None
    # Allow optional trailing punctuation on a single token.
    import re
    cleaned = re.sub(r"[^\w]", "", stripped) if " " not in stripped else stripped
    if " " in cleaned:
        return None
    if cleaned in _BARE_APPROVE_WORDS:
        return "approved"
    if cleaned in _BARE_REJECT_WORDS:
        return "rejected"
    return None


async def _try_bare_approval_resolution(
    ctx: HandlerContext, msg, user,
) -> bool:
    """Resolve a bare 'approve'/'reject' typed into the Approvals topic.

    Returns True if the message was consumed as an approval resolution
    (caller should return early).  False if the message is not a bare
    approval word, or is not inside the Approvals topic, or there is no
    pending autonomous_cli_fallback request to resolve.

    This is the fix for the "I typed 'approve' and nothing happened
    because I didn't formally quote-reply" UX bug: in the Approvals
    topic, a bare 'approve' resolves the most recent pending request.
    """
    if ctx.autonomous_cli_gate is None:
        return False
    decision = _bare_decision(msg.text)
    if decision is None:
        return False
    # Must be inside a forum topic (not general chat or DM).
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return False
    # Must be the Approvals topic specifically.  Look up via the
    # OutreachPipeline's public topic_manager property.  The pipeline
    # is fetched from the runtime singleton because the topic_manager
    # is created AFTER the Telegram adapter starts (it needs adapter.
    # _app.bot), so we cannot wire it into HandlerContext at build time.
    # Adding "approvals" to the pre-create list in bridge.py means the
    # topic exists from startup and get_thread_id returns immediately.
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = rt.outreach_pipeline
        if pipeline is None:
            return False
        topic_manager = pipeline.topic_manager
        if topic_manager is None:
            return False
        approvals_thread_id = topic_manager.get_thread_id("approvals")
        if approvals_thread_id is None or approvals_thread_id != thread_id:
            return False
    except Exception:
        log.warning(
            "Topic manager lookup failed for bare approval resolution",
            exc_info=True,
        )
        return False

    try:
        resolved_id = await ctx.autonomous_cli_gate.resolve_most_recent_pending(
            decision=decision,
            resolved_by=f"telegram:bare_text:{user.id}",
        )
    except Exception:
        log.error("Failed to resolve bare approval", exc_info=True)
        return False
    if resolved_id is None:
        log.debug(
            "Bare %s in Approvals topic ignored — no pending requests",
            decision,
        )
        return False
    log.info(
        "Bare %s in Approvals topic resolved request %s (user %s)",
        decision, resolved_id, user.id,
    )
    try:
        ack = "✅ Approved" if decision == "approved" else "❌ Rejected"
        await msg.reply_text(f"{ack} request <code>{resolved_id}</code>",
                             parse_mode="HTML")
    except Exception:
        log.debug("Failed to ack bare approval", exc_info=True)
    return True



async def _try_proposal_resolution(ctx: HandlerContext, msg, reply_to_id: str) -> bool:
    """Resolve a proposal batch from a quote-reply to an ego digest.

    Returns True if resolved (caller should return), False to continue chain.
    """
    if ctx.proposal_workflow is None or ctx.db is None:
        return False
    try:
        from genesis.db.crud import ego as ego_crud
        from genesis.ego.proposals import parse_proposal_decisions

        batch_id = await ego_crud.get_batch_for_delivery(ctx.db, reply_to_id)
        if batch_id is None:
            return False

        decisions = parse_proposal_decisions(msg.text)
        if not decisions:
            return False  # Unparseable — fall through to correction store

        # Handle "approve all" / "reject all" (sentinel key 0)
        if 0 in decisions:
            status, reason = decisions[0]
            # Get all proposals in this batch and resolve them all
            proposals = await ego_crud.list_proposals_by_batch(ctx.db, batch_id)
            all_decisions = {
                i + 1: (status, reason) for i in range(len(proposals))
            }
            results = await ctx.proposal_workflow.resolve_proposals(
                batch_id, all_decisions,
            )
        else:
            results = await ctx.proposal_workflow.resolve_proposals(
                batch_id, decisions,
            )

        # Send confirmation
        approved = sum(1 for s in results.values() if s == "approved")
        rejected = sum(1 for s in results.values() if s == "rejected")
        parts = []
        if approved:
            parts.append(f"{approved} approved")
        if rejected:
            parts.append(f"{rejected} rejected")
        already = len(decisions) - len(results) if 0 not in decisions else 0
        if already > 0:
            parts.append(f"{already} already resolved")
        summary = ", ".join(parts) or "no changes"
        try:
            await msg.reply_text(f"✅ Resolved: {summary}")
        except Exception:
            log.debug("Failed to send proposal resolution ack", exc_info=True)
        return True
    except Exception:
        log.warning("Proposal resolution failed", exc_info=True)
        return False


async def _try_bare_proposal_resolution(ctx: HandlerContext, msg) -> bool:
    """Resolve the most recent pending proposal batch from a bare message.

    Fires for non-reply messages in the ego_proposals topic that parse
    as proposal decisions. Resolves the most recent unresolved batch.
    Returns True if resolved, False to fall through to correction store.
    """
    if ctx.proposal_workflow is None or ctx.db is None:
        return False

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return False

    # Check we're in the ego_proposals topic
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = rt.outreach_pipeline
        if pipeline is None:
            return False
        topic_manager = pipeline.topic_manager
        if topic_manager is None:
            return False
        ego_thread_id = topic_manager.get_thread_id("ego_proposals")
        if ego_thread_id is None or ego_thread_id != thread_id:
            return False
    except Exception:
        return False

    # Parse the text — if unparseable, fall through to correction store
    from genesis.ego.proposals import parse_proposal_decisions

    decisions = parse_proposal_decisions(msg.text)
    if not decisions:
        return False

    # Find the most recent unresolved batch
    try:
        from genesis.db.crud import ego as ego_crud

        # Get most recent pending proposals to find their batch
        pending = await ego_crud.list_pending_proposals(ctx.db)
        if not pending:
            return False
        # All pending proposals share a batch_id
        batch_id = pending[0].get("batch_id")
        if not batch_id:
            return False

        # Handle "approve all" / "reject all" (sentinel key 0)
        if 0 in decisions:
            status, reason = decisions[0]
            all_decisions = {
                i + 1: (status, reason) for i in range(len(pending))
            }
            results = await ctx.proposal_workflow.resolve_proposals(
                batch_id, all_decisions,
            )
        else:
            results = await ctx.proposal_workflow.resolve_proposals(
                batch_id, decisions,
            )

        approved = sum(1 for s in results.values() if s == "approved")
        rejected = sum(1 for s in results.values() if s == "rejected")
        parts = []
        if approved:
            parts.append(f"{approved} approved")
        if rejected:
            parts.append(f"{rejected} rejected")
        summary = ", ".join(parts) or "no changes"
        try:
            await msg.reply_text(f"\u2705 Resolved: {summary}")
        except Exception:
            log.debug("Failed to send bare proposal resolution ack", exc_info=True)
        return True
    except Exception:
        log.warning("Bare proposal resolution failed", exc_info=True)
        return False


async def _try_ego_correction_store(ctx: HandlerContext, msg) -> bool:
    """Store non-reply messages in the ego_proposals topic as user corrections.

    These messages are general input to the ego (corrections, context,
    instructions) rather than proposal approve/reject replies.  Stored
    in the memory system so the ego can recall them in future cycles.
    """
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return False

    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = rt.outreach_pipeline
        if pipeline is None:
            return False
        topic_manager = pipeline.topic_manager
        if topic_manager is None:
            return False
        ego_thread_id = topic_manager.get_thread_id("ego_proposals")
        if ego_thread_id is None or ego_thread_id != thread_id:
            return False
    except Exception:
        return False

    # This message is in the ego_proposals topic and is not a quote-reply.
    # Store it as a user correction for the ego.
    try:
        store = rt._memory_store
        if store is not None:
            await store.store(
                content=f"User correction (ego): {msg.text}",
                source="telegram_ego_correction",
                tags=["user_correction", "ego"],
                memory_type="episodic",
                wing="autonomy",
                room="ego",
            )
            log.info("Stored ego user correction (%d chars)", len(msg.text))
            try:
                await msg.reply_text("Noted — the ego will see this next cycle.")
            except Exception:
                log.debug("Failed to ack ego correction", exc_info=True)
            return True
    except Exception:
        log.warning("Failed to store ego correction in memory", exc_info=True)
    return False


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

    # Bare "approve"/"reject" typed into the Approvals topic — resolve
    # the most recent pending autonomous CLI approval without requiring
    # a formal Telegram quote-reply.  No-op for anything else.
    if await _try_bare_approval_resolution(ctx, msg, user):
        return

    # Bare proposal decisions in the ego_proposals topic (no quote-reply needed)
    if not msg.reply_to_message and await _try_bare_proposal_resolution(ctx, msg):
        return

    # Messages in the ego_proposals topic (that aren't quote-replies to
    # proposals and weren't parsed as decisions) get stored as
    # user corrections for the ego's next cycle.
    if not msg.reply_to_message and await _try_ego_correction_store(ctx, msg):
        return

    if msg.reply_to_message:
        reply_to_id = str(msg.reply_to_message.message_id)

        # Resolve autonomous CLI fallback approvals before generic reply waiters.
        if ctx.autonomous_cli_gate is not None:
            try:
                if await ctx.autonomous_cli_gate.resolve_from_reply(reply_to_id, msg.text):
                    log.info("Autonomous CLI approval resolved for delivery %s", reply_to_id)
                    return
            except Exception:
                log.warning("Failed to resolve approval reply", exc_info=True)

        # Resolve proposal batch approvals from quote-reply to ego digest
        if await _try_proposal_resolution(ctx, msg, reply_to_id):
            log.info("Proposal batch resolved for delivery %s", reply_to_id)
            return

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

    # resolve_any_pending DISABLED — it conflates messages across chats/topics.
    # A DM message resolved an alert-topic approval request. Use quote-reply
    # or inline keyboard buttons instead. See plan: fluttering-humming-bentley.md

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


async def handle_callback_query(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses (approval flows).

    Recognized callback_data prefixes:

    - ``approve:{waiter_key}`` / ``reject:{waiter_key}`` — Sentinel
      blocking-approval flow; resolves a ``ReplyWaiter`` keyed by
      ``waiter_key``.
    - ``cli_approve:{request_id}`` — autonomous CLI fallback single-approve;
      resolves the referenced request directly via
      ``AutonomousCliApprovalGate.resolve_request``.  Bypasses ReplyWaiter.
    - ``cli_approve_all:{request_id}`` — autonomous CLI fallback batch-
      approve; resolves the triggering request first (for correct message
      edit) then calls ``approve_all_pending`` to clear every remaining
      pending ``autonomous_cli_fallback`` row.
    """
    query = update.callback_query
    if not query:
        return

    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        await query.answer("Not authorized", show_alert=True)
        return

    await query.answer()  # Dismiss spinner immediately

    data = query.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        return

    action, key = parts[0], parts[1]

    # --- Autonomous CLI fallback: single approve ---
    if action == "cli_approve":
        if ctx.autonomous_cli_gate is None:
            log.error(
                "cli_approve button pressed but autonomous_cli_gate is not "
                "wired into HandlerContext — approval request %s not resolved",
                key,
            )
            return
        try:
            ok = await ctx.autonomous_cli_gate.resolve_request(
                key, decision="approved", resolved_by=f"telegram:button:{user.id}",
            )
        except Exception:
            log.error(
                "Failed to resolve cli_approve for request %s", key, exc_info=True,
            )
            return
        label = "✅ Approved" if ok else "⚠️ Already resolved"
        log.info("cli_approve %s → %s (user %s)", key, label, user.id)
        try:
            original = query.message.text_html or query.message.text or ""
            await query.edit_message_text(
                text=f"{original}\n\n<b>{label}</b>",
                parse_mode="HTML",
            )
        except Exception:
            log.debug("Failed to edit message after cli_approve", exc_info=True)
        return

    # --- Autonomous CLI fallback: batch approve ---
    if action == "cli_approve_all":
        if ctx.autonomous_cli_gate is None:
            log.error(
                "cli_approve_all button pressed but autonomous_cli_gate is not "
                "wired into HandlerContext — request %s not resolved",
                key,
            )
            return
        try:
            # Resolve the triggering request first so the message edit
            # reflects the click, even if approve_all_pending re-resolves
            # it as part of the batch sweep.
            triggered_ok = await ctx.autonomous_cli_gate.resolve_request(
                key, decision="approved",
                resolved_by=f"telegram:batch:{user.id}",
            )
            batch_count = await ctx.autonomous_cli_gate.approve_all_pending(
                resolved_by=f"telegram:batch:{user.id}",
            )
        except Exception:
            log.error(
                "Failed to resolve cli_approve_all for %s", key, exc_info=True,
            )
            return
        total = batch_count + (1 if triggered_ok else 0)
        label = f"✅ Approved ({total} total)" if total else "⚠️ Already resolved"
        log.info(
            "cli_approve_all triggered by %s: triggered=%s batch=%d total=%d (user %s)",
            key, triggered_ok, batch_count, total, user.id,
        )
        try:
            original = query.message.text_html or query.message.text or ""
            await query.edit_message_text(
                text=f"{original}\n\n<b>{label}</b>",
                parse_mode="HTML",
            )
        except Exception:
            log.debug("Failed to edit message after cli_approve_all", exc_info=True)
        return

    # --- Sentinel-style reply_waiter flow (existing behavior) ---
    if action not in ("approve", "reject"):
        log.warning("Unexpected callback action: %r", action)
        return

    if ctx.reply_waiter:
        resolved = ctx.reply_waiter.resolve(key, action)
        if resolved:
            decision = "Approved" if action == "approve" else "Rejected"
            log.info("Callback query resolved waiter %s: %s (user %s)", key, decision, user.id)
            try:
                original = query.message.text_html or query.message.text or ""
                await query.edit_message_text(
                    text=f"{original}\n\n<b>{decision}</b>",
                    parse_mode="HTML",
                )
            except Exception:
                log.debug("Failed to edit message after callback resolution", exc_info=True)
        else:
            # Waiter expired or already processed — still show user feedback
            # so button presses aren't silently swallowed.
            decision_label = "Approved" if action == "approve" else "Rejected"
            log.info(
                "Callback for expired/processed waiter %s: %s (user %s)",
                key, decision_label, user.id,
            )
            try:
                original = query.message.text_html or query.message.text or ""
                await query.edit_message_text(
                    text=f"{original}\n\n<b>{decision_label}</b> <i>(expired)</i>",
                    parse_mode="HTML",
                )
            except Exception:
                log.debug("Failed to edit expired waiter message", exc_info=True)


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
