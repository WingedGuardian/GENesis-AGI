"""Tests for genesis.observability.service_status."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from genesis.observability.service_status import (
    _load_watchdog_state,
    collect_service_status,
    collect_tmpfs_usage,
    compute_uptime_seconds,
    parse_systemd_timestamp,
    probe_qdrant_collections,
)


class TestParseSystemdTimestamp:
    def test_valid_timestamp(self):
        result = parse_systemd_timestamp("Mon 2026-03-16 16:30:26 UTC")
        assert result is not None
        assert "2026-03-16" in result

    def test_empty_string(self):
        assert parse_systemd_timestamp("") is None

    def test_na_string(self):
        assert parse_systemd_timestamp("n/a") is None

    def test_invalid_format(self):
        assert parse_systemd_timestamp("not-a-date") is None


class TestComputeUptimeSeconds:
    def test_none_input(self):
        assert compute_uptime_seconds(None) is None

    def test_valid_timestamp(self):
        from datetime import UTC, datetime, timedelta

        recent = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        result = compute_uptime_seconds(recent)
        assert result is not None
        assert 55 < result < 70  # ~60s with tolerance


class TestLoadWatchdogState:
    def test_missing_file(self, tmp_path):
        with patch("genesis.observability.service_status._WATCHDOG_STATE", tmp_path / "missing.json"):
            state = _load_watchdog_state()
        assert state["consecutive_failures"] == 0
        assert state["last_reason"] is None

    def test_valid_state_file(self, tmp_path):
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 3,
            "last_reason": "stale_status_restart",
            "next_attempt_after": time.time() + 60,
            "last_check_at": "2026-03-16T16:30:00+00:00",
        }))
        with patch("genesis.observability.service_status._WATCHDOG_STATE", state_file):
            state = _load_watchdog_state()
        assert state["consecutive_failures"] == 3
        assert state["last_reason"] == "stale_status_restart"
        assert state["last_check_at"] == "2026-03-16T16:30:00+00:00"

    def test_corrupt_json(self, tmp_path):
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text("{corrupt")
        with patch("genesis.observability.service_status._WATCHDOG_STATE", state_file):
            state = _load_watchdog_state()
        assert state["consecutive_failures"] == 0


class TestCollectServiceStatus:
    def test_returns_bridge_key(self):
        with patch("genesis.observability.service_status.query_systemd_unit", return_value={}):
            result = collect_service_status()
        assert "bridge" in result
        assert "watchdog_timer" in result
        assert "watchdog" in result

    def test_bridge_active(self):
        def mock_query(unit):
            if "bridge" in unit:
                return {
                    "ActiveState": "active",
                    "SubState": "running",
                    "NRestarts": "0",
                    "ExecMainStartTimestamp": "",
                }
            return {}

        with (
            patch("genesis.observability.service_status.query_systemd_unit", side_effect=mock_query),
            patch("genesis.observability.service_status._bridge_pid_alive", return_value=(12345, True)),
        ):
            result = collect_service_status()
        assert result["bridge"]["active_state"] == "active"
        assert result["bridge"]["pid"] == 12345
        assert result["bridge"]["pid_alive"] is True

    def test_watchdog_in_backoff(self, tmp_path):
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 2,
            "last_reason": "stale_status_restart",
            "next_attempt_after": time.time() + 300,
        }))
        with (
            patch("genesis.observability.service_status.query_systemd_unit", return_value={}),
            patch("genesis.observability.service_status._WATCHDOG_STATE", state_file),
        ):
            result = collect_service_status()
        assert result["watchdog"]["in_backoff"] is True
        assert result["watchdog"]["consecutive_failures"] == 2


class TestCollectTmpfsUsage:
    def test_returns_free_pct(self):
        result = collect_tmpfs_usage()
        assert "free_pct" in result
        assert isinstance(result["free_pct"], float)
        assert 0 <= result["free_pct"] <= 100


class TestProbeQdrantCollections:
    @pytest.mark.asyncio
    async def test_healthy_collections(self):
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "result": {
                "collections": [
                    {"name": "episodic_memory"},
                    {"name": "knowledge_base"},
                ]
            }
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_qdrant_collections()
        assert result["status"] == "healthy"
        assert result["missing"] == []

    @pytest.mark.asyncio
    async def test_missing_collection(self):
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "result": {
                "collections": [
                    {"name": "episodic_memory"},
                    # knowledge_base missing!
                ]
            }
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_qdrant_collections()
        assert result["status"] == "degraded"
        assert "knowledge_base" in result["missing"]

    @pytest.mark.asyncio
    async def test_connection_error(self):
        import aiohttp

        with patch("aiohttp.ClientSession", side_effect=aiohttp.ClientError("fail")):
            result = await probe_qdrant_collections()
        assert result["status"] == "error"
