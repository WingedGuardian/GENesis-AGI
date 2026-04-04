"""Genesis observability — agent_init wrapper.

Copies event bus from GenesisRuntime to self.agent and wires the
NotificationBridge to AZ's web UI (requires Agent object).
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger(__name__)


class GenesisObservability(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            bus = rt.event_bus
            if bus is None:
                return

            # Wire to agent namespace
            self.agent.genesis_event_bus = bus

            # Wire NotificationBridge to AZ's UI (requires Agent)
            try:
                from python.helpers.notification import (
                    NotificationManager,
                    NotificationPriority,
                    NotificationType,
                )

                from genesis.observability import NotificationBridge, Severity

                _TYPE_MAP = {
                    "warning": NotificationType.WARNING,
                    "error": NotificationType.ERROR,
                }
                _PRIORITY_MAP = {
                    10: NotificationPriority.NORMAL,
                    20: NotificationPriority.HIGH,
                }

                def send_fn(*, type, priority, message, title="", detail="", display_time=3, group=""):
                    az_type = _TYPE_MAP.get(type, NotificationType.WARNING)
                    az_priority = _PRIORITY_MAP.get(priority, NotificationPriority.NORMAL)
                    NotificationManager.send_notification(
                        az_type, az_priority, message, title, detail, display_time, group,
                    )

                bridge = NotificationBridge(send_fn=send_fn)
                bus.subscribe(bridge.handle_event, min_severity=Severity.WARNING)
                logger.info("NotificationBridge wired to AZ UI")
            except Exception:
                logger.warning("Could not wire NotificationBridge to AZ UI", exc_info=True)

            logger.info("Genesis observability wired to agent")

        except ImportError:
            logger.warning("Genesis observability not available")
        except Exception:
            logger.exception("Failed to wire Genesis observability")
