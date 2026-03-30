"""Tests for NotificationBridge."""

import pytest

from genesis.observability.az_bridge import NotificationBridge
from genesis.observability.types import GenesisEvent, Severity, Subsystem


def _make_event(severity: Severity, event_type: str = "test") -> GenesisEvent:
    return GenesisEvent(
        subsystem=Subsystem.ROUTING,
        severity=severity,
        event_type=event_type,
        message="test message",
        timestamp="2026-03-04T00:00:00",
    )


class TestNotificationBridge:
    @pytest.mark.asyncio
    async def test_warning_forwarded(self):
        calls = []

        def mock_send(**kwargs):
            calls.append(kwargs)

        bridge = NotificationBridge(send_fn=mock_send)
        await bridge.handle_event(_make_event(Severity.WARNING))
        assert len(calls) == 1
        assert calls[0]["type"] == "warning"
        assert calls[0]["priority"] == 10
        assert "Genesis: routing" in calls[0]["title"]

    @pytest.mark.asyncio
    async def test_error_forwarded_high_priority(self):
        calls = []

        def mock_send(**kwargs):
            calls.append(kwargs)

        bridge = NotificationBridge(send_fn=mock_send)
        await bridge.handle_event(_make_event(Severity.ERROR))
        assert calls[0]["type"] == "error"
        assert calls[0]["priority"] == 20

    @pytest.mark.asyncio
    async def test_info_not_forwarded(self):
        calls = []

        def mock_send(**kwargs):
            calls.append(kwargs)

        bridge = NotificationBridge(send_fn=mock_send)
        await bridge.handle_event(_make_event(Severity.INFO))
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_send_fn_error_isolated(self):
        """Bridge must not raise if send_fn fails."""
        def bad_send(**kwargs):
            raise RuntimeError("AZ down")

        bridge = NotificationBridge(send_fn=bad_send)
        # Should not raise
        await bridge.handle_event(_make_event(Severity.ERROR))

    @pytest.mark.asyncio
    async def test_event_details_in_detail_field(self):
        calls = []

        def mock_send(**kwargs):
            calls.append(kwargs)

        bridge = NotificationBridge(send_fn=mock_send)
        event = GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.WARNING,
            event_type="breaker.tripped",
            message="X tripped",
            timestamp="2026-03-04T00:00:00",
            details={"provider": "openrouter"},
        )
        await bridge.handle_event(event)
        assert "openrouter" in calls[0]["detail"]
