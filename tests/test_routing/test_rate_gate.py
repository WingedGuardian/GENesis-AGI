"""Tests for ProviderRateGate and RateGateRegistry."""

import asyncio
import time

import pytest

from genesis.routing.rate_gate import ProviderRateGate, RateGateRegistry


@pytest.mark.asyncio
async def test_rate_gate_first_call_no_wait():
    """First call should not wait."""
    gate = ProviderRateGate("test", rpm=30)
    t0 = time.monotonic()
    waited = await gate.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1
    assert waited == 0.0


@pytest.mark.asyncio
async def test_rate_gate_enforces_interval():
    """Second call within interval should wait."""
    gate = ProviderRateGate("test", rpm=60)  # 1 req/sec
    await gate.acquire()

    t0 = time.monotonic()
    await gate.acquire()
    elapsed = time.monotonic() - t0

    # Should have waited ~1 second (60 RPM = 1s interval)
    assert 0.8 < elapsed < 1.5


@pytest.mark.asyncio
async def test_rate_gate_no_wait_after_interval():
    """Call after interval has passed should not wait."""
    gate = ProviderRateGate("test", rpm=600)  # 0.1s interval
    await gate.acquire()
    await asyncio.sleep(0.15)

    t0 = time.monotonic()
    await gate.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_rate_gate_serializes_concurrent():
    """Concurrent calls should be serialized, not rejected."""
    gate = ProviderRateGate("test", rpm=120)  # 0.5s interval
    results = []

    async def call(idx):
        await gate.acquire()
        results.append((idx, time.monotonic()))

    t0 = time.monotonic()
    await asyncio.gather(call(0), call(1), call(2))

    # 3 calls at 0.5s interval = ~1.0s total
    total = results[-1][1] - t0
    assert total >= 0.8  # at least 2 intervals
    assert len(results) == 3


@pytest.mark.asyncio
async def test_registry_no_gate_returns_zero():
    """Provider without gate should return 0.0 wait."""
    reg = RateGateRegistry()
    waited = await reg.acquire("unknown_provider")
    assert waited == 0.0


@pytest.mark.asyncio
async def test_registry_with_gate():
    """Registered provider should use its gate."""
    reg = RateGateRegistry()
    reg.register("groq", rpm=60)

    assert reg.has_gate("groq")
    assert not reg.has_gate("other")
    assert len(reg) == 1

    waited = await reg.acquire("groq")
    assert waited == 0.0  # first call, no wait


@pytest.mark.asyncio
async def test_registry_respects_rpm():
    """Registry should enforce the registered RPM."""
    reg = RateGateRegistry()
    reg.register("slow", rpm=60)  # 1s interval

    await reg.acquire("slow")

    t0 = time.monotonic()
    await reg.acquire("slow")
    elapsed = time.monotonic() - t0

    assert 0.8 < elapsed < 1.5


def test_rate_gate_interval():
    """Interval calculation from RPM."""
    gate = ProviderRateGate("test", rpm=30)
    assert gate.interval == 2.0

    gate2 = ProviderRateGate("test", rpm=15)
    assert gate2.interval == 4.0


@pytest.mark.asyncio
async def test_try_acquire_first_call_true_no_block():
    """try_acquire grants the first request and never blocks."""
    gate = ProviderRateGate("test", rpm=3)  # 20s interval
    t0 = time.monotonic()
    assert await gate.try_acquire() is True
    assert time.monotonic() - t0 < 0.1


@pytest.mark.asyncio
async def test_try_acquire_denies_within_interval_instantly():
    """A second try_acquire inside the interval returns False IMMEDIATELY —
    unlike acquire(), it must not sleep ~20s at 3 RPM."""
    gate = ProviderRateGate("test", rpm=3)
    assert await gate.try_acquire() is True
    t0 = time.monotonic()
    assert await gate.try_acquire() is False
    assert time.monotonic() - t0 < 0.1  # returned instantly, did NOT block


@pytest.mark.asyncio
async def test_try_acquire_grants_after_interval():
    """Once the interval has elapsed, try_acquire grants again. Driven by
    rewinding _last_request so the test is wall-clock-independent (no real sleep)."""
    gate = ProviderRateGate("test", rpm=3)
    assert await gate.try_acquire() is True
    assert await gate.try_acquire() is False
    gate._last_request = time.monotonic() - gate.interval - 0.01
    assert await gate.try_acquire() is True


@pytest.mark.asyncio
async def test_try_acquire_denial_does_not_advance_window():
    """A denied try_acquire must NOT reserve the slot — otherwise repeated denials
    would push _last_request forward and could starve the caller indefinitely."""
    gate = ProviderRateGate("test", rpm=3)
    assert await gate.try_acquire() is True
    last = gate._last_request
    assert await gate.try_acquire() is False
    assert gate._last_request == last
