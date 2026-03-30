"""Tests for SystemStatusAggregator."""

from datetime import UTC, datetime

import pytest

from genesis.observability.status import SystemStatusAggregator
from genesis.observability.types import ProbeResult, ProbeStatus

FROZEN_CLOCK = lambda: datetime(2026, 3, 4, tzinfo=UTC)  # noqa: E731


async def healthy_probe():
    return ProbeResult(name="test", status=ProbeStatus.HEALTHY, latency_ms=1.0)


async def down_probe():
    return ProbeResult(name="broken", status=ProbeStatus.DOWN, latency_ms=0.0, message="dead")


async def exploding_probe():
    raise RuntimeError("probe crashed")


class TestSystemStatusAggregator:
    @pytest.mark.asyncio
    async def test_all_healthy(self):
        agg = SystemStatusAggregator(clock=FROZEN_CLOCK)
        agg.register_probe(healthy_probe)
        snap = await agg.snapshot()
        assert snap.overall_healthy is True
        assert len(snap.probes) == 1

    @pytest.mark.asyncio
    async def test_one_down(self):
        agg = SystemStatusAggregator(clock=FROZEN_CLOCK)
        agg.register_probe(healthy_probe)
        agg.register_probe(down_probe)
        snap = await agg.snapshot()
        assert snap.overall_healthy is False
        assert len(snap.probes) == 2

    @pytest.mark.asyncio
    async def test_probe_exception_handled(self):
        agg = SystemStatusAggregator(clock=FROZEN_CLOCK)
        agg.register_probe(exploding_probe)
        snap = await agg.snapshot()
        assert snap.overall_healthy is False
        assert snap.probes[0].status == ProbeStatus.DOWN
        assert "probe crashed" in snap.probes[0].message

    @pytest.mark.asyncio
    async def test_empty_snapshot(self):
        agg = SystemStatusAggregator(clock=FROZEN_CLOCK)
        snap = await agg.snapshot()
        assert snap.overall_healthy is True
        assert snap.probes == []

    @pytest.mark.asyncio
    async def test_probe_with_args(self):
        async def probe_with_arg(value):
            return ProbeResult(
                name=f"probe_{value}", status=ProbeStatus.HEALTHY, latency_ms=0.5
            )

        agg = SystemStatusAggregator(clock=FROZEN_CLOCK)
        agg.register_probe(probe_with_arg, "foo")
        snap = await agg.snapshot()
        assert snap.probes[0].name == "probe_foo"
