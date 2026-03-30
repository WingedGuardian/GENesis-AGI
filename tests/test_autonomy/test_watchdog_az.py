"""Tests for Agent Zero watchdog monitoring."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from genesis.autonomy.types import WatchdogAction
from genesis.autonomy.watchdog import WatchdogChecker, restart_az


class TestAZHealthCheck:
    @pytest.fixture()
    def checker(self, tmp_path):
        return WatchdogChecker(
            status_file=tmp_path / "status.json",
            state_file=tmp_path / "watchdog_state.json",
        )

    def test_az_active_no_action(self, checker):
        """When AZ is active, _check_az_health does nothing."""
        mock_result = MagicMock()
        mock_result.stdout = "active\n"

        with patch("subprocess.run", return_value=mock_result):
            # Should not raise or call restart
            checker._check_az_health()

    def test_az_inactive_triggers_restart(self, checker):
        """When AZ is inactive, _check_az_health calls restart_az."""
        mock_is_active = MagicMock()
        mock_is_active.stdout = "inactive\n"

        mock_restart = MagicMock()
        mock_restart.returncode = 0

        with (
            patch("subprocess.run", side_effect=[mock_is_active, mock_restart]),
        ):
            checker._check_az_health()

    def test_az_backoff_respects_max_attempts(self, checker):
        """After max AZ restart attempts, stop trying."""
        az_state_path = checker._state_path.parent / "watchdog_az_state.json"
        az_state_path.parent.mkdir(parents=True, exist_ok=True)
        az_state_path.write_text(json.dumps({
            "consecutive_failures": 3,
            "next_attempt_after": None,
        }))

        mock_result = MagicMock()
        mock_result.stdout = "inactive\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            checker._check_az_health()
            # Should only call is-active, not restart
            assert mock_run.call_count == 1

    def test_az_unknown_status_no_action(self, checker):
        """When systemctl check fails, don't act."""
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            checker._check_az_health()  # Should not raise


class TestRestartAZ:
    def test_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            rc = restart_az()
            assert rc == 0
            assert "agent-zero.service" in mock_run.call_args[0][0]

    def test_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Failed"

        with patch("subprocess.run", return_value=mock_result):
            rc = restart_az()
            assert rc == 1

    def test_timeout(self):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            rc = restart_az()
            assert rc == -1


class TestWatchdogActionEnum:
    def test_restart_az_value(self):
        assert WatchdogAction.RESTART_AZ == "restart_az"

    def test_all_values(self):
        values = {a.value for a in WatchdogAction}
        assert "restart" in values
        assert "restart_az" in values
        assert "skip" in values
        assert "notify" in values
        assert "backoff" in values
