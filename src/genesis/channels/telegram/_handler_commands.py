"""Command handlers for V2 Telegram handlers."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from genesis.cc.types import CCModel, ChannelType, EffortLevel
from genesis.db.crud import cc_sessions

if TYPE_CHECKING:
    from genesis.channels.telegram._handler_context import HandlerContext

log = logging.getLogger(__name__)


async def cmd_start(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not ctx.authorized(update.effective_user.id):
        return
    tts_line = "/tts [voice|text|match] — switch reply mode\n" if ctx.voice_helper else ""
    await update.message.reply_text(
        "Genesis online. Send me a message or voice note.\n\n"
        "Commands:\n"
        "/new — start a fresh session\n"
        "/stop — stop current generation\n"
        "/status — show current session info\n"
        "/model sonnet|opus|haiku — switch model\n"
        "/effort low|medium|high|xhigh|max — change thinking effort\n"
        "/pause [on|off] — pause/resume all background activity\n"
        f"{tts_line}"
    )


async def cmd_new(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    user_id = f"tg-{user.id}"
    tid = ctx.thread_id(update)
    session = await cc_sessions.get_active_foreground(
        ctx.loop._db, user_id=user_id, channel=str(ChannelType.TELEGRAM),
        thread_id=tid,
    )
    if session:
        await cc_sessions.update_status(ctx.loop._db, session["id"], status="completed")
        await update.message.reply_text("Session ended. Next message starts fresh.")
    else:
        await update.message.reply_text("No active session. Next message starts fresh.")


async def cmd_stop(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop current CC generation — sends SIGINT to subprocess."""
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return

    ikey = (user.id, update.effective_chat.id)
    interrupt_event = ctx.active_interrupts.get(ikey)
    if interrupt_event and not interrupt_event.is_set():
        interrupt_event.set()
        try:
            await ctx.loop.interrupt()
        except Exception:
            log.warning("Failed to send interrupt to invoker", exc_info=True)
        await update.message.reply_text("Stopping...")
    else:
        await update.message.reply_text("Nothing running to stop.")


async def cmd_model(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch model directly in session DB — no LLM involved."""
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /model sonnet|opus|haiku")
        return

    model_str = context.args[0].lower().strip()
    model_map = {
        "sonnet": CCModel.SONNET,
        "opus": CCModel.OPUS,
        "haiku": CCModel.HAIKU,
    }
    if model_str not in model_map:
        await update.message.reply_text(
            f"Unknown model '{model_str}'. Use: sonnet, opus, haiku"
        )
        return

    model = model_map[model_str]
    user_id = f"tg-{user.id}"
    tid = ctx.thread_id(update)
    session = await cc_sessions.get_active_foreground(
        ctx.loop._db, user_id=user_id, channel=str(ChannelType.TELEGRAM),
        thread_id=tid,
    )
    if session:
        await cc_sessions.update_model_effort(ctx.loop._db, session["id"], model=str(model))
        await update.message.reply_text(f"Model switched to {model_str}.")
    else:
        ctx.pending_settings.setdefault(user.id, {})["model"] = str(model)
        await update.message.reply_text(
            f"Model set to {model_str}. Will apply when session starts."
        )


async def cmd_effort(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch effort directly in session DB — no LLM involved."""
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /effort low|medium|high|xhigh|max")
        return

    effort_str = context.args[0].lower().strip()
    effort_map = {
        "low": EffortLevel.LOW,
        "medium": EffortLevel.MEDIUM,
        "high": EffortLevel.HIGH,
        "xhigh": EffortLevel.XHIGH,
        "max": EffortLevel.MAX,
    }
    if effort_str not in effort_map:
        await update.message.reply_text(
            f"Unknown effort '{effort_str}'. Use: low, medium, high, xhigh, max"
        )
        return

    effort = effort_map[effort_str]
    user_id = f"tg-{user.id}"
    tid = ctx.thread_id(update)
    session = await cc_sessions.get_active_foreground(
        ctx.loop._db, user_id=user_id, channel=str(ChannelType.TELEGRAM),
        thread_id=tid,
    )
    if session:
        await cc_sessions.update_model_effort(ctx.loop._db, session["id"], effort=str(effort))
        await update.message.reply_text(f"Effort switched to {effort_str}.")
    else:
        ctx.pending_settings.setdefault(user.id, {})["effort"] = str(effort)
        await update.message.reply_text(
            f"Effort set to {effort_str}. Will apply when session starts."
        )


async def cmd_status(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    user_id = f"tg-{user.id}"
    tid = ctx.thread_id(update)
    session = await cc_sessions.get_active_foreground(
        ctx.loop._db, user_id=user_id, channel=str(ChannelType.TELEGRAM),
        thread_id=tid,
    )
    if not session:
        await update.message.reply_text("No active session. Send a message to start one.")
        return

    model = session.get("model", "sonnet")
    effort = session.get("effort", "medium")
    started = session.get("started_at", "unknown")
    cc_sid = session.get("cc_session_id")
    lines = [
        f"Model: {model}",
        f"Thinking effort: {effort}",
        f"Started: {started}",
        f"CC session: {'active' if cc_sid else 'pending first response'}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_usage(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    user_id = f"tg-{user.id}"
    tid = ctx.thread_id(update)
    session = await cc_sessions.get_active_foreground(
        ctx.loop._db, user_id=user_id, channel=str(ChannelType.TELEGRAM),
        thread_id=tid,
    )
    if not session:
        await update.message.reply_text("No active session.")
        return

    lines = []
    rl_at = session.get("rate_limited_at")
    rl_resumes = session.get("rate_limit_resumes_at")
    if rl_at:
        lines.append(f"Rate limited at: {rl_at}")
        if rl_resumes:
            lines.append(f"Resumes at: {rl_resumes}")
    else:
        lines.append("No rate limits detected in current session.")
    model = session.get("model", "unknown")
    effort = session.get("effort", "unknown")
    lines.append(f"Model: {model}, Effort: {effort}")
    await update.message.reply_text("\n".join(lines))


async def cmd_tts(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return
    chat_id = update.effective_chat.id
    if not ctx.voice_helper:
        await update.message.reply_text("No TTS provider configured.")
        return
    args = (context.args[0].lower() if context.args else "").strip()
    current = ctx.chat_reply_mode.get(chat_id, "match")
    if args in ("voice", "text", "match"):
        new_mode = args
    else:
        new_mode = {"match": "voice", "voice": "text", "text": "match"}[current]
    ctx.chat_reply_mode[chat_id] = new_mode
    labels = {"match": "Match input", "voice": "Always voice", "text": "Always text"}
    await update.message.reply_text(f"Reply mode: {labels[new_mode]}")


async def cmd_pause(ctx: HandlerContext, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Genesis pause state. /pause [on|off] or just /pause to toggle."""
    user = update.effective_user
    if not user or not ctx.authorized(user.id):
        return

    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    args = context.args or []

    if not args:
        new_state = not rt.paused
    elif args[0].lower() in ("on", "1", "yes", "true"):
        new_state = True
    elif args[0].lower() in ("off", "0", "no", "false"):
        new_state = False
    else:
        await update.message.reply_text("Usage: /pause [on|off]")
        return

    reason = f"User {user.id} via Telegram /pause"
    rt.set_paused(new_state, reason)

    if new_state:
        await update.message.reply_text(
            "Genesis paused. All background activity stopped.\n"
            "Conversations still work but without background enrichment.\n"
            "/pause off to resume."
        )
    else:
        await update.message.reply_text(
            "Genesis resumed. Background activity restarting."
        )
