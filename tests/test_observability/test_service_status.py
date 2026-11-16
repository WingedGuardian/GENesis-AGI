"""Tests for genesis.observability.service_status."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from genesis.observability.service_status import (
    _detect_genesis_service,
    _load_watchdog_state,
    collect_service_status,
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
            if "genesis-server" in unit or "genesis-bridge" in unit:
                return {
                    "ActiveState": "active",
                    "SubState": "running",
                    "NRestarts": "0",
                    "ExecMainStartTimestamp": "",
                }
            return {}

        with (
            patch("genesis.observability.service_status.query_systemd_unit", side_effect=mock_query),
            patch("genesis.observability.service_status._service_pid_alive", return_value=(12345, True)),
            patch("genesis.observability.service_status._detect_genesis_service",
                  return_value=("genesis-server.service", "Server")),
        ):
            result = collect_service_status()
        assert result["bridge"]["active_state"] == "active"
        assert result["bridge"]["pid"] == 12345
        assert result["bridge"]["pid_alive"] is True
        assert result["bridge"]["service_label"] == "Server"

    def _collect_with(self, tmp_path, unit, label, *, main_pid, locks):
        """Run collect_service_status against lock files in tmp_path.

        ``locks`` maps lock filename -> pid text written into it.
        """
        for name, content in locks.items():
            (tmp_path / name).write_text(content)

        def mock_query(u):
            if u == unit:
                return {
                    "ActiveState": "active", "SubState": "running",
                    "NRestarts": "0", "ExecMainStartTimestamp": "",
                    "MainPID": str(main_pid),
                }
            return {}

        with (
            patch("genesis.observability.service_status._PID_DIR", tmp_path),
            patch("genesis.observability.service_status.query_systemd_unit", side_effect=mock_query),
            patch("genesis.observability.service_status._detect_genesis_service",
                  return_value=(unit, label)),
        ):
            return collect_service_status()["bridge"]

    def test_server_unit_reads_server_lock(self, tmp_path):
        """#4071: under genesis-server the pid comes from genesis-server.lock."""
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=me, locks={"genesis-server.lock": str(me)},
        )
        assert result["pid"] == me
        assert result["pid_alive"] is True

    def test_bridge_unit_reads_bridge_lock(self, tmp_path):
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-bridge.service", "Bridge",
            main_pid=me, locks={"bridge.lock": str(me)},
        )
        assert result["pid"] == me
        assert result["pid_alive"] is True

    def test_server_unit_ignores_bridge_lock(self, tmp_path):
        """A leftover bridge.lock must not stand in for the server's own lock."""
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=me, locks={"bridge.lock": str(me)},
        )
        assert result["pid"] is None
        assert result["pid_alive"] is False

    def test_lock_pid_not_main_pid_is_not_alive(self, tmp_path):
        """A stale lock whose pid was reused by another live process."""
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=os.getppid(), locks={"genesis-server.lock": str(me)},
        )
        assert result["pid"] is None  # never show a pid that is not the unit's
        assert result["pid_alive"] is False

    def test_stopped_unit_main_pid_zero_is_not_alive(self, tmp_path):
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=0, locks={"genesis-server.lock": str(me)},
        )
        assert result["pid_alive"] is False

    def test_dead_lock_pid_is_not_alive(self, tmp_path):
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead = proc.pid
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=dead, locks={"genesis-server.lock": str(dead)},
        )
        assert result["pid"] is None  # a stale lock's pid is never reported
        assert result["pid_alive"] is False

    def test_non_integer_main_pid_is_not_alive(self, tmp_path):
        """An unparseable MainPID fails closed, never falls through to 'alive'."""
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid="[not set]", locks={"genesis-server.lock": str(me)},
        )
        assert result["pid_alive"] is False

    @pytest.mark.parametrize("content", ["0", "-1", "1"])
    def test_group_or_init_pid_in_lock_is_rejected(self, tmp_path, content):
        """kill(0|-1, 0) probes a process group, not one process; never 'alive'."""
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid="", locks={"genesis-server.lock": content},
        )
        assert result["pid"] is None
        assert result["pid_alive"] is False

    def test_unknown_main_pid_falls_back_to_existence(self, tmp_path):
        """systemctl query failed (no MainPID): only the existence check remains."""
        import os

        me = os.getpid()
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid="", locks={"genesis-server.lock": str(me)},
        )
        assert result["pid_alive"] is True

    def test_garbage_lock_is_not_alive(self, tmp_path):
        result = self._collect_with(
            tmp_path, "genesis-server.service", "Server",
            main_pid=1234, locks={"genesis-server.lock": "not-a-pid"},
        )
        assert result["pid"] is None
        assert result["pid_alive"] is False

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


class TestDetectGenesisService:
    """Direct tests for the service detector's install-preference logic."""

    @staticmethod
    def _mk(returncode=0, stdout=""):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def _side_effect(self, *, server_installed, relay_active):
        def run(cmd, **kw):
            if "list-unit-files" in cmd:  # _unit_installed(genesis-server.service)
                if server_installed:
                    return self._mk(0, "genesis-server.service disabled enabled\n")
                return self._mk(0, "")  # no matching unit file -> not installed
            if "is-active" in cmd:  # relay probe
                return self._mk(0, "active" if relay_active else "inactive")
            return self._mk(1, "")

        return run

    def test_server_installed_prefers_server_even_when_relay_enabled(self):
        # 2026-07-02 s7 edge: server present-but-disabled + relay enabled must
        # still resolve to the server -- recover/surface the main service, never
        # the deprecated relay.
        with patch(
            "genesis.observability.service_status.subprocess.run",
            side_effect=self._side_effect(server_installed=True, relay_active=True),
        ):
            assert _detect_genesis_service() == ("genesis-server.service", "Server")

    def test_server_absent_relay_active_returns_relay(self):
        with patch(
            "genesis.observability.service_status.subprocess.run",
            side_effect=self._side_effect(server_installed=False, relay_active=True),
        ):
            assert _detect_genesis_service() == ("genesis-bridge.service", "Bridge")

    def test_server_absent_relay_inactive_defaults_to_server(self):
        with patch(
            "genesis.observability.service_status.subprocess.run",
            side_effect=self._side_effect(server_installed=False, relay_active=False),
        ):
            assert _detect_genesis_service() == ("genesis-server.service", "Server")
