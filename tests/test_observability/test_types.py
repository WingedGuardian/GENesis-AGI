"""Tests for observability type definitions."""

from genesis.observability.types import (
    GenesisEvent,
    ProbeResult,
    ProbeStatus,
    Severity,
    Subsystem,
    SubsystemStatus,
    SystemSnapshot,
)


class TestEnums:
    def test_severity_values(self):
        assert Severity.DEBUG == "debug"
        assert Severity.INFO == "info"
        assert Severity.WARNING == "warning"
        assert Severity.ERROR == "error"
        assert Severity.CRITICAL == "critical"

    def test_subsystem_values(self):
        assert Subsystem.ROUTING == "routing"
        assert Subsystem.AWARENESS == "awareness"
        assert Subsystem.SURPLUS == "surplus"
        assert Subsystem.MEMORY == "memory"
        assert Subsystem.HEALTH == "health"

    def test_probe_status_values(self):
        assert ProbeStatus.HEALTHY == "healthy"
        assert ProbeStatus.DEGRADED == "degraded"
        assert ProbeStatus.DOWN == "down"


class TestDataclasses:
    def test_genesis_event_frozen(self):
        event = GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.WARNING,
            event_type="breaker.tripped",
            message="Provider X tripped",
            timestamp="2026-03-04T00:00:00",
        )
        assert event.subsystem == Subsystem.ROUTING
        assert event.details == {}

    def test_genesis_event_with_details(self):
        event = GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.ERROR,
            event_type="all_exhausted",
            message="No providers",
            timestamp="2026-03-04T00:00:00",
            details={"call_site": "chat"},
        )
        assert event.details == {"call_site": "chat"}

    def test_probe_result(self):
        result = ProbeResult(
            name="qdrant", status=ProbeStatus.HEALTHY, latency_ms=5.2
        )
        assert result.name == "qdrant"
        assert result.message == ""

    def test_subsystem_status(self):
        status = SubsystemStatus(subsystem=Subsystem.ROUTING, healthy=True)
        assert status.healthy is True

    def test_system_snapshot(self):
        snap = SystemSnapshot(timestamp="2026-03-04T00:00:00")
        assert snap.overall_healthy is True
        assert snap.probes == []
        assert snap.subsystems == []
