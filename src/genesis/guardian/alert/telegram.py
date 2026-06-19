"""Telegram alert channel — stdlib-only implementation.

Uses urllib.request to POST to Telegram Bot API. No python-telegram-bot
dependency. The Guardian must run with minimal dependencies (stdlib + pyyaml).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from genesis.guardian.alert.base import Alert, AlertChannel, AlertSeverity

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {
    AlertSeverity.INFO: "\u2705",        # ✅
    AlertSeverity.WARNING: "\u26a0\ufe0f",  # ⚠️
    AlertSeverity.CRITICAL: "\U0001f6a8",   # 🚨
    AlertSeverity.EMERGENCY: "\U0001f198",  # 🆘
}

_API_BASE = "https://api.telegram.org/bot{token}"

# Sentinel returned by _poll_for_keyword_sync when getUpdates reports a 409
# Conflict. A 409 means another consumer (the main Genesis bot) is actively
# long-polling the SAME bot token — i.e. the main bot is alive. The caller
# uses this to alert "cannot confirm Genesis is down" rather than treating it
# as "no reply yet".
CONFLICT_SENTINEL = "__CONFLICT__"


class TelegramAlertChannel(AlertChannel):
    """Send alerts via Telegram Bot API using stdlib urllib."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        thread_id: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._timeout = timeout_s
        self._api_base = _API_BASE.format(token=bot_token)

    async def send(self, alert: Alert) -> bool:
        """Format and send alert as HTML message to Telegram.

        AlertChannel interface contract: returns True on success. The
        underlying _send_message now returns a message_id (or None), so we
        coerce to bool here to preserve the interface.
        """
        text = self._format_alert(alert)
        loop = asyncio.get_running_loop()
        msg_id = await loop.run_in_executor(
            None, self._send_message, text, "HTML",
        )
        return msg_id is not None

    async def send_text(self, text: str) -> int | None:
        """Send a plain message and return its message_id (or None on failure).

        Used by the recovery-approval gate to send a prompt the user must
        REPLY to (the gate's message_id is the reply target). The text is
        sent with HTML parse mode so the existing bold/escape conventions
        work, matching `send`.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._send_message, text, "HTML",
        )

    async def poll_for_keyword(
        self,
        gate_message_id: int,
        keywords: frozenset[str],
        timeout_s: int = 25,
    ) -> str | None:
        """Async wrapper over _poll_for_keyword_sync (mirrors `send`).

        Returns the matched keyword (uppercased), CONFLICT_SENTINEL on a 409
        getUpdates conflict, or None if no matching reply arrived in this
        long-poll window.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._poll_for_keyword_sync, gate_message_id, keywords, timeout_s,
        )

    async def test_connectivity(self) -> bool:
        """Test Telegram API connectivity via getMe."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._test_connectivity_sync)

    def _test_connectivity_sync(self) -> bool:
        """Synchronous connectivity test."""
        try:
            url = f"{self._api_base}/getMe"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
                return data.get("ok", False)
        except Exception as exc:
            logger.warning("Telegram connectivity test failed: %s", exc)
            return False

    def _format_alert(self, alert: Alert) -> str:
        """Format alert as HTML for Telegram."""
        emoji = _SEVERITY_EMOJI.get(alert.severity, "")
        parts = [f"{emoji} <b>Guardian: {html.escape(alert.title)}</b>"]

        if alert.body:
            parts.append(f"\n{html.escape(alert.body)}")

        if alert.failed_probes:
            probes = ", ".join(html.escape(p) for p in alert.failed_probes)
            parts.append(f"\n<b>Failed probes:</b> {probes}")

        if alert.duration_s is not None:
            if alert.duration_s < 60:
                duration = f"{alert.duration_s:.0f}s"
            elif alert.duration_s < 3600:
                duration = f"{alert.duration_s / 60:.0f}m"
            else:
                duration = f"{alert.duration_s / 3600:.1f}h"
            parts.append(f"<b>Duration:</b> {duration}")

        if alert.likely_cause:
            parts.append(f"<b>Likely cause:</b> {html.escape(alert.likely_cause)}")

        if alert.proposed_action:
            parts.append(f"<b>Proposed action:</b> {html.escape(alert.proposed_action)}")

        return "\n".join(parts)

    def _send_message(self, text: str, parse_mode: str = "HTML") -> int | None:
        """Send a message via Telegram sendMessage API.

        Returns the sent message_id on success, or None on failure. The
        message_id is the reply target the keyword-approval gate polls for.
        """
        url = f"{self._api_base}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if self._thread_id:
            payload["message_thread_id"] = int(self._thread_id)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    msg_id = result.get("result", {}).get("message_id")
                    return int(msg_id) if msg_id is not None else None
                logger.warning("Telegram API returned not ok: %s", result)
                return None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "Telegram sendMessage failed: HTTP %d: %s", exc.code, body,
                exc_info=True,
            )
            # Retry without HTML on parse error
            if exc.code == 400 and "can't parse" in body.lower() and parse_mode == "HTML":
                logger.info("Retrying as plain text")
                return self._send_message(
                    text.replace("<b>", "").replace("</b>", "")
                        .replace("<a ", "").replace("</a>", ""),
                    parse_mode="",
                )
            return None
        except Exception as exc:
            logger.error("Telegram sendMessage failed: %s", exc, exc_info=True)
            return None

    def _poll_for_keyword_sync(
        self,
        gate_message_id: int,
        keywords: frozenset[str],
        timeout_s: int = 25,
    ) -> str | None:
        """Long-poll getUpdates for a keyword REPLY to the gate message.

        Reads updates via getUpdates with a server-side long-poll
        (``timeout``=timeout_s). Critically, NO ``offset`` is sent: the main
        Genesis bot shares this token and owns the global update offset.
        Advancing the offset here would consume/ack updates the main bot must
        see, dropping its messages. We instead filter on
        ``message.reply_to_message.message_id == gate_message_id`` to find
        only replies to our own gate prompt — which the main bot's handlers
        ignore — so we never need to ack.

        SHARED-TOKEN SAFETY (offset's sibling): ``allowed_updates`` is *also*
        sticky and bot-global, so we must NEVER narrow it here. Sending
        ["message"] once silently disabled the main bot's ``callback_query``
        (inline-button) delivery for ~5 days (#666 regression). We send an empty
        list, which Telegram treats as the default set (message + callback_query
        + …) — never narrowing it; our reply filter still ignores non-message
        updates.

        Returns:
            - the matched keyword (uppercased) if a reply matches,
            - CONFLICT_SENTINEL on HTTP 409 (main bot is long-polling the same
              token — proof it's alive),
            - None if no matching reply arrived in this window (caller loops).
        """
        url = f"{self._api_base}/getUpdates"
        payload: dict[str, Any] = {
            "timeout": timeout_s,
            # Empty list = Telegram's default update set (includes message AND
            # callback_query). Do NOT narrow this to ["message"]: allowed_updates
            # is sticky + bot-global and this token is shared with the main bot,
            # so narrowing it disables the main bot's inline-button callbacks (#666).
            "allowed_updates": [],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )

        # getUpdates blocks server-side up to timeout_s; give the socket a
        # margin beyond the long-poll window so a slow round-trip is not cut
        # short and misreported as "no reply".
        socket_timeout = max(self._timeout, timeout_s + 10)

        try:
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                result = json.loads(resp.read())
            if not result.get("ok"):
                logger.warning("Telegram getUpdates returned not ok: %s", result)
                return None
            for update in result.get("result", []):
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                reply_to = message.get("reply_to_message")
                if not isinstance(reply_to, dict):
                    continue
                if reply_to.get("message_id") != gate_message_id:
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                candidate = text.strip().upper()
                if candidate in keywords:
                    return candidate
            return None
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # Conflict: another getUpdates consumer (the main Genesis bot)
                # is polling the same token. The main bot being alive is itself
                # diagnostic — surface it to the caller, do not treat as silence.
                logger.warning(
                    "Telegram getUpdates 409 Conflict — main bot is polling "
                    "the same token (Genesis likely alive)",
                )
                return CONFLICT_SENTINEL
            body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "Telegram getUpdates failed: HTTP %d: %s", exc.code, body,
            )
            return None
        except Exception as exc:
            logger.error("Telegram getUpdates failed: %s", exc, exc_info=True)
            return None
