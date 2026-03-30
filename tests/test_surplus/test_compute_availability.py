"""Tests for ComputeAvailability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.types import ComputeTier


@pytest.fixture
def clock():
    now = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: now


@pytest.fixture
def ca(clock):
    return ComputeAvailability(clock=clock, cache_ttl_s=60)


@pytest.mark.asyncio
async def test_free_api_always_available(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        tiers = await ca.get_available_tiers()
    assert ComputeTier.FREE_API in tiers


@pytest.mark.asyncio
async def test_lmstudio_available_when_ping_succeeds(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
        tiers = await ca.get_available_tiers()
    assert ComputeTier.LOCAL_30B in tiers


@pytest.mark.asyncio
async def test_lmstudio_unavailable_when_ping_fails(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        tiers = await ca.get_available_tiers()
    assert ComputeTier.LOCAL_30B not in tiers


@pytest.mark.asyncio
async def test_cache_prevents_repeated_pings(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=True) as mock_ping:
        await ca.get_available_tiers()
        await ca.get_available_tiers()
    assert mock_ping.call_count == 1


@pytest.mark.asyncio
async def test_cache_expires():
    t0 = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
    times = iter([t0, t0 + timedelta(seconds=61)])
    ca = ComputeAvailability(clock=lambda: next(times), cache_ttl_s=60)

    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=True) as mock_ping:
        await ca.get_available_tiers()
        await ca.get_available_tiers()
    assert mock_ping.call_count == 2


@pytest.mark.asyncio
async def test_check_lmstudio_directly(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
        assert await ca.check_lmstudio() is True
    # Reset cache
    ca._lmstudio_cached = None
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        assert await ca.check_lmstudio() is False


@pytest.mark.asyncio
async def test_never_tier_excluded(ca: ComputeAvailability):
    with patch.object(ca, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
        tiers = await ca.get_available_tiers()
    assert ComputeTier.NEVER not in tiers
    assert ComputeTier.CHEAP_PAID not in tiers
