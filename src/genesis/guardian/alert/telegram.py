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
        """Format and send alert as HTML message to Telegram."""
        text = self._format_alert(alert)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._send_message, text, "HTML",
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

        if alert.approval_url:
            parts.append(
                f'\n<a href="{html.escape(alert.approval_url)}">Click to approve recovery</a>'
            )

        return "\n".join(parts)

    def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram sendMessage API."""
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
                    return True
                logger.warning("Telegram API returned not ok: %s", result)
                return False
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
            return False
        except Exception as exc:
            logger.error("Telegram sendMessage failed: %s", exc, exc_info=True)
            return False
