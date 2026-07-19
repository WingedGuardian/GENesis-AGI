"""Idempotent send wrapper for Telegram API calls.

Wraps PTB bot methods with:
- Pre/post-send error classification for safe retry decisions
- Exponential backoff from network_config
- HTML formatting with plain-text fallback on BadRequest
- Thread fallback: if message_thread_not_found, strip thread_id and retry
- "Message is not modified" → treat as success

Reference: OpenClaw TS send.ts
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from genesis.channels.telegram.transport.network_config import calculate_retry_delay
from genesis.channels.telegram.transport.send_safety import (
    classify_send_error,
    is_safe_to_retry_send,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_TG_MAX_LEN = 4096


def _is_closed_client_error(exc: BaseException) -> bool:
    """True if ``exc`` signals PTB's httpx send-client was closed (shut down).

    PTB's ``HTTPXRequest.do_request`` raises
    ``RuntimeError("This HTTPXRequest is not initialized!")`` when the client
    is closed, which ``_request_wrapper`` rewraps as
    ``NetworkError("Unknown error in HTTP implementation: RuntimeError('This
    HTTPXRequest is not initialized!')") from exc``. The ``not initialized``
    signature therefore appears both on the outer ``NetworkError`` message and
    on its ``__cause__``/``__context__`` chain, so we walk the chain and match
    either — precise enough that ordinary network blips never trigger a rebuild,
    and resilient if a future PTB tweaks the wrapper text.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < 10 and id(cur) not in seen:
        seen.add(id(cur))
        if "not initialized" in str(cur).lower():
            return True
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return False


async def send_with_client_heal(
    bot: Bot,
    send: Callable[[], Awaitable[Any]],
    *,
    stopping: Callable[[], bool],
) -> Any:
    """Run a Telegram send, self-healing a shutdown-closed httpx client once.

    ``send`` is a zero-arg coroutine factory that performs the actual bot call
    (so it can be re-run cleanly on retry). On a closed-client error, if we are
    NOT shutting down (``stopping()`` is ``False``), rebuild the send client via
    ``bot.request.initialize()`` — idempotent, since ``HTTPXRequest.initialize``
    only rebuilds when the client ``is_closed`` — and retry the send exactly
    once. During shutdown we re-raise instead of resurrecting a client the
    process is deliberately tearing down.

    Background: on 2026-07-15 the send client was closed while background senders
    kept firing; every send raised the closed-client error, the outreach recovery
    worker exhausted its 5 retries against the dead client, and two Sentinel
    approval requests were permanently discarded (the owner never saw them). This
    recovers the client instead of failing permanently — whatever closed it.
    """
    try:
        return await send()
    except Exception as exc:
        if not _is_closed_client_error(exc) or stopping():
            raise
        logger.warning(
            "Telegram send client was closed; reinitializing and retrying once",
        )
        await bot.request.initialize()
        return await send()


async def safe_send_message(
    bot: Bot,
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    message_thread_id: int | None = None,
    reply_markup: Any = None,
    max_retries: int = _MAX_RETRIES,
) -> Any:
    """Send a message with safe retry logic.

    Automatically splits messages that exceed Telegram's 4096-character
    limit.  Returns the last sent Message object, or None if all attempts
    failed.  reply_markup is attached to the LAST chunk only.
    """
    if len(text) > _TG_MAX_LEN:
        # Split into chunks — use the existing code-block-aware splitter
        from genesis.channels.telegram._handler_helpers import _split_for_telegram

        chunks = _split_for_telegram(text, _TG_MAX_LEN)
        if not chunks:
            logger.error(
                "Message splitter returned empty list for %d-char message — "
                "sending truncated fallback", len(text),
            )
            chunks = [text[:_TG_MAX_LEN]]
        last_msg = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            last_msg = await safe_send_message(
                bot, chat_id, chunk,
                parse_mode=parse_mode,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup if is_last else None,
                max_retries=max_retries,
            )
        return last_msg

    for attempt in range(max_retries):
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            return await bot.send_message(**kwargs)

        except BadRequest as exc:
            err_msg = str(exc).lower()
            # Thread not found — retry without thread
            if "thread not found" in err_msg and message_thread_id is not None:
                logger.warning("Thread %s not found, retrying without thread", message_thread_id)
                message_thread_id = None
                continue
            # HTML parse error — retry as plain text
            if parse_mode and ("can't parse" in err_msg or "parse entities" in err_msg):
                logger.warning("HTML parse failed, retrying as plain text")
                parse_mode = None
                continue
            raise

        except RetryAfter as exc:
            logger.warning("Rate limited, waiting %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            continue

        except Exception as exc:
            classification = classify_send_error(exc)
            if attempt < max_retries - 1 and is_safe_to_retry_send(exc):
                delay = calculate_retry_delay(attempt)
                logger.warning(
                    "Send failed (%s), safe to retry. Attempt %d/%d, waiting %.1fs",
                    classification, attempt + 1, max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.error(
                "Send failed (%s), NOT safe to retry or max retries reached",
                classification, exc_info=True,
            )
            raise

    return None


async def safe_edit_message(
    bot: Bot,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    reply_markup: Any = None,
) -> Any:
    """Edit a message with safe error handling.

    "Message is not modified" is treated as success.
    Returns the edited Message object, or None.
    """
    try:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await bot.edit_message_text(**kwargs)

    except BadRequest as exc:
        err_msg = str(exc).lower()
        if "message is not modified" in err_msg:
            return None  # Success — content unchanged
        if parse_mode and ("can't parse" in err_msg or "parse entities" in err_msg):
            # Retry as plain text
            try:
                return await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except Exception:
                logger.warning("Edit plain-text fallback also failed", exc_info=True)
                return None
        raise


async def safe_send_document(
    bot: Bot,
    chat_id: int | str,
    document: Any,
    *,
    caption: str | None = None,
    message_thread_id: int | None = None,
    max_retries: int = _MAX_RETRIES,
) -> Any:
    """Send a document with safe retry logic."""
    for attempt in range(max_retries):
        # Reset stream position on retry (same BytesIO issue as safe_send_voice)
        if attempt > 0 and hasattr(document, "seek"):
            document.seek(0)
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "document": document,
            }
            if caption:
                kwargs["caption"] = caption
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            return await bot.send_document(**kwargs)

        except BadRequest as exc:
            err_msg = str(exc).lower()
            if "thread not found" in err_msg and message_thread_id is not None:
                logger.warning("Thread %s not found for document, retrying without thread", message_thread_id)
                message_thread_id = None
                continue
            raise

        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            continue

        except Exception as exc:
            if attempt < max_retries - 1 and is_safe_to_retry_send(exc):
                delay = calculate_retry_delay(attempt)
                await asyncio.sleep(delay)
                continue
            raise

    return None


async def safe_send_voice(
    bot: Bot,
    chat_id: int | str,
    voice: Any,
    *,
    reply_to_message_id: int | None = None,
    max_retries: int = _MAX_RETRIES,
) -> Any:
    """Send a voice message with safe retry logic.

    Returns the sent Message object, or None if all attempts failed.
    """
    for attempt in range(max_retries):
        # Reset stream position on retry — after first send attempt the
        # BytesIO cursor is at EOF, causing zero-byte uploads.
        if attempt > 0 and hasattr(voice, "seek"):
            voice.seek(0)
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "voice": voice,
            }
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
            return await bot.send_voice(**kwargs)

        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            continue

        except Exception as exc:
            if attempt < max_retries - 1 and is_safe_to_retry_send(exc):
                delay = calculate_retry_delay(attempt)
                await asyncio.sleep(delay)
                continue
            raise

    return None
