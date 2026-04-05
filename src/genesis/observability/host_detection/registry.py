"""Host framework detector registry.

Discovers and runs detectors in priority order. Caches the result to avoid
redundant subprocess calls on every dashboard poll.
"""

from __future__ import annotations

import logging
import time

from genesis.observability.host_detection.types import HostDetector, HostFrameworkStatus

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 30.0


class HostDetectorRegistry:
    """Registry that probes for the active host framework.

    Detectors are checked in priority order (lower first). The first detector
    that returns ``detected=True`` wins. Results are cached for 30 seconds.
    """

    def __init__(self) -> None:
        self._detectors: list[HostDetector] = []
        self._cached: HostFrameworkStatus | None = None
        self._cached_at: float = 0.0
        self._discover_detectors()

    def _discover_detectors(self) -> None:
        """Import and register all concrete detectors."""
        detectors: list[HostDetector] = []

        # Future detectors go here:
        # from genesis.observability.host_detection.openclaw import OpenClawDetector
        # detectors.append(OpenClawDetector())

        self._detectors = sorted(detectors, key=lambda d: d.priority)

    def detect(self) -> HostFrameworkStatus:
        """Return the detected host framework, using cache if fresh."""
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < _CACHE_TTL_S:
            return self._cached

        for detector in self._detectors:
            try:
                result = detector.detect()
                if result.detected:
                    self._cached = result
                    self._cached_at = now
                    return result
            except Exception:
                logger.warning(
                    "Host detector %s failed", detector.name, exc_info=True
                )

        # Nothing detected — standalone mode
        fallback = HostFrameworkStatus(
            name="standalone", detected=False, status="unknown"
        )
        self._cached = fallback
        self._cached_at = now
        return fallback
