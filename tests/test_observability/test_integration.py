"""Integration test: full event pipeline emit -> bus -> bridge -> send_fn."""

import pytest

from genesis.observability.az_bridge import NotificationBridge
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem


@pytest.mark.asyncio
async def test_full_pipeline():
    """Emit an event through the bus, verify the bridge calls send_fn."""
    from datetime import UTC, datetime

    notifications = []

    def mock_send(**kwargs):
        notifications.append(kwargs)

    bus = GenesisEventBus(clock=lambda: datetime(2026, 3, 4, tzinfo=UTC))
    bridge = NotificationBridge(send_fn=mock_send)
    bus.subscribe(bridge.handle_event, min_severity=Severity.WARNING)

    # INFO should not reach bridge
    await bus.emit(Subsystem.ROUTING, Severity.INFO, "info.event", "nothing")
    assert len(notifications) == 0

    # WARNING should reach bridge
    event = await bus.emit(
        Subsystem.ROUTING, Severity.WARNING, "breaker.tripped",
        "Provider X tripped after 3 failures",
        provider="openrouter",
    )
    assert len(notifications) == 1
    assert notifications[0]["type"] == "warning"
    assert "breaker.tripped" in notifications[0]["message"]
    assert event.event_type == "breaker.tripped"

    # ERROR should also reach bridge
    await bus.emit(
        Subsystem.SURPLUS, Severity.ERROR, "task.failed", "Brainstorm exploded"
    )
    assert len(notifications) == 2
    assert notifications[1]["type"] == "error"
    assert notifications[1]["priority"] == 20
