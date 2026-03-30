"""Tests for health probes."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from genesis.observability.health import (
    probe_db,
    probe_disk,
    probe_ollama,
    probe_qdrant,
    probe_scheduler,
    probe_tmp,
)
from genesis.observability.types import ProbeStatus

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
        aiohttp_mock.get("http://localhost:11434/api/tags", status=200)
        result = await probe_ollama(clock=FROZEN_CLOCK)
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


class TestProbeTmp:
    @pytest.mark.asyncio
    async def test_healthy_low_usage(self):
        # 30% used: 300 of 1000 blocks used
        with patch("os.statvfs", return_value=_fake_statvfs(1000, 700)):
            result = await probe_tmp(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.HEALTHY
        assert result.name == "tmp_usage"
        assert result.details["pct_used"] == 30.0

    @pytest.mark.asyncio
    async def test_degraded_at_warn(self):
        # 85% used (between warn=80% and critical=90%)
        with patch("os.statvfs", return_value=_fake_statvfs(1000, 150)):
            result = await probe_tmp(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_down_at_critical(self):
        # 90% used
        with patch("os.statvfs", return_value=_fake_statvfs(1000, 100)):
            result = await probe_tmp(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert "/tmp" in result.message

    @pytest.mark.asyncio
    async def test_oserror_returns_down(self):
        with patch("os.statvfs", side_effect=OSError("no such")):
            result = await probe_tmp(clock=FROZEN_CLOCK)
        assert result.status == ProbeStatus.DOWN
        assert "Cannot stat" in result.message


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
