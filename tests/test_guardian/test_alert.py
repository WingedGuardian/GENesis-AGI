"""Tests for Guardian alert interface, Telegram channel, and dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.alert.base import Alert, AlertChannel, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.alert.telegram import TelegramAlertChannel

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
        assert "Click to approve" in text

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
