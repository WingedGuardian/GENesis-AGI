"""Tests for the Qdrant vitals section's honest degrade on a SQLite outage.

The embedding-throughput reads (pending_queue / embedded_24h) hit SQLite — a
DIFFERENT dependency than Qdrant reachability. On a query failure they must NOT
report literal ``0`` (indistinguishable from a real zero, a green-reading lie);
they degrade to ``None`` + a dedicated ``throughput_error``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.dashboard.routes.vitals import _build_qdrant_section


class _NoQdrant:
    """httpx.AsyncClient stand-in whose context entry fails, so the Qdrant HTTP
    block is skipped (its error is set) and the SQLite throughput reads — the unit
    under test, which run AFTER and independent of the Qdrant block — still run."""

    async def __aenter__(self):
        raise RuntimeError("no qdrant in test")

    async def __aexit__(self, *a):
        return False


def _rt(*, execute):
    rt = MagicMock()
    db = MagicMock()
    db.execute = execute
    rt.db = db
    return rt


@pytest.mark.asyncio
async def test_throughput_failure_is_none_not_zero():
    with patch("genesis.dashboard.routes.vitals.httpx.AsyncClient", return_value=_NoQdrant()):
        rt = _rt(execute=AsyncMock(side_effect=RuntimeError("db locked")))
        section = await _build_qdrant_section(rt)
    assert section["embedded_24h"] is None
    assert section["pending_queue"] is None
    assert section["throughput_error"]


@pytest.mark.asyncio
async def test_throughput_healthy_reports_counts():
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=[7])
    with patch("genesis.dashboard.routes.vitals.httpx.AsyncClient", return_value=_NoQdrant()):
        rt = _rt(execute=AsyncMock(return_value=cursor))
        section = await _build_qdrant_section(rt)
    assert section["embedded_24h"] == 7
    assert section["pending_queue"] == 7
    assert section["throughput_error"] is None
