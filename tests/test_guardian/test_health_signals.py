"""Tests for Guardian health signal probes.

All probes are tested with mocked subprocess calls — no real incus/ping/HTTP.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import (
    HealthSnapshot,
    PauseState,
    SignalResult,
    SuspiciousResult,
    _parse_psi_avg10,
    check_error_spike,
    check_health_api_depth,
    check_io_pressure,
    check_memory_pressure,
    check_pause_state,
    check_restart_count,
    check_tick_regularity,
    check_tmp_usage,
    collect_all_signals,
    probe_container_exists,
    probe_health_api,
    probe_heartbeat_canary,
    probe_icmp_reachable,
    probe_io_saturation,
    probe_log_freshness,
)


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


def _mock_subprocess(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Create a mock for _run_subprocess."""
    async def mock(*args, **kwargs):
        return (rc, stdout, stderr)
    return mock


def _mock_subprocess_sequence(results: list[tuple[int, str, str]]):
    """Create a mock for _run_subprocess that returns a different result per call.

    Raises IndexError if called more times than results provides.
    """
    calls = {"n": 0}

    async def mock(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return results[i]

    mock.call_count = lambda: calls["n"]  # type: ignore[attr-defined]
    return mock


def _mock_http(status: int = 200, body: str = "{}"):
    """Create a mock for _http_get_async."""
    async def mock(url, timeout=10.0):
        return (status, body)
    return mock


# ── Container Exists Probe ──────────────────────────────────────────────


class TestProbeContainerExists:

    @pytest.mark.asyncio
    async def test_running_container(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "Name: genesis\nStatus: RUNNING\nType: container"),
        ):
            result = await probe_container_exists(config)
        assert result.alive is True
        assert result.name == "container_exists"
        assert "running" in result.detail

    @pytest.mark.asyncio
    async def test_stopped_container(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "Name: genesis\nStatus: STOPPED\nType: container"),
        ):
            result = await probe_container_exists(config)
        assert result.alive is False
        assert "not running" in result.detail

    @pytest.mark.asyncio
    async def test_incus_error(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(1, "", "error: not found"),
        ):
            result = await probe_container_exists(config)
        assert result.alive is False
        assert "failed" in result.detail

    @pytest.mark.asyncio
    async def test_timeout(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(-1, "", "timeout"),
        ):
            result = await probe_container_exists(config)
        assert result.alive is False


# ── ICMP Probe ──────────────────────────────────────────────────────────


class TestProbeIcmpReachable:

    @pytest.mark.asyncio
    async def test_reachable(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "1 packet received"),
        ):
            result = await probe_icmp_reachable(config)
        assert result.alive is True
        assert result.name == "icmp_reachable"
        # First-call success must not be labelled "retry" — regression guard
        # for the retry-loop refactor.
        assert result.detail == "reachable"

    @pytest.mark.asyncio
    async def test_unreachable(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(1, "", "100% packet loss"),
        ):
            result = await probe_icmp_reachable(config)
        assert result.alive is False

    @pytest.mark.asyncio
    async def test_first_ping_fails_retry_succeeds(self, config: GuardianConfig) -> None:
        """Bridge ARP race — first packet drops, second arrives."""
        mock = _mock_subprocess_sequence([
            (1, "", "100% packet loss"),
            (0, "1 packet received", ""),
        ])
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            mock,
        ):
            result = await probe_icmp_reachable(config)
        assert result.alive is True
        assert "retry" in result.detail
        assert mock.call_count() == 2

    @pytest.mark.asyncio
    async def test_both_attempts_fail(self, config: GuardianConfig) -> None:
        """Two consecutive failures — container really is unreachable."""
        mock = _mock_subprocess_sequence([
            (1, "", "100% packet loss"),
            (1, "", "100% packet loss"),
        ])
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            mock,
        ):
            result = await probe_icmp_reachable(config)
        assert result.alive is False
        assert mock.call_count() == 2


# ── Health API Probe ────────────────────────────────────────────────────


class TestProbeHealthApi:

    @pytest.mark.asyncio
    async def test_healthy(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, '{"status": "healthy"}'),
        ):
            result = await probe_health_api(config)
        assert result.alive is True
        assert result.name == "health_api"

    @pytest.mark.asyncio
    async def test_unhealthy(self, config: GuardianConfig) -> None:
        with (
            patch(
                "genesis.guardian.health_signals._http_get_async",
                _mock_http(503, '{"status": "unhealthy"}'),
            ),
            patch("genesis.guardian.health_signals.asyncio.sleep", return_value=None),
        ):
            result = await probe_health_api(config)
        assert result.alive is False

    @pytest.mark.asyncio
    async def test_503_retry_succeeds(self, config: GuardianConfig) -> None:
        """503 followed by 200 should report alive=True."""
        call_count = [0]

        async def mock_retry(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return (503, '{"status": "unhealthy"}')
            return (200, '{"status": "healthy"}')

        with (
            patch("genesis.guardian.health_signals._http_get_async", mock_retry),
            patch("genesis.guardian.health_signals.asyncio.sleep", return_value=None),
        ):
            result = await probe_health_api(config)
        assert result.alive is True
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_connection_refused(self, config: GuardianConfig) -> None:
        async def failing_http(*args, **kwargs):
            raise ConnectionRefusedError("Connection refused")

        with patch(
            "genesis.guardian.health_signals._http_get_async",
            failing_http,
        ):
            result = await probe_health_api(config)
        assert result.alive is False
        assert "exception" in result.detail


# ── Heartbeat Canary Probe ──────────────────────────────────────────────


class TestProbeHeartbeatCanary:

    @pytest.mark.asyncio
    async def test_alive(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, '{"alive": true, "tick_count": 42}'),
        ):
            result = await probe_heartbeat_canary(config)
        assert result.alive is True
        assert "ticks=42" in result.detail

    @pytest.mark.asyncio
    async def test_bootstrapping(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(503, '{"alive": false, "reason": "not bootstrapped"}'),
        ):
            result = await probe_heartbeat_canary(config)
        assert result.alive is False
        assert "bootstrapping" in result.detail

    @pytest.mark.asyncio
    async def test_down(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(0, ""),
        ):
            result = await probe_heartbeat_canary(config)
        assert result.alive is False


# ── Log Freshness Probe ─────────────────────────────────────────────────


class TestProbeLogFreshness:

    @pytest.mark.asyncio
    async def test_fresh_logs(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "2026-03-25T12:00:00+0000 bridge: tick complete"),
        ):
            result = await probe_log_freshness(config)
        assert result.alive is True
        assert result.name == "log_freshness"

    @pytest.mark.asyncio
    async def test_no_logs(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, ""),
        ):
            result = await probe_log_freshness(config)
        assert result.alive is False
        assert "no journal" in result.detail

    @pytest.mark.asyncio
    async def test_journalctl_error(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(1, "", "Failed to get journal"),
        ):
            result = await probe_log_freshness(config)
        assert result.alive is False


# ── Suspicious Checks ───────────────────────────────────────────────────


class TestCheckTickRegularity:

    @pytest.mark.asyncio
    async def test_regular_ticks(self, config: GuardianConfig) -> None:
        # 5 ticks, 300s apart (5 min)
        ticks = "\n".join([
            "2026-03-25T12:20:00+00:00",
            "2026-03-25T12:15:00+00:00",
            "2026-03-25T12:10:00+00:00",
            "2026-03-25T12:05:00+00:00",
            "2026-03-25T12:00:00+00:00",
        ])
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, ticks),
        ):
            result = await check_tick_regularity(config)
        assert result.ok is True
        assert result.name == "tick_regularity"

    @pytest.mark.asyncio
    async def test_gap_detected(self, config: GuardianConfig) -> None:
        # 15 min gap between ticks (>600s threshold)
        ticks = "\n".join([
            "2026-03-25T12:20:00+00:00",
            "2026-03-25T12:05:00+00:00",  # 15 min gap
        ])
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, ticks),
        ):
            result = await check_tick_regularity(config)
        assert result.ok is False
        assert "gap=" in result.detail

    @pytest.mark.asyncio
    async def test_too_fast_detected(self, config: GuardianConfig) -> None:
        # 60s between ticks (<120s threshold)
        ticks = "\n".join([
            "2026-03-25T12:01:00+00:00",
            "2026-03-25T12:00:00+00:00",
        ])
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, ticks),
        ):
            result = await check_tick_regularity(config)
        assert result.ok is False
        assert "too fast" in result.detail


class TestCheckMemoryPressure:

    @pytest.mark.asyncio
    async def test_normal_memory(self, config: GuardianConfig) -> None:
        # 50% memory usage (anon + kernel)
        stat_output = "anon 10737418240\nfile 5368709120\nkernel 2147483648\n"
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            side_effect=[
                (0, stat_output, ""),         # memory.stat
                (0, str(24 * 1024**3), ""),   # 24 GiB max
            ],
        ):
            result = await check_memory_pressure(config)
        assert result.ok is True
        assert "50.0%" in result.detail

    @pytest.mark.asyncio
    async def test_high_memory(self, config: GuardianConfig) -> None:
        # 90% memory usage (anon + kernel)
        stat_output = "anon 21045339750\nfile 1073741824\nkernel 2147483648\n"
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            side_effect=[
                (0, stat_output, ""),         # memory.stat
                (0, str(24 * 1024**3), ""),   # 24 GiB max
            ],
        ):
            result = await check_memory_pressure(config)
        assert result.ok is False
        assert "90.0%" in result.detail

    @pytest.mark.asyncio
    async def test_no_limit(self, config: GuardianConfig) -> None:
        stat_output = f"anon {10 * 1024**3}\nkernel {2 * 1024**3}\n"
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            side_effect=[
                (0, stat_output, ""),
                (0, "max", ""),
            ],
        ):
            result = await check_memory_pressure(config)
        assert result.ok is True
        assert "no memory limit" in result.detail


class TestCheckTmpUsage:

    @pytest.mark.asyncio
    async def test_normal_tmp(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "Use%\n  30%"),
        ):
            result = await check_tmp_usage(config)
        assert result.ok is True
        assert "30%" in result.detail

    @pytest.mark.asyncio
    async def test_high_tmp(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "Use%\n  80%"),
        ):
            result = await check_tmp_usage(config)
        assert result.ok is False


class TestCheckRestartCount:

    @pytest.mark.asyncio
    async def test_no_restarts(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "0"),
        ):
            result = await check_restart_count(config)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_crash_loop(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "5"),
        ):
            result = await check_restart_count(config)
        assert result.ok is False
        assert "5 restarts" in result.detail


class TestCheckErrorSpike:

    @pytest.mark.asyncio
    async def test_normal_errors(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "3"),
        ):
            result = await check_error_spike(config)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_error_spike(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._run_subprocess",
            _mock_subprocess(0, "100"),
        ):
            result = await check_error_spike(config)
        assert result.ok is False


class TestCheckHealthApiDepth:

    @pytest.mark.asyncio
    async def test_all_healthy(self, config: GuardianConfig) -> None:
        body = '{"infrastructure": {"genesis.db": {"status": "healthy", "latency_ms": 5}, "scheduler": {"status": "healthy"}, "qdrant": {"status": "healthy"}}}'
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, body),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is True
        assert "all metrics healthy" in result.detail

    @pytest.mark.asyncio
    async def test_high_db_latency(self, config: GuardianConfig) -> None:
        body = '{"infrastructure": {"genesis.db": {"status": "healthy", "latency_ms": 8000}, "scheduler": {"status": "healthy"}, "qdrant": {"status": "healthy"}}}'
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, body),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is False
        assert "db_latency=8000ms" in result.detail

    @pytest.mark.asyncio
    async def test_degraded_scheduler(self, config: GuardianConfig) -> None:
        body = '{"infrastructure": {"genesis.db": {"status": "healthy", "latency_ms": 10}, "scheduler": {"status": "degraded"}, "qdrant": {"status": "healthy"}}}'
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, body),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is False
        assert "scheduler=degraded" in result.detail

    @pytest.mark.asyncio
    async def test_qdrant_down(self, config: GuardianConfig) -> None:
        body = '{"infrastructure": {"genesis.db": {"status": "healthy", "latency_ms": 10}, "scheduler": {"status": "healthy"}, "qdrant": {"status": "down"}}}'
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, body),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is False
        assert "qdrant=down" in result.detail

    @pytest.mark.asyncio
    async def test_multiple_warnings(self, config: GuardianConfig) -> None:
        body = '{"infrastructure": {"genesis.db": {"status": "healthy", "latency_ms": 9000}, "scheduler": {"status": "error"}, "qdrant": {"status": "down"}}}'
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, body),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is False
        assert "db_latency=9000ms" in result.detail
        assert "scheduler=error" in result.detail
        assert "qdrant=down" in result.detail

    @pytest.mark.asyncio
    async def test_non_200_skipped(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(503, '{"status": "unhealthy"}'),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is True
        assert "skipped" in result.detail

    @pytest.mark.asyncio
    async def test_malformed_body(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, "not json"),
        ):
            result = await check_health_api_depth(config)
        assert result.ok is True  # Errors fall safe


class TestCheckPauseState:

    @pytest.mark.asyncio
    async def test_not_paused(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, '{"paused": false, "reason": null, "since": null}'),
        ):
            result = await check_pause_state(config)
        assert result.paused is False

    @pytest.mark.asyncio
    async def test_paused(self, config: GuardianConfig) -> None:
        with patch(
            "genesis.guardian.health_signals._http_get_async",
            _mock_http(200, '{"paused": true, "reason": "testing", "since": "2026-03-25T12:00:00"}'),
        ):
            result = await check_pause_state(config)
        assert result.paused is True
        assert result.reason == "testing"

    @pytest.mark.asyncio
    async def test_api_fails_fallback_to_file(self, config: GuardianConfig) -> None:
        async def failing_http(*args, **kwargs):
            raise ConnectionRefusedError("down")

        with (
            patch("genesis.guardian.health_signals._http_get_async", failing_http),
            patch(
                "genesis.guardian.health_signals._run_subprocess",
                _mock_subprocess(0, '{"paused": true, "reason": "via file"}'),
            ),
        ):
            result = await check_pause_state(config)
        assert result.paused is True
        assert result.reason == "via file"


# ── I/O Saturation Probe ───────────────────────────────────────────────


class TestProbeIoSaturation:

    @pytest.mark.asyncio
    async def test_normal_io(self, config: GuardianConfig) -> None:
        psi_content = "some avg10=0.50 avg60=0.30 avg300=0.10 total=12345\nfull avg10=0.25 avg60=0.10 avg300=0.05 total=6789\n"
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: psi_content
            result = await probe_io_saturation(config)
        assert result.alive is True
        assert result.name == "io_saturation"
        assert "0.25%" in result.detail

    @pytest.mark.asyncio
    async def test_severe_io_stall(self, config: GuardianConfig) -> None:
        psi_content = "some avg10=60.00 avg60=40.00 avg300=20.00 total=12345\nfull avg10=55.00 avg60=30.00 avg300=15.00 total=6789\n"
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: psi_content
            result = await probe_io_saturation(config)
        assert result.alive is False
        assert "55.00%" in result.detail

    @pytest.mark.asyncio
    async def test_file_not_found(self, config: GuardianConfig) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = await probe_io_saturation(config)
        assert result.alive is True
        assert "not found" in result.detail


class TestCheckIoPressure:

    @pytest.mark.asyncio
    async def test_normal_io(self, config: GuardianConfig) -> None:
        psi_content = "some avg10=1.00 avg60=0.50 avg300=0.10 total=12345\nfull avg10=0.50 avg60=0.20 avg300=0.05 total=6789\n"
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: psi_content
            result = await check_io_pressure(config)
        assert result.ok is True
        assert "0.50%" in result.detail

    @pytest.mark.asyncio
    async def test_elevated_io(self, config: GuardianConfig) -> None:
        psi_content = "some avg10=20.00 avg60=15.00 avg300=10.00 total=12345\nfull avg10=15.00 avg60=10.00 avg300=5.00 total=6789\n"
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: psi_content
            result = await check_io_pressure(config)
        assert result.ok is False
        assert "15.00%" in result.detail

    @pytest.mark.asyncio
    async def test_file_not_found(self, config: GuardianConfig) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = await check_io_pressure(config)
        assert result.ok is True
        assert "not found" in result.detail


class TestParsePsiAvg10:

    def test_parse_full_line(self) -> None:
        content = "some avg10=1.50 avg60=0.50 avg300=0.10 total=12345\nfull avg10=0.25 avg60=0.10 avg300=0.05 total=6789\n"
        assert _parse_psi_avg10(content, "full") == 0.25
        assert _parse_psi_avg10(content, "some") == 1.50

    def test_missing_prefix(self) -> None:
        content = "some avg10=1.00 avg60=0.50 avg300=0.10 total=12345\n"
        assert _parse_psi_avg10(content, "full") is None

    def test_malformed_value(self) -> None:
        content = "full avg10=bad avg60=0.50 avg300=0.10 total=12345\n"
        assert _parse_psi_avg10(content, "full") is None

    def test_empty_content(self) -> None:
        assert _parse_psi_avg10("", "full") is None


# ── HealthSnapshot ──────────────────────────────────────────────────────


class TestHealthSnapshot:

    def test_all_alive(self) -> None:
        snapshot = HealthSnapshot(signals={
            "a": SignalResult("a", True, 1.0, "ok", "t"),
            "b": SignalResult("b", True, 2.0, "ok", "t"),
        })
        assert snapshot.all_alive is True
        assert snapshot.any_alive is True
        assert snapshot.failed_signals == []

    def test_partial_failure(self) -> None:
        snapshot = HealthSnapshot(signals={
            "a": SignalResult("a", True, 1.0, "ok", "t"),
            "b": SignalResult("b", False, 2.0, "down", "t"),
        })
        assert snapshot.all_alive is False
        assert snapshot.any_alive is True
        assert len(snapshot.failed_signals) == 1

    def test_all_dead(self) -> None:
        snapshot = HealthSnapshot(signals={
            "a": SignalResult("a", False, 1.0, "down", "t"),
            "b": SignalResult("b", False, 2.0, "down", "t"),
        })
        assert snapshot.all_alive is False
        assert snapshot.any_alive is False

    def test_suspicious_warnings(self) -> None:
        snapshot = HealthSnapshot(suspicious={
            "x": SuspiciousResult("x", True, "ok", "t"),
            "y": SuspiciousResult("y", False, "bad", "t"),
        })
        assert len(snapshot.suspicious_warnings) == 1
        assert snapshot.suspicious_warnings[0].name == "y"


# ── Collect All Signals ─────────────────────────────────────────────────


class TestCollectAllSignals:

    @pytest.mark.asyncio
    async def test_all_healthy(self, config: GuardianConfig) -> None:
        with (
            patch("genesis.guardian.health_signals.probe_container_exists", return_value=SignalResult("container_exists", True, 1.0, "running", "t")),
            patch("genesis.guardian.health_signals.probe_icmp_reachable", return_value=SignalResult("icmp_reachable", True, 1.0, "reachable", "t")),
            patch("genesis.guardian.health_signals.probe_health_api", return_value=SignalResult("health_api", True, 1.0, "healthy", "t")),
            patch("genesis.guardian.health_signals.probe_heartbeat_canary", return_value=SignalResult("heartbeat_canary", True, 1.0, "alive", "t")),
            patch("genesis.guardian.health_signals.probe_log_freshness", return_value=SignalResult("log_freshness", True, 1.0, "fresh", "t")),
            patch("genesis.guardian.health_signals.probe_io_saturation", return_value=SignalResult("io_saturation", True, 1.0, "io.pressure full avg10=0.00%", "t")),
            patch("genesis.guardian.health_signals.check_pause_state", return_value=PauseState(paused=False)),
            patch("genesis.guardian.health_signals.check_tick_regularity", return_value=SuspiciousResult("tick_regularity", True, "ok", "t")),
            patch("genesis.guardian.health_signals.check_memory_pressure", return_value=SuspiciousResult("memory_pressure", True, "ok", "t")),
            patch("genesis.guardian.health_signals.check_tmp_usage", return_value=SuspiciousResult("tmp_usage", True, "ok", "t")),
            patch("genesis.guardian.health_signals.check_restart_count", return_value=SuspiciousResult("restart_count", True, "ok", "t")),
            patch("genesis.guardian.health_signals.check_error_spike", return_value=SuspiciousResult("error_spike", True, "ok", "t")),
            patch("genesis.guardian.health_signals.check_io_pressure", return_value=SuspiciousResult("io_pressure", True, "io.pressure full avg10=0.00%", "t")),
            patch("genesis.guardian.health_signals.check_health_api_depth", return_value=SuspiciousResult("health_api_depth", True, "all metrics healthy", "t")),
        ):
            snapshot = await collect_all_signals(config)

        assert snapshot.all_alive is True
        assert len(snapshot.signals) == 6
        # Suspicious checks run when all alive (7 checks)
        assert len(snapshot.suspicious) == 7

    @pytest.mark.asyncio
    async def test_partial_failure_skips_suspicious(self, config: GuardianConfig) -> None:
        with (
            patch("genesis.guardian.health_signals.probe_container_exists", return_value=SignalResult("container_exists", False, 1.0, "down", "t")),
            patch("genesis.guardian.health_signals.probe_icmp_reachable", return_value=SignalResult("icmp_reachable", True, 1.0, "ok", "t")),
            patch("genesis.guardian.health_signals.probe_health_api", return_value=SignalResult("health_api", True, 1.0, "ok", "t")),
            patch("genesis.guardian.health_signals.probe_heartbeat_canary", return_value=SignalResult("heartbeat_canary", True, 1.0, "ok", "t")),
            patch("genesis.guardian.health_signals.probe_log_freshness", return_value=SignalResult("log_freshness", True, 1.0, "ok", "t")),
            patch("genesis.guardian.health_signals.probe_io_saturation", return_value=SignalResult("io_saturation", True, 1.0, "ok", "t")),
            patch("genesis.guardian.health_signals.check_pause_state", return_value=PauseState(paused=False)),
        ):
            snapshot = await collect_all_signals(config)

        assert snapshot.all_alive is False
        # Suspicious checks NOT run when any probe fails
        assert len(snapshot.suspicious) == 0
