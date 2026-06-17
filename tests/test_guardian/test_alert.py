"""Tests for Guardian alert interface, Telegram channel, and dispatcher."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.alert.base import Alert, AlertChannel, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel

# ── Alert Dataclass ─────────────────────────────────────────────────────


class TestAlert:

    def test_basic_alert(self) -> None:
        alert = Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down",
            body="Container not responding",
        )
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.title == "Genesis down"

    def test_alert_with_context(self) -> None:
        alert = Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down",
            body="Multiple probes failed",
            failed_probes=["container_exists", "icmp_reachable"],
            duration_s=120.0,
            likely_cause="Container crash",
            proposed_action="RESTART_CONTAINER",
            approval_url="http://100.1.2.3:8888/approve/abc123",
        )
        assert len(alert.failed_probes) == 2
        assert alert.approval_url is not None

    def test_severity_values(self) -> None:
        assert AlertSeverity.INFO == "info"
        assert AlertSeverity.WARNING == "warning"
        assert AlertSeverity.CRITICAL == "critical"
        assert AlertSeverity.EMERGENCY == "emergency"


# ── Telegram Channel ────────────────────────────────────────────────────


class TestTelegramChannel:

    @pytest.fixture
    def channel(self) -> TelegramAlertChannel:
        return TelegramAlertChannel(
            bot_token="test-token",
            chat_id="12345",
            thread_id="67890",
        )

    def test_format_critical_alert(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down",
            body="Container not responding",
            failed_probes=["container_exists", "icmp_reachable"],
            duration_s=120.0,
            likely_cause="OOM kill",
            proposed_action="RESTART_CONTAINER",
            approval_url="http://100.1.2.3:8888/approve/abc",
        )
        text = channel._format_alert(alert)
        assert "Guardian: Genesis down" in text
        assert "container_exists" in text
        assert "icmp_reachable" in text
        assert "2m" in text  # 120s = 2m
        assert "OOM kill" in text
        assert "RESTART_CONTAINER" in text
        # Approval is now a keyword-reply Telegram gate (host-side getUpdates),
        # not a clickable localhost link — the link must NOT be in the alert.
        assert "Click to approve" not in text

    def test_format_info_alert(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(
            severity=AlertSeverity.INFO,
            title="Recovery complete",
            body="Genesis is back online",
        )
        text = channel._format_alert(alert)
        assert "Recovery complete" in text
        assert "\u2705" in text  # ✅

    def test_format_duration_seconds(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(severity=AlertSeverity.WARNING, title="Test", body="", duration_s=45.0)
        text = channel._format_alert(alert)
        assert "45s" in text

    def test_format_duration_hours(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(severity=AlertSeverity.WARNING, title="Test", body="", duration_s=7200.0)
        text = channel._format_alert(alert)
        assert "2.0h" in text

    def test_html_escaping(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(
            severity=AlertSeverity.WARNING,
            title="<script>alert(1)</script>",
            body="body with <b>tags</b>",
        )
        text = channel._format_alert(alert)
        assert "&lt;script&gt;" in text
        assert "&lt;b&gt;tags&lt;/b&gt;" in text

    @pytest.mark.asyncio
    async def test_send_success(self, channel: TelegramAlertChannel) -> None:
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test body")

        with patch.object(channel, "_send_message", return_value=True):
            result = await channel.send(alert)
        assert result is True

    @pytest.mark.asyncio
    async def test_test_connectivity(self, channel: TelegramAlertChannel) -> None:
        import urllib.request

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true, "result": {"id": 123}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = await channel.test_connectivity()
        assert result is True


# ── Alert Dispatcher ────────────────────────────────────────────────────


class TestAlertDispatcher:

    @pytest.mark.asyncio
    async def test_dispatch_to_multiple_channels(self) -> None:
        ch1 = AsyncMock(spec=AlertChannel)
        ch1.send.return_value = True
        ch2 = AsyncMock(spec=AlertChannel)
        ch2.send.return_value = True

        dispatcher = AlertDispatcher([ch1, ch2])
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")

        result = await dispatcher.send(alert)
        assert result is True
        ch1.send.assert_called_once_with(alert)
        ch2.send.assert_called_once_with(alert)

    @pytest.mark.asyncio
    async def test_partial_failure_still_succeeds(self) -> None:
        ch1 = AsyncMock(spec=AlertChannel)
        ch1.send.return_value = False  # fails
        ch2 = AsyncMock(spec=AlertChannel)
        ch2.send.return_value = True   # succeeds

        dispatcher = AlertDispatcher([ch1, ch2])
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")

        result = await dispatcher.send(alert)
        assert result is True  # any channel succeeded

    @pytest.mark.asyncio
    async def test_all_channels_fail(self) -> None:
        ch1 = AsyncMock(spec=AlertChannel)
        ch1.send.return_value = False
        ch2 = AsyncMock(spec=AlertChannel)
        ch2.send.return_value = False

        dispatcher = AlertDispatcher([ch1, ch2])
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")

        result = await dispatcher.send(alert)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_channels_configured(self) -> None:
        dispatcher = AlertDispatcher()
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")

        result = await dispatcher.send(alert)
        assert result is False

    @pytest.mark.asyncio
    async def test_channel_exception_handled(self) -> None:
        ch1 = AsyncMock(spec=AlertChannel)
        ch1.send.side_effect = RuntimeError("boom")
        ch2 = AsyncMock(spec=AlertChannel)
        ch2.send.return_value = True

        dispatcher = AlertDispatcher([ch1, ch2])
        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")

        result = await dispatcher.send(alert)
        assert result is True  # ch2 still succeeded

    @pytest.mark.asyncio
    async def test_add_channel(self) -> None:
        dispatcher = AlertDispatcher()
        ch = AsyncMock(spec=AlertChannel)
        ch.send.return_value = True
        dispatcher.add_channel(ch)

        alert = Alert(severity=AlertSeverity.INFO, title="Test", body="Test")
        result = await dispatcher.send(alert)
        assert result is True

    @pytest.mark.asyncio
    async def test_test_all(self) -> None:
        ch1 = AsyncMock(spec=AlertChannel)
        ch1.test_connectivity.return_value = True

        dispatcher = AlertDispatcher([ch1])
        results = await dispatcher.test_all()
        assert "AsyncMock" in str(results) or len(results) == 1


# ── Keyword-reply recovery-approval gate ─────────────────────────────────


def _urlopen_returning(payload: dict) -> MagicMock:
    """Fake urlopen() context manager whose .read() yields `payload` as JSON."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestTelegramKeywordGate:
    """The recovery gate sends a reply-target prompt, then reads the keyword reply.

    Shared-token safety is the load-bearing property: the Guardian must NOT
    advance the getUpdates offset (that would consume updates the main bot needs)
    and must only honour replies to its OWN gate message.
    """

    @pytest.fixture
    def channel(self) -> TelegramAlertChannel:
        return TelegramAlertChannel(
            bot_token="test-token", chat_id="12345", thread_id="67890",
        )

    # --- _send_message returns the message_id (the reply target) ---

    def test_send_message_returns_message_id(self, channel: TelegramAlertChannel) -> None:
        resp = _urlopen_returning({"ok": True, "result": {"message_id": 4242}})
        with patch.object(urllib.request, "urlopen", return_value=resp):
            assert channel._send_message("hi") == 4242

    def test_send_message_returns_none_when_not_ok(
        self, channel: TelegramAlertChannel,
    ) -> None:
        resp = _urlopen_returning({"ok": False, "description": "bad request"})
        with patch.object(urllib.request, "urlopen", return_value=resp):
            assert channel._send_message("hi") is None

    # --- _poll_for_keyword_sync: reply-to filter + keyword match + 409 sentinel ---

    def test_poll_matches_keyword_reply_to_gate(
        self, channel: TelegramAlertChannel,
    ) -> None:
        """A keyword reply to OUR gate message is returned, upper-cased."""
        updates = {"ok": True, "result": [
            {"message": {"text": "approve", "reply_to_message": {"message_id": 100}}},
        ]}
        with patch.object(urllib.request, "urlopen", return_value=_urlopen_returning(updates)):
            kw = channel._poll_for_keyword_sync(100, frozenset({"APPROVE", "DENY"}))
        assert kw == "APPROVE"

    def test_poll_ignores_reply_to_other_message(
        self, channel: TelegramAlertChannel,
    ) -> None:
        """A keyword reply to a DIFFERENT message is not ours — ignore it."""
        updates = {"ok": True, "result": [
            {"message": {"text": "APPROVE", "reply_to_message": {"message_id": 999}}},
        ]}
        with patch.object(urllib.request, "urlopen", return_value=_urlopen_returning(updates)):
            kw = channel._poll_for_keyword_sync(100, frozenset({"APPROVE", "DENY"}))
        assert kw is None

    def test_poll_ignores_non_reply_keyword(
        self, channel: TelegramAlertChannel,
    ) -> None:
        """A bare keyword that is NOT a reply (e.g. someone chatting) is ignored."""
        updates = {"ok": True, "result": [{"message": {"text": "APPROVE"}}]}
        with patch.object(urllib.request, "urlopen", return_value=_urlopen_returning(updates)):
            kw = channel._poll_for_keyword_sync(100, frozenset({"APPROVE", "DENY"}))
        assert kw is None

    def test_poll_ignores_non_keyword_reply(
        self, channel: TelegramAlertChannel,
    ) -> None:
        """A reply to the gate that isn't an allowed keyword is ignored."""
        updates = {"ok": True, "result": [
            {"message": {"text": "maybe later", "reply_to_message": {"message_id": 100}}},
        ]}
        with patch.object(urllib.request, "urlopen", return_value=_urlopen_returning(updates)):
            kw = channel._poll_for_keyword_sync(100, frozenset({"APPROVE", "DENY"}))
        assert kw is None

    def test_poll_returns_conflict_sentinel_on_409(
        self, channel: TelegramAlertChannel,
    ) -> None:
        """HTTP 409 → CONFLICT_SENTINEL (main bot alive on same token), not None."""
        err = urllib.error.HTTPError("url", 409, "Conflict", {}, None)
        with patch.object(urllib.request, "urlopen", side_effect=err):
            kw = channel._poll_for_keyword_sync(100, frozenset({"APPROVE", "DENY"}))
        assert kw == CONFLICT_SENTINEL

    def test_poll_never_sends_offset(self, channel: TelegramAlertChannel) -> None:
        """SAFETY: with a shared token, advancing the offset would drop the main
        bot's updates. The gate must NEVER send an 'offset' to getUpdates."""
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _urlopen_returning({"ok": True, "result": []})

        with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
            channel._poll_for_keyword_sync(100, frozenset({"APPROVE"}))
        assert "offset" not in captured["body"]
        assert captured["body"]["allowed_updates"] == ["message"]
