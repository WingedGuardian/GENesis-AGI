"""Integration test: full event pipeline emit -> bus -> subscriber."""

import pytest

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem


@pytest.mark.asyncio
async def test_full_pipeline():
    """Emit an event through the bus, verify a subscriber callback receives it."""
    from datetime import UTC, datetime

    notifications = []

    async def mock_subscriber(event):
        notifications.append({
            "type": event.severity.name.lower(),
            "message": f"{event.event_type}: {event.message}",
            "priority": event.severity.value,
        })

    bus = GenesisEventBus(clock=lambda: datetime(2026, 3, 4, tzinfo=UTC))
    bus.subscribe(mock_subscriber, min_severity=Severity.WARNING)

    # INFO should not reach subscriber
    await bus.emit(Subsystem.ROUTING, Severity.INFO, "info.event", "nothing")
    assert len(notifications) == 0

    # WARNING should reach subscriber
    event = await bus.emit(
        Subsystem.ROUTING, Severity.WARNING, "breaker.tripped",
        "Provider X tripped after 3 failures",
        provider="openrouter",
    )
    assert len(notifications) == 1
    assert notifications[0]["type"] == "warning"
    assert "breaker.tripped" in notifications[0]["message"]
    assert event.event_type == "breaker.tripped"

    # ERROR should also reach subscriber
    await bus.emit(
        Subsystem.SURPLUS, Severity.ERROR, "task.failed", "Brainstorm exploded"
    )
    assert len(notifications) == 2
    assert notifications[1]["type"] == "error"
