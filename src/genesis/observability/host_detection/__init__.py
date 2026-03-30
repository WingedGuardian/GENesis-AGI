"""Host framework detection — USB-mode auto-discovery of the host Genesis runs inside."""

from genesis.observability.host_detection.registry import HostDetectorRegistry
from genesis.observability.host_detection.types import HostDetector, HostFrameworkStatus

__all__ = ["HostDetector", "HostDetectorRegistry", "HostFrameworkStatus"]
