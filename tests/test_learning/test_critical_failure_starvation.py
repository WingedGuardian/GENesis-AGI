"""critical_failure must not fire 1.0 when a probe DOWN is loop-starvation-caused.

A health probe (Qdrant/Ollama) uses a 3s aiohttp timeout. When the event loop is
starved (background work blocking it), the probe coroutine can't complete inside
that budget and raises a timeout -> ProbeStatus.DOWN -> critical_failure=1.0, even
though the infra is fine. These tests pin the suppression: a *timeout*-caused DOWN
under a demonstrably unhealthy loop is reclassified to 0.0 (+ metadata), while a
real outage (hard-error DOWN, or a healthy loop, or absent loop-health evidence)
still fires 1.0.
"""

from __future__ import annotations

import time

import pytest

from genesis.learning.signals.critical_failure import CriticalFailureCollector
from genesis.observability.types import ProbeResult, ProbeStatus
from genesis.util import loop_health


def _probe(name: str, status: ProbeStatus, *, timed_out: bool = False):
    """A zero-arg callable returning a coroutine yielding a crafted ProbeResult."""

    async def _run() -> ProbeResult:
        return ProbeResult(name=name, status=status, latency_ms=1.0, timed_out=timed_out)

    return _run


def _sample(*, age_s: float, lagging: bool, drift_ms: float = 2600.0):
    """A LoopHealthSample whose age is `age_s` seconds (via sampled_monotonic)."""
    return loop_health.LoopHealthSample(
        drift_ms=drift_ms,
        peak_ms=drift_ms,
        lagging=lagging,
        threshold_ms=250.0,
        executor={"pending": 0, "workers": 11, "max_workers": 11},
        sampled_monotonic=time.monotonic() - age_s,
    )


@pytest.mark.asyncio
async def test_all_timeout_down_under_wedged_loop_suppressed(monkeypatch):
    # Wedged: the on-loop sampler stopped publishing (stale sample, age > threshold).
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=2.0, lagging=False))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 0.0
    assert r.metadata and r.metadata.get("starvation_suppressed") is True
    assert "qdrant" in r.metadata.get("suppressed_probes", "")


@pytest.mark.asyncio
async def test_fresh_lagging_sample_also_suppresses(monkeypatch):
    # WEDGED (stale age) is the primary discriminator, but a fresh sample still
    # flagged lagging within the ~0.5s window also counts as loop-unhealthy.
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=0.1, lagging=True))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 0.0


@pytest.mark.asyncio
async def test_timeout_down_healthy_loop_not_suppressed(monkeypatch):
    # Fresh sample, not lagging -> loop is healthy -> a real timeout still fires.
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=0.1, lagging=False))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_mixed_hard_and_timeout_down_not_suppressed(monkeypatch):
    # DB genuinely down (hard error) + Qdrant timing out from starvation: the real
    # DB outage must NOT be masked. Suppress only when ALL DOWN probes timed out.
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=2.0, lagging=True))
    c = CriticalFailureCollector(
        [
            _probe("db", ProbeStatus.DOWN, timed_out=False),
            _probe("qdrant", ProbeStatus.DOWN, timed_out=True),
        ]
    )
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_dead_publisher_stale_sample_not_suppressed(monkeypatch):
    # The lag sampler (the heartbeat publisher) died: loop_health.read() keeps
    # returning a once-healthy sample whose age climbs unbounded. That is absent
    # LIVE evidence, NOT proof the loop is wedged now -> a real timeout DOWN must
    # not be masked (fail-closed past the stale ceiling).
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=120.0, lagging=False))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_dead_publisher_stale_lagging_sample_not_suppressed(monkeypatch):
    # Symmetric to the lagging=False case: if the publisher died MID-episode, its
    # last sample carries lagging=True and its age climbs unbounded. A stale
    # lagging=True sample past the ceiling is still absent LIVE evidence -> a real
    # timeout DOWN must not be masked. (Both branches of the dead-publisher class.)
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=120.0, lagging=True))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_wedged_window_lagging_suppresses(monkeypatch):
    # Within the wedged window (fresh enough that the publisher is alive), a
    # lagging sample is live evidence of a current stall -> suppress.
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=2.0, lagging=True))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 0.0


@pytest.mark.asyncio
async def test_no_loop_sample_not_suppressed(monkeypatch):
    # Absent loop-health evidence -> fail-closed, never suppress.
    monkeypatch.setattr(loop_health, "read", lambda: None)
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_suppression_check_exception_keeps_value(monkeypatch):
    # B1: a bug anywhere in the suppression path must default to the un-suppressed
    # value (1.0) -- never let it escape to _safe_collect, which would return 0.0
    # and silently zero a real outage.
    def _boom():
        raise RuntimeError("loop_health blew up")

    monkeypatch.setattr(loop_health, "read", _boom)
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DOWN, timed_out=True)])
    r = await c.collect()
    assert r.value == 1.0


@pytest.mark.asyncio
async def test_degraded_unchanged(monkeypatch):
    monkeypatch.setattr(loop_health, "read", lambda: _sample(age_s=2.0, lagging=True))
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.DEGRADED)])
    r = await c.collect()
    assert r.value == 0.5


@pytest.mark.asyncio
async def test_all_healthy():
    c = CriticalFailureCollector([_probe("qdrant", ProbeStatus.HEALTHY)])
    r = await c.collect()
    assert r.value == 0.0
