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

    Returns the sent Message object, or None if all attempts failed.
    """
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
