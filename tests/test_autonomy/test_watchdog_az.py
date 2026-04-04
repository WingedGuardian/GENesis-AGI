"""Tests for Agent Zero watchdog monitoring."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from genesis.autonomy.types import WatchdogAction
from genesis.autonomy.watchdog import WatchdogChecker, restart_az


def _mock_enabled(returncode=0, stdout="enabled\n"):
    """Create a mock for 'systemctl --user is-enabled' calls."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


class TestAZHealthCheck:
    @pytest.fixture()
    def checker(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_enabled(returncode=1)):
            c = WatchdogChecker(
                status_file=tmp_path / "status.json",
                state_file=tmp_path / "watchdog_state.json",
            )
        # Default: AZ installed (tests can override)
        c._az_installed = True
        return c

    def test_az_active_no_action(self, checker):
        """When AZ is installed and active, _check_az_health does nothing."""
        mock_is_active = MagicMock()
        mock_is_active.stdout = "active\n"
        mock_is_active.returncode = 0

        with patch("subprocess.run", return_value=mock_is_active):
            checker._check_az_health()

    def test_az_inactive_triggers_restart(self, checker):
        """When AZ is installed but inactive, _check_az_health calls restart_az."""
        mock_is_active = MagicMock()
        mock_is_active.stdout = "inactive\n"
        mock_is_active.returncode = 3  # systemctl returns 3 for inactive

        # restart_az also calls is-enabled + restart
        mock_az_enabled = _mock_enabled(returncode=0)
        mock_restart = MagicMock()
        mock_restart.returncode = 0

        with patch("subprocess.run", side_effect=[
            mock_is_active,    # _check_az_health: is-active
            mock_az_enabled,   # restart_az: is-enabled
            mock_restart,      # restart_az: restart
        ]):
            checker._check_az_health()

    def test_az_backoff_respects_max_attempts(self, checker):
        """After max AZ restart attempts, stop trying."""
        az_state_path = checker._state_path.parent / "watchdog_az_state.json"
        az_state_path.parent.mkdir(parents=True, exist_ok=True)
        az_state_path.write_text(json.dumps({
            "consecutive_failures": 3,
            "next_attempt_after": None,
        }))

        mock_is_active = MagicMock()
        mock_is_active.stdout = "inactive\n"
        mock_is_active.returncode = 3

        with patch("subprocess.run", return_value=mock_is_active) as mock_run:
            checker._check_az_health()
            # Only is-active check, NOT restart (max attempts reached)
            assert mock_run.call_count == 1

    def test_az_not_installed_skips_check(self, checker):
        """When _az_installed is False (standalone mode), skip entirely."""
        checker._az_installed = False

        with patch("subprocess.run") as mock_run:
            checker._check_az_health()
            # No subprocess calls at all — cached flag prevents it
            assert mock_run.call_count == 0

    def test_az_unknown_status_no_action(self, checker):
        """When systemctl is-active check fails, don't act."""
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            checker._check_az_health()  # Should not raise


class TestRestartAZ:
    def test_success(self):
        mock_is_enabled = _mock_enabled(returncode=0)
        mock_restart = MagicMock()
        mock_restart.returncode = 0

        with patch("subprocess.run", side_effect=[mock_is_enabled, mock_restart]) as mock_run:
            rc = restart_az()
            assert rc == 0
            # Second call (restart) should have agent-zero.service
            assert "agent-zero.service" in mock_run.call_args_list[1][0][0]

    def test_failure(self):
        mock_is_enabled = _mock_enabled(returncode=0)
        mock_restart = MagicMock()
        mock_restart.returncode = 1
        mock_restart.stderr = "Failed"

        with patch("subprocess.run", side_effect=[mock_is_enabled, mock_restart]):
            rc = restart_az()
            assert rc == 1

    def test_timeout(self):
        import subprocess

        mock_is_enabled = _mock_enabled(returncode=0)

        with patch("subprocess.run", side_effect=[mock_is_enabled, subprocess.TimeoutExpired("cmd", 30)]):
            rc = restart_az()
            assert rc == -1

    def test_standalone_mode_skips(self):
        """When AZ is not enabled, restart_az returns 0 (no-op)."""
        mock_not_enabled = _mock_enabled(returncode=1)

        with patch("subprocess.run", return_value=mock_not_enabled) as mock_run:
            rc = restart_az()
            assert rc == 0
            assert mock_run.call_count == 1  # Only the is-enabled check


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
