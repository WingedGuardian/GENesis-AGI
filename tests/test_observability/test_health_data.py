"""Tests for HealthDataService — snapshot enrichments."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from genesis.observability.health_data import HealthDataService


@pytest.fixture
def mock_db():
    """Fake aiosqlite connection with row_factory."""
    db = AsyncMock()

    async def _execute(sql, params=None):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        cursor.fetchall = AsyncMock(return_value=[])
        return cursor

    db.execute = AsyncMock(side_effect=_execute)
    return db


class TestSnapshot:
    def test_snapshot_includes_awareness_key(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        assert "awareness" in snap

    def test_snapshot_includes_outreach_stats_key(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        assert "outreach_stats" in snap

    def test_queues_include_age_keys(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        queues = snap["queues"]
        assert "dead_letter_oldest_age_seconds" in queues
        assert "deferred_oldest_age_seconds" in queues

    def test_cc_sessions_include_duration_keys(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        cc = snap["cc_sessions"]
        assert "avg_duration_ms_24h" in cc
        assert "failed_24h" in cc

    def test_infrastructure_includes_disk(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        infra = snap["infrastructure"]
        assert "disk" in infra
        assert "free_gb" in infra["disk"]
        assert "free_pct" in infra["disk"]

    def test_snapshot_includes_services_key(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        assert "services" in snap

    def test_infrastructure_includes_tmpfs(self, mock_db):
        svc = HealthDataService(db=mock_db)
        snap = asyncio.run(svc.snapshot())
        infra = snap["infrastructure"]
        assert "tmpfs" in infra

    def test_no_db_returns_unknown(self):
        svc = HealthDataService()
        snap = asyncio.run(svc.snapshot())
        assert snap["awareness"]["status"] == "unknown"
        assert snap["outreach_stats"]["status"] == "unknown"
