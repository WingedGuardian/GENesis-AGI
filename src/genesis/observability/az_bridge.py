"""NotificationBridge — forwards Genesis events to Agent Zero's notification UI.

The bridge accepts a ``send_fn`` callable so that ``genesis`` never imports AZ code.
The AZ plugin extension passes ``NotificationManager.send_notification`` at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from genesis.observability.types import GenesisEvent, Severity

logger = logging.getLogger(__name__)

# send_fn signature matches NotificationManager.send_notification's positional args:
#   send_fn(type, priority, message, title, detail, display_time, group)
# But we pass keywords for clarity. The callable just needs to accept those kwargs.
SendFn = Callable[..., object]

# Map Genesis severity to AZ notification type string values
_SEVERITY_TO_AZ_TYPE = {
    Severity.WARNING: "warning",
    Severity.ERROR: "error",
    Severity.CRITICAL: "error",
}

_SEVERITY_TO_AZ_PRIORITY = {
    Severity.WARNING: 10,   # NORMAL
    Severity.ERROR: 20,     # HIGH
    Severity.CRITICAL: 20,  # HIGH
}


class NotificationBridge:
    """Listens to GenesisEventBus and forwards WARNING+ events to AZ UI.

    Usage::

        bridge = NotificationBridge(send_fn=NotificationManager.send_notification)
        event_bus.subscribe(bridge.handle_event, min_severity=Severity.WARNING)
    """

    def __init__(self, send_fn: SendFn):
        self._send_fn = send_fn

    async def handle_event(self, event: GenesisEvent) -> None:
        """Forward event to AZ notification system."""
        az_type_str = _SEVERITY_TO_AZ_TYPE.get(event.severity)
        if az_type_str is None:
            return  # Below WARNING, skip

        az_priority = _SEVERITY_TO_AZ_PRIORITY[event.severity]
        title = f"Genesis: {event.subsystem.value}"
        message = f"[{event.event_type}] {event.message}"
        detail = str(event.details) if event.details else ""

        try:
            self._send_fn(
                type=az_type_str,
                priority=az_priority,
                message=message,
                title=title,
                detail=detail,
                display_time=5,
                group=f"genesis.{event.subsystem.value}",
            )
        except Exception:
            logger.exception("Failed to send notification to AZ for event %s", event.event_type)
