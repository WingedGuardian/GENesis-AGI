"""Tests for Guardian diagnostic collector."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.guardian.collector import (
    CPUInfo,
    DiagnosticSnapshot,
    DiskInfo,
    IOInfo,
    MemoryInfo,
    ServiceInfo,
    _parse_pressure,
    collect_diagnostics,
)
from genesis.guardian.config import GuardianConfig


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


def _mock_incus(rc: int = 0, stdout: str = ""):
    async def mock(*args, **kwargs):
        return (rc, stdout)
    return mock


class TestParsePressure:

    def test_parse_full_line(self) -> None:
        text = "some avg10=1.23 avg60=4.56 avg300=7.89 total=100\nfull avg10=0.50 avg60=1.00 avg300=2.00 total=50"
        avg10, avg60 = _parse_pressure(text, "full")
        assert avg10 == 0.50
        assert avg60 == 1.00

    def test_parse_some_line(self) -> None:
        text = "some avg10=1.23 avg60=4.56 avg300=7.89 total=100\nfull avg10=0.50 avg60=1.00 avg300=2.00 total=50"
        avg10, avg60 = _parse_pressure(text, "some")
        assert avg10 == 1.23
        assert avg60 == 4.56

    def test_parse_missing_prefix(self) -> None:
        avg10, avg60 = _parse_pressure("nope", "full")
        assert avg10 == 0.0
        assert avg60 == 0.0


class TestDiagnosticSnapshot:

    def test_to_prompt_text_includes_all_sections(self) -> None:
        snap = DiagnosticSnapshot(
            collected_at="2026-03-25T12:00:00",
            container_status="Running",
            uptime="2 days",
            memory=MemoryInfo(
                current_bytes=12 * 1024**3,
                max_bytes=24 * 1024**3,
                usage_pct=50.0,
            ),
            cpu=CPUInfo(usage_usec=1000000),
            io=IOInfo(pressure_full_10s=0.5),
            disks=[DiskInfo(mount="/", total_mb=100000, used_mb=60000, avail_mb=40000, usage_pct=60.0)],
            services=[ServiceInfo(name="genesis-bridge", active=True, sub_state="running", n_restarts=0)],
            top_processes="PID USER ... python3",
            journal_recent="some log lines",
            error_count_1h=5,
            error_count_6h=20,
            git_last_commit="abc123 fix: something",
            git_uncommitted="",
            status_json='{"status": "ok"}',
            watchdog_state='{"state": "idle"}',
        )

        text = snap.to_prompt_text()
        assert "DIAGNOSTIC SNAPSHOT" in text
        assert "50.0%" in text  # memory
        assert "genesis-bridge" in text
        assert "ACTIVE" in text
        assert "abc123" in text
        assert "MEMORY" in text
        assert "CPU" in text
        assert "DISK" in text
        assert "STATUS.JSON" in text

    def test_empty_snapshot_renders(self) -> None:
        snap = DiagnosticSnapshot()
        text = snap.to_prompt_text()
        assert "DIAGNOSTIC SNAPSHOT" in text
        assert "(unavailable)" in text


class TestCollectDiagnostics:

    @pytest.mark.asyncio
    async def test_all_collectors_run(self, config: GuardianConfig) -> None:
        """Verify that collect_diagnostics runs all collectors and returns a snapshot."""
        with (
            patch("genesis.guardian.collector._collect_container_info", return_value=("Running", "2 days")),
            patch("genesis.guardian.collector._collect_processes", return_value=("top output", 0, 0)),
            patch("genesis.guardian.collector._collect_memory", return_value=MemoryInfo(current_bytes=1024, max_bytes=2048, usage_pct=50.0)),
            patch("genesis.guardian.collector._collect_io", return_value=IOInfo()),
            patch("genesis.guardian.collector._collect_cpu", return_value=CPUInfo()),
            patch("genesis.guardian.collector._collect_disk", return_value=[DiskInfo(mount="/", usage_pct=60.0)]),
            patch("genesis.guardian.collector._collect_services", return_value=[ServiceInfo(name="bridge", active=True)]),
            patch("genesis.guardian.collector._collect_journal", return_value=("log lines", 5, 20)),
            patch("genesis.guardian.collector._collect_git", return_value=("abc123 fix", "")),
            patch("genesis.guardian.collector._collect_status_files", return_value=('{"ok": true}', '{"idle": true}')),
        ):
            snap = await collect_diagnostics(config)

        assert snap.container_status == "Running"
        assert snap.memory.usage_pct == 50.0
        assert len(snap.disks) == 1
        assert len(snap.services) == 1
        assert snap.error_count_1h == 5
        assert snap.git_last_commit == "abc123 fix"

    @pytest.mark.asyncio
    async def test_collector_exception_handled(self, config: GuardianConfig) -> None:
        """If a collector raises, the snapshot still returns with defaults."""
        async def failing_collector(*args, **kwargs):
            raise RuntimeError("boom")

        with (
            patch("genesis.guardian.collector._collect_container_info", side_effect=RuntimeError("boom")),
            patch("genesis.guardian.collector._collect_processes", return_value=("", 0, 0)),
            patch("genesis.guardian.collector._collect_memory", return_value=MemoryInfo()),
            patch("genesis.guardian.collector._collect_io", return_value=IOInfo()),
            patch("genesis.guardian.collector._collect_cpu", return_value=CPUInfo()),
            patch("genesis.guardian.collector._collect_disk", return_value=[]),
            patch("genesis.guardian.collector._collect_services", return_value=[]),
            patch("genesis.guardian.collector._collect_journal", return_value=("", 0, 0)),
            patch("genesis.guardian.collector._collect_git", return_value=("", "")),
            patch("genesis.guardian.collector._collect_status_files", return_value=("", "")),
        ):
            snap = await collect_diagnostics(config)

        # Container info should be default since it raised
        assert snap.container_status == ""
        assert snap.collected_at != ""
