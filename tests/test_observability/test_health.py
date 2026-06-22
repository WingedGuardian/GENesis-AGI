"""Tests for health probes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.observability.health import (
    probe_ambient_health,
    probe_db,
    probe_disk,
    probe_ollama,
    probe_qdrant,
    probe_scheduler,
    probe_wal,
)
from genesis.observability.types import ProbeResult, ProbeStatus

FROZEN_CLOCK = lambda: datetime(2026, 3, 4, tzinfo=UTC)  # noqa: E731


class TestProbeDb:
    @pytest.mark.asyncio
    async def test_healthy(self, db):
        result = await probe_db(db, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY
        assert result.name == "db"
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_down_on_error(self):
        bad_db = MagicMock()
        bad_db.execute = MagicMock(side_effect=RuntimeError("disk full"))
        result = await probe_db(bad_db, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert "disk full" in result.message


class TestProbeQdrant:
    @pytest.mark.asyncio
    async def test_healthy(self, aiohttp_mock, monkeypatch):
        monkeypatch.delenv("QDRANT_URL", raising=False)
        aiohttp_mock.get("http://localhost:6333/healthz", status=200)
        result = await probe_qdrant(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_degraded_on_non_200(self, aiohttp_mock):
        aiohttp_mock.get("http://localhost:6333/healthz", status=503)
        result = await probe_qdrant(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_down_on_connection_error(self):
        # Use an unreachable URL with very short timeout
        result = await probe_qdrant(
            url="http://192.0.2.1:1/healthz",  # RFC 5737 TEST-NET
            timeout_s=1,
            clock=FROZEN_CLOCK,
        )
        assert result.status == ProbeStatus.DOWN


class TestProbeOllama:
    @pytest.mark.asyncio
    async def test_healthy(self, aiohttp_mock, monkeypatch):
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        test_url = "http://localhost:11434/api/tags"
        aiohttp_mock.get(test_url, status=200)
        result = await probe_ollama(url=test_url, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_down(self):
        result = await probe_ollama(
            url="http://192.0.2.1:1/api/tags",
            timeout_s=1,
            clock=FROZEN_CLOCK,
        )
        assert result.status == ProbeStatus.DOWN


class TestProbeScheduler:
    @pytest.mark.asyncio
    async def test_running(self):
        scheduler = MagicMock()
        scheduler.running = True
        result = await probe_scheduler(scheduler, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_not_running(self):
        scheduler = MagicMock()
        scheduler.running = False
        result = await probe_scheduler(scheduler, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert "not running" in result.message

    @pytest.mark.asyncio
    async def test_error(self):
        scheduler = MagicMock()
        type(scheduler).running = property(lambda self: (_ for _ in ()).throw(RuntimeError("exploded")))
        result = await probe_scheduler(scheduler, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN


def _fake_statvfs(total_blocks, free_blocks, frsize=4096):
    """Create a fake statvfs result."""
    result = MagicMock()
    result.f_blocks = total_blocks
    result.f_bavail = free_blocks
    result.f_frsize = frsize
    return result


class TestProbeDisk:
    @pytest.mark.asyncio
    async def test_healthy(self):
        # 50% used
        with patch("os.statvfs", return_value=_fake_statvfs(1000000, 500000)):
            result = await probe_disk(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY
        assert result.name == "disk"
        assert result.details["pct_used"] == 50.0

    @pytest.mark.asyncio
    async def test_degraded_at_warn(self):
        # 85% used
        with patch("os.statvfs", return_value=_fake_statvfs(1000000, 150000)):
            result = await probe_disk(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_down_at_critical(self):
        # 95% used
        with patch("os.statvfs", return_value=_fake_statvfs(1000000, 50000)):
            result = await probe_disk(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert "free" in result.message.lower()

    @pytest.mark.asyncio
    async def test_oserror(self):
        with patch("os.statvfs", side_effect=OSError("read-only")):
            result = await probe_disk(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN


_MB = 1024 * 1024


def _make_wal(tmp_path, size_bytes):
    """Create a sparse <db>-wal file of the given size (near-zero real disk)."""
    wal = tmp_path / "genesis.db-wal"
    with open(wal, "wb") as f:
        f.truncate(size_bytes)
    return wal


class TestProbeWal:
    @pytest.mark.asyncio
    async def test_healthy_small(self, tmp_path):
        wal = _make_wal(tmp_path, 50 * _MB)
        result = await probe_wal(wal_path=wal, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY
        assert result.name == "wal"
        assert result.details["wal_mb"] == 50.0

    @pytest.mark.asyncio
    async def test_degraded_at_warn(self, tmp_path):
        wal = _make_wal(tmp_path, 150 * _MB)
        result = await probe_wal(wal_path=wal, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED
        assert result.details["wal_mb"] == 150.0

    @pytest.mark.asyncio
    async def test_down_at_critical(self, tmp_path):
        wal = _make_wal(tmp_path, 600 * _MB)
        result = await probe_wal(wal_path=wal, clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert result.details["wal_mb"] == 600.0
        assert "MB" in result.message

    @pytest.mark.asyncio
    async def test_missing_wal_is_healthy_zero(self, tmp_path):
        result = await probe_wal(
            wal_path=tmp_path / "nonexistent.db-wal", clock=FROZEN_CLOCK
        )
        assert result.status == ProbeStatus.HEALTHY
        assert result.details["wal_mb"] == 0.0


class TestInfrastructureWalPlumbing:
    """The WAL probe result must attach to the genesis.db entry in the
    infrastructure snapshot the dashboard health payload reads."""

    @pytest.mark.asyncio
    async def test_wal_attached_to_genesis_db(self, db):
        wal_probe = ProbeResult(
            name="wal",
            status=ProbeStatus.DEGRADED,
            latency_ms=0.1,
            details={"wal_mb": 150.0},
        )
        with patch(
            "genesis.observability.snapshots.infrastructure.probe_wal",
            new_callable=AsyncMock,
            return_value=wal_probe,
        ):
            from genesis.observability.snapshots.infrastructure import infrastructure

            infra = await infrastructure(
                db=db,
                routing_config=None,
                learning_scheduler=None,
                state_machine=None,
            )

        assert infra["genesis.db"]["wal_mb"] == 150.0
        assert infra["genesis.db"]["wal_status"] == "degraded"

    @pytest.mark.asyncio
    async def test_wal_not_attached_when_db_none(self):
        """No DB connection → no WAL readout (a green 0 MB next to a DB-error
        row would mislead operators)."""
        from genesis.observability.snapshots.infrastructure import infrastructure

        infra = await infrastructure(
            db=None,
            routing_config=None,
            learning_scheduler=None,
            state_machine=None,
        )

        assert "wal_mb" not in infra["genesis.db"]
        assert "wal_status" not in infra["genesis.db"]


class TestProbeAmbientHealth:
    """probe_ambient_health: maps the ambient evaluator's verdict to a
    ProbeResult for the observability surface (config + SSH read are mocked;
    the real, pure evaluate_ambient_health runs)."""

    _MOD = "genesis.observability.ambient_health"

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from genesis.observability import health

        health._ambient_ssh_cache.clear()
        yield
        health._ambient_ssh_cache.clear()

    def _cfg(self):
        from genesis.observability.ambient_health import AmbientRemoteConfig

        return AmbientRemoteConfig(host_ip="ambient-test-host", host_user="edge")

    @pytest.mark.asyncio
    async def test_not_configured_returns_none(self):
        # No ambient edge configured -> observability no-op (caller omits it).
        with patch(f"{self._MOD}.load_ambient_remote_config", return_value=None):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result is None

    @pytest.mark.asyncio
    async def test_misconfigured_config_is_degraded_not_silent(self):
        # Present-but-malformed config (loader raises) -> VISIBLE degraded card,
        # NOT a silent None that looks identical to "not configured".
        from genesis.observability.ambient_health import AmbientRemoteConfigError

        with patch(
            f"{self._MOD}.load_ambient_remote_config",
            side_effect=AmbientRemoteConfigError("missing host_ip/host_user"),
        ):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result is not None
        assert result.name == "ambient"
        assert result.status == ProbeStatus.DEGRADED
        assert result.details["verdict"] == "misconfigured"
        assert "misconfigured" in result.message

    @pytest.mark.asyncio
    async def test_healthy(self):
        snap = {"ts": FROZEN_CLOCK().isoformat(), "diar_enabled": True, "diar_worker_alive": True}
        with (
            patch(f"{self._MOD}.load_ambient_remote_config", return_value=self._cfg()),
            patch(f"{self._MOD}.read_edge_health", new_callable=AsyncMock, return_value=snap),
        ):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result is not None
        assert result.name == "ambient"
        assert result.status == ProbeStatus.HEALTHY
        assert result.details["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_stale_heartbeat_is_down(self):
        stale = (FROZEN_CLOCK() - timedelta(minutes=10)).isoformat()
        snap = {"ts": stale, "diar_enabled": True, "diar_worker_alive": True}
        with (
            patch(f"{self._MOD}.load_ambient_remote_config", return_value=self._cfg()),
            patch(f"{self._MOD}.read_edge_health", new_callable=AsyncMock, return_value=snap),
        ):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert result.details["verdict"] == "down"

    @pytest.mark.asyncio
    async def test_dead_diar_worker_is_degraded(self):
        snap = {"ts": FROZEN_CLOCK().isoformat(), "diar_enabled": True, "diar_worker_alive": False}
        with (
            patch(f"{self._MOD}.load_ambient_remote_config", return_value=self._cfg()),
            patch(f"{self._MOD}.read_edge_health", new_callable=AsyncMock, return_value=snap),
        ):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED
        assert result.details["verdict"] == "degraded"

    @pytest.mark.asyncio
    async def test_unreachable_edge_is_degraded_not_down(self):
        # Read failure -> verdict "unknown" -> DEGRADED: we can't confirm, which
        # is neither healthy nor a confirmed-dead bridge.
        with (
            patch(f"{self._MOD}.load_ambient_remote_config", return_value=self._cfg()),
            patch(f"{self._MOD}.read_edge_health", new_callable=AsyncMock, return_value=None),
        ):
            result = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED
        assert result.details["verdict"] == "unknown"

    @pytest.mark.asyncio
    async def test_ttl_cache_avoids_second_ssh(self):
        snap = {"ts": FROZEN_CLOCK().isoformat(), "diar_enabled": True, "diar_worker_alive": True}
        read_mock = AsyncMock(return_value=snap)
        with (
            patch(f"{self._MOD}.load_ambient_remote_config", return_value=self._cfg()),
            patch(f"{self._MOD}.read_edge_health", read_mock),
        ):
            first = await probe_ambient_health(clock=FROZEN_CLOCK)
            second = await probe_ambient_health(clock=FROZEN_CLOCK)
        assert first == second  # cached result returned
        assert read_mock.await_count == 1  # the real "no second SSH" guarantee


class TestInfrastructureAmbientPlumbing:
    """Ambient health must surface in the infrastructure snapshot (so it flows
    into health_status), and be ABSENT when no ambient edge is configured."""

    @pytest.mark.asyncio
    async def test_ambient_attached_when_configured(self):
        ambient_probe = ProbeResult(
            name="ambient",
            status=ProbeStatus.HEALTHY,
            latency_ms=1.2,
            message="healthy",
            details={"verdict": "ok"},
        )
        with patch(
            "genesis.observability.snapshots.infrastructure.probe_ambient_health",
            new_callable=AsyncMock,
            return_value=ambient_probe,
        ):
            from genesis.observability.snapshots.infrastructure import infrastructure

            infra = await infrastructure(
                db=None, routing_config=None, learning_scheduler=None, state_machine=None,
            )
        assert infra["ambient"]["status"] == "healthy"
        assert infra["ambient"]["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_ambient_absent_when_not_configured(self):
        with patch(
            "genesis.observability.snapshots.infrastructure.probe_ambient_health",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from genesis.observability.snapshots.infrastructure import infrastructure

            infra = await infrastructure(
                db=None, routing_config=None, learning_scheduler=None, state_machine=None,
            )
        assert "ambient" not in infra

    @pytest.mark.asyncio
    async def test_ambient_error_surfaced_when_probe_raises(self):
        # The probe normally swallows read failures (-> "unknown"), but the
        # snapshot's defensive guard must still surface an unexpected raise.
        with patch(
            "genesis.observability.snapshots.infrastructure.probe_ambient_health",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            from genesis.observability.snapshots.infrastructure import infrastructure

            infra = await infrastructure(
                db=None, routing_config=None, learning_scheduler=None, state_machine=None,
            )
        assert infra["ambient"]["status"] == "error"
        assert "boom" in infra["ambient"]["error"]
