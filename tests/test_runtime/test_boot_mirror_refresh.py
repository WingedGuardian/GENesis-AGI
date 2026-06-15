"""Tests for the best-effort reference-mirror boot refresh.

`init()` regenerates the human-readable reference mirror once at startup so
references added by non-triggering paths (extraction job, bulk imports) surface
after a restart. The refresh is best-effort: a failure is swallowed (the mirror
is a derived view, not a source of truth), but a mid-boot cancel must propagate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from genesis.runtime.init import memory as init_memory


@pytest.mark.asyncio
async def test_refresh_calls_regenerate(monkeypatch):
    """Happy path: the boot refresh forwards the db to regenerate_mirror."""
    regen = AsyncMock()
    # The helper imports regenerate_mirror at call time, so patch the source.
    monkeypatch.setattr("genesis.memory.reference_mirror.regenerate_mirror", regen)

    db = object()
    await init_memory._refresh_reference_mirror(db)

    regen.assert_awaited_once_with(db)


@pytest.mark.asyncio
async def test_refresh_swallows_exceptions(monkeypatch):
    """A regenerate failure must not crash boot — a stale mirror is acceptable."""
    regen = AsyncMock(side_effect=RuntimeError("db locked"))
    monkeypatch.setattr("genesis.memory.reference_mirror.regenerate_mirror", regen)

    await init_memory._refresh_reference_mirror(object())  # no raise


@pytest.mark.asyncio
async def test_refresh_propagates_cancellation(monkeypatch):
    """CancelledError is re-raised so a mid-boot SIGTERM isn't swallowed."""
    regen = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("genesis.memory.reference_mirror.regenerate_mirror", regen)

    with pytest.raises(asyncio.CancelledError):
        await init_memory._refresh_reference_mirror(object())
