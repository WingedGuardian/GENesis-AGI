"""genesis.guardian.alert — Channel-agnostic alerting interface."""

from genesis.guardian.alert.base import Alert, AlertChannel, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher

__all__ = [
    "Alert",
    "AlertChannel",
    "AlertDispatcher",
    "AlertSeverity",
]
