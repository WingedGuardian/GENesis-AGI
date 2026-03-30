"""Tests for genesis.observability.host_detection."""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from unittest.mock import patch

from genesis.observability.host_detection.agent_zero import AgentZeroDetector
from genesis.observability.host_detection.registry import HostDetectorRegistry
from genesis.observability.host_detection.types import (
    HostDetector,
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
            name="Agent Zero",
            detected=True,
            status="healthy",
            uptime_seconds=120.0,
            restart_cmd="systemctl restart az",
            details={"port": 5000},
        )
        assert s.name == "Agent Zero"
        assert s.detected is True
        assert s.details["port"] == 5000


class TestProtocol:
    def test_detector_satisfies_protocol(self):
        assert isinstance(AgentZeroDetector(), HostDetector)


# ---------------------------------------------------------------------------
# AgentZeroDetector
# ---------------------------------------------------------------------------


def _az_patches(*, systemd=None, port=True, dir_exists=True):
    """Helper to build common AZ detector patches."""
    if systemd is None:
        systemd = {}
    return (
        patch(
            "genesis.observability.host_detection.agent_zero.query_systemd_unit",
            return_value=systemd,
        ),
        patch(
            "genesis.observability.host_detection.agent_zero._check_port",
            return_value=port,
        ),
        patch(
            "genesis.observability.host_detection.agent_zero._AZ_DIR",
            Path("/tmp") if dir_exists else Path("/nonexistent-dir-xyz"),
        ),
    )


class TestAgentZeroDetector:
    def test_all_signals_present(self):
        """3/3 signals -> detected, healthy."""
        p1, p2, p3 = _az_patches(
            systemd={
                "ActiveState": "active",
                "SubState": "running",
                "NRestarts": "1",
                "ExecMainStartTimestamp": "",
            },
            port=True,
            dir_exists=True,
        )
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.detected is True
        assert result.status == "healthy"
        assert result.name == "Agent Zero"
        assert result.restart_cmd is not None

    def test_two_signals_port_and_dir(self):
        """2/3 signals (port + dir, no systemd) -> detected, degraded."""
        p1, p2, p3 = _az_patches(systemd={}, port=True, dir_exists=True)
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.detected is True
        assert result.status == "degraded"

    def test_one_signal_not_detected(self):
        """1/3 signals -> NOT detected."""
        p1, p2, p3 = _az_patches(systemd={}, port=False, dir_exists=True)
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.detected is False

    def test_zero_signals_not_detected(self):
        """0/3 signals -> NOT detected."""
        p1, p2, p3 = _az_patches(systemd={}, port=False, dir_exists=False)
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.detected is False

    def test_systemd_active_plus_dir(self):
        """Systemd + dir (no port) -> detected, healthy."""
        p1, p2, p3 = _az_patches(
            systemd={"ActiveState": "active", "SubState": "running"},
            port=False,
            dir_exists=True,
        )
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.detected is True
        assert result.status == "healthy"

    def test_restart_count_in_details(self):
        """Restart count is extracted from systemd properties."""
        p1, p2, p3 = _az_patches(
            systemd={"ActiveState": "active", "NRestarts": "3"},
            port=True,
            dir_exists=True,
        )
        with p1, p2, p3:
            result = AgentZeroDetector().detect()
        assert result.details["restart_count"] == 3

    def test_priority(self):
        assert AgentZeroDetector().priority == 10

    def test_name(self):
        assert AgentZeroDetector().name == "Agent Zero"


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


# ---------------------------------------------------------------------------
# Integration: services snapshot
# ---------------------------------------------------------------------------


class TestServicesSnapshotIntegration:
    def test_host_framework_in_snapshot(self):
        """services() includes host_framework key."""
        svc_mod = importlib.import_module("genesis.observability.snapshots.services")

        p1, p2, p3 = _az_patches(
            systemd={"ActiveState": "active"},
            port=True,
            dir_exists=True,
        )
        with (
            patch("genesis.observability.service_status.query_systemd_unit", return_value={}),
            p1,
            p2,
            p3,
        ):
            svc_mod._registry = None
            try:
                result = svc_mod.services()
            finally:
                svc_mod._registry = None

        assert "host_framework" in result
        assert result["host_framework"]["detected"] is True
        assert result["host_framework"]["name"] == "Agent Zero"
        assert result["host_framework"]["has_restart"] is True
        assert "restart_cmd" not in result["host_framework"]
