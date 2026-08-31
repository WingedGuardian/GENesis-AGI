"""probe_db is bounded by ``timeout_s`` (parity with probe_qdrant/probe_ollama).

A slow-but-not-down DB must not hang unbounded inside the critical_failure
gather, and a timeout must be tagged ``timed_out=True`` so the loop-starvation
suppression logic can evaluate it the same way as the HTTP probes.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from genesis.observability.health import probe_db
from genesis.observability.types import ProbeStatus

FROZEN_CLOCK = lambda: datetime(2026, 3, 4, tzinfo=UTC)  # noqa: E731


class _HangingCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchone(self):
        await asyncio.sleep(30)  # never returns within the probe's timeout


class _HangingDb:
    """A db whose query hangs — the slow-but-not-down case probe_db must bound."""

    def execute(self, *args, **kwargs):
        return _HangingCursor()


@pytest.mark.asyncio
async def test_probe_db_times_out_and_flags():
    result = await probe_db(_HangingDb(), timeout_s=0.05, clock=FROZEN_CLOCK)
    assert result.status == ProbeStatus.DOWN
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_probe_db_hard_error_not_flagged():
    bad = MagicMock()
    bad.execute = MagicMock(side_effect=RuntimeError("disk full"))
    result = await probe_db(bad, clock=FROZEN_CLOCK)
    assert result.status == ProbeStatus.DOWN
    assert result.timed_out is False
    assert "disk full" in result.message
