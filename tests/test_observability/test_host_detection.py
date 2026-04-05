"""Tests for genesis.observability.host_detection."""

from __future__ import annotations

import time

from genesis.observability.host_detection.registry import HostDetectorRegistry
from genesis.observability.host_detection.types import (
    HostFrameworkStatus,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class TestHostFrameworkStatus:
    def test_defaults(self):
        s = HostFrameworkStatus(name="test", detected=False)
        assert s.status == "unknown"
        assert s.version is None
        assert s.uptime_seconds is None
        assert s.pid is None
        assert s.restart_cmd is None
        assert s.details == {}

    def test_full_construction(self):
        s = HostFrameworkStatus(
            name="Test Framework",
            detected=True,
            status="healthy",
            uptime_seconds=120.0,
            restart_cmd="systemctl restart test",
            details={"port": 5000},
        )
        assert s.name == "Test Framework"
        assert s.detected is True
        assert s.details["port"] == 5000


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _FakeDetector:
    """Test detector that returns a configurable result."""

    def __init__(self, name: str, priority: int, detected: bool, status: str = "healthy"):
        self._name = name
        self._priority = priority
        self._detected = detected
        self._status = status

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def detect(self) -> HostFrameworkStatus:
        return HostFrameworkStatus(
            name=self._name,
            detected=self._detected,
            status=self._status,
        )


class TestHostDetectorRegistry:
    def test_priority_ordering(self):
        """Lower priority detectors are checked first."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        reg._detectors = [
            _FakeDetector("Second", 20, detected=True),
            _FakeDetector("First", 10, detected=True),
        ]
        reg._detectors.sort(key=lambda d: d.priority)

        result = reg.detect()
        assert result.name == "First"

    def test_fallback_to_second_detector(self):
        """If first detector doesn't detect, falls through to second."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        reg._detectors = [
            _FakeDetector("Primary", 10, detected=False),
            _FakeDetector("Fallback", 20, detected=True),
        ]

        result = reg.detect()
        assert result.name == "Fallback"
        assert result.detected is True

    def test_no_detectors_returns_standalone(self):
        """No detectors -> standalone fallback."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        reg._detectors = []

        result = reg.detect()
        assert result.name == "standalone"
        assert result.detected is False

    def test_none_detected_returns_standalone(self):
        """All detectors return not-detected -> standalone."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        reg._detectors = [
            _FakeDetector("A", 10, detected=False),
            _FakeDetector("B", 20, detected=False),
        ]

        result = reg.detect()
        assert result.name == "standalone"
        assert result.detected is False

    def test_caching(self):
        """Repeated calls within TTL return cached result."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        call_count = 0

        class CountingDetector:
            name = "Counter"
            priority = 10

            def detect(self_inner):
                nonlocal call_count
                call_count += 1
                return HostFrameworkStatus(name="Counter", detected=True, status="healthy")

        reg._detectors = [CountingDetector()]

        reg.detect()
        reg.detect()
        reg.detect()
        assert call_count == 1  # Cached after first call

    def test_cache_expiry(self):
        """Cache expires after TTL."""
        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = HostFrameworkStatus(name="Old", detected=True, status="healthy")
        reg._cached_at = time.monotonic() - 60  # Well past 30s TTL
        reg._detectors = [_FakeDetector("New", 10, detected=True)]

        result = reg.detect()
        assert result.name == "New"

    def test_detector_exception_is_caught(self):
        """A detector that raises is skipped gracefully."""

        class BrokenDetector:
            name = "Broken"
            priority = 5

            def detect(self_inner):
                raise RuntimeError("kaboom")

        reg = HostDetectorRegistry.__new__(HostDetectorRegistry)
        reg._cached = None
        reg._cached_at = 0.0
        reg._detectors = [
            BrokenDetector(),
            _FakeDetector("Healthy", 10, detected=True),
        ]

        result = reg.detect()
        assert result.name == "Healthy"
        assert result.detected is True
