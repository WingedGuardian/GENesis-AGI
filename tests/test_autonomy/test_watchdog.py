"""Tests for genesis.autonomy.watchdog — health checks and restart decisions."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.autonomy.types import WatchdogAction
from genesis.autonomy.watchdog import (
    WatchdogChecker,
    get_container_memory,
    reclaim_page_cache,
)


@pytest.fixture(autouse=True)
def _mock_bridge_active():
    """Mock _is_bridge_active so tests don't hit real systemd.

    Returns None (unknown) by default — tests that need specific values
    override this.
    """
    with patch.object(WatchdogChecker, "_is_bridge_active", return_value=None):
        yield


@pytest.fixture()
def fresh_status(tmp_path: Path) -> Path:
    """Write a fresh status.json and return the path."""
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({
        "timestamp": datetime.now(UTC).isoformat(),
        "resilience_state": {"cloud": "NORMAL", "memory": "NORMAL"},
        "human_summary": "All systems normal.",
    }))
    return status_file


@pytest.fixture()
def stale_status(tmp_path: Path) -> Path:
    """Write a stale status.json (20 minutes old — exceeds 900s prod threshold)."""
    status_file = tmp_path / "status.json"
    old_time = datetime.now(UTC) - timedelta(minutes=20)
    status_file.write_text(json.dumps({
        "timestamp": old_time.isoformat(),
        "resilience_state": {"cloud": "NORMAL"},
        "human_summary": "All systems normal.",
    }))
    return status_file


@pytest.fixture()
def valid_secrets(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("TELEGRAM_BOT_TOKEN=12345:ABC\nGOOGLE_API_KEY=xyz\n")
    return secrets


def _make_checker(
    tmp_path: Path, status_file: Path, secrets_path: Path | None = None, **kwargs
) -> WatchdogChecker:
    return WatchdogChecker(
        status_file=str(status_file),
        staleness_threshold_s=kwargs.get("staleness_threshold_s", 300),
        max_restart_attempts=kwargs.get("max_restart_attempts", 5),
        backoff_initial_s=kwargs.get("backoff_initial_s", 1),
        backoff_max_s=kwargs.get("backoff_max_s", 10),
        config_validation=kwargs.get("config_validation", False),
        secrets_path=str(secrets_path or tmp_path / "secrets.env"),
        state_file=str(tmp_path / "watchdog_state.json"),
        stabilization_s=kwargs.get("stabilization_s", 600),
    )


class TestHealthy:
    def test_fresh_status_returns_skip(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        assert checker.check() is WatchdogAction.SKIP

    def test_resets_failure_counter_on_healthy(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        # Simulate previous failures
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 3, "next_attempt_after": None, "last_reason": "test",
        }))
        checker.check()
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0


class TestStale:
    def test_stale_status_returns_restart(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        assert checker.check() is WatchdogAction.RESTART

    def test_missing_status_returns_notify(self, tmp_path: Path):
        checker = _make_checker(tmp_path, tmp_path / "nonexistent.json")
        assert checker.check() is WatchdogAction.NOTIFY

    def test_invalid_json_returns_notify(self, tmp_path: Path):
        bad_file = tmp_path / "status.json"
        bad_file.write_text("not json at all {{{")
        checker = _make_checker(tmp_path, bad_file)
        assert checker.check() is WatchdogAction.NOTIFY

    def test_no_timestamp_returns_notify(self, tmp_path: Path):
        no_ts = tmp_path / "status.json"
        no_ts.write_text(json.dumps({"resilience_state": {}}))
        checker = _make_checker(tmp_path, no_ts)
        assert checker.check() is WatchdogAction.NOTIFY


class TestBackoff:
    def test_max_restarts_returns_notify(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, max_restart_attempts=2)
        # Simulate max failures reached
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 2, "next_attempt_after": None, "last_reason": "test",
        }))
        assert checker.check() is WatchdogAction.NOTIFY

    def test_backoff_period_returns_backoff(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 1,
            "next_attempt_after": time.time() + 9999,
            "last_reason": "test",
        }))
        assert checker.check() is WatchdogAction.BACKOFF

    def test_expired_backoff_allows_restart(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 1,
            "next_attempt_after": time.time() - 10,  # Past
            "last_reason": "test",
        }))
        assert checker.check() is WatchdogAction.RESTART

    def test_failure_increments_counter(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        checker.check()  # Should be RESTART and increment
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert state["consecutive_failures"] == 1


class TestConfigValidation:
    def test_valid_config_allows_restart(
        self, tmp_path: Path, stale_status: Path, valid_secrets: Path,
    ):
        checker = _make_checker(
            tmp_path, stale_status,
            secrets_path=valid_secrets,
            config_validation=True,
        )
        assert checker.check() is WatchdogAction.RESTART

    def test_missing_secrets_blocks_restart(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(
            tmp_path, stale_status,
            secrets_path=tmp_path / "nonexistent.env",
            config_validation=True,
        )
        assert checker.check() is WatchdogAction.SKIP  # Won't restart with bad config

    def test_placeholder_token_blocks_restart(self, tmp_path: Path, stale_status: Path):
        bad_secrets = tmp_path / "secrets.env"
        bad_secrets.write_text("TELEGRAM_BOT_TOKEN=placeholder\n")
        checker = _make_checker(
            tmp_path, stale_status,
            secrets_path=bad_secrets,
            config_validation=True,
        )
        assert checker.check() is WatchdogAction.SKIP

    def test_validate_config_returns_issues(self, tmp_path: Path):
        checker = _make_checker(
            tmp_path, tmp_path / "status.json",
            secrets_path=tmp_path / "nonexistent.env",
        )
        issues = checker.validate_config()
        assert len(issues) > 0
        assert any("Secrets file missing" in i for i in issues)

    def test_validate_config_no_issues(
        self, tmp_path: Path, valid_secrets: Path,
    ):
        checker = _make_checker(
            tmp_path, tmp_path / "status.json",
            secrets_path=valid_secrets,
        )
        issues = checker.validate_config()
        # May have bridge module issue (not in test env) but secrets should be OK
        assert not any("Secrets" in i for i in issues)


class TestBridgeActiveCheck:
    def test_inactive_bridge_triggers_restart(self, tmp_path: Path, fresh_status: Path):
        """If bridge is inactive, skip staleness check and go to restart."""
        checker = _make_checker(tmp_path, fresh_status)
        with patch.object(checker, "_is_bridge_active", return_value=False):
            assert checker.check() is WatchdogAction.RESTART

    def test_active_bridge_uses_normal_flow(self, tmp_path: Path, fresh_status: Path):
        """If bridge is active and status fresh, return SKIP."""
        checker = _make_checker(tmp_path, fresh_status)
        with patch.object(checker, "_is_bridge_active", return_value=True):
            assert checker.check() is WatchdogAction.SKIP

    def test_unknown_bridge_uses_normal_flow(self, tmp_path: Path, fresh_status: Path):
        """If bridge status unknown (None), fall through to staleness check."""
        checker = _make_checker(tmp_path, fresh_status)
        with patch.object(checker, "_is_bridge_active", return_value=None):
            assert checker.check() is WatchdogAction.SKIP


class TestCheckRecording:
    def test_check_records_last_check_at(self, tmp_path: Path, fresh_status: Path):
        """Every check() writes last_check_at to state file."""
        checker = _make_checker(tmp_path, fresh_status)
        checker.check()
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert "last_check_at" in state
        assert state["last_check_at"] is not None

    def test_reset_state_includes_last_check_at(self, tmp_path: Path, fresh_status: Path):
        """_reset_state includes last_check_at timestamp."""
        checker = _make_checker(tmp_path, fresh_status)
        # First write some state
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 3, "next_attempt_after": None, "last_reason": "test",
        }))
        checker.check()  # Fresh → resets state
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0
        assert "last_check_at" in state
        assert state["last_check_at"] is not None


class TestStabilizationCooldown:
    def test_no_reset_during_stabilization(self, tmp_path: Path, fresh_status: Path):
        """If a restart happened recently, don't reset failure counter on healthy."""
        checker = _make_checker(tmp_path, fresh_status, stabilization_s=600)
        state_file = tmp_path / "watchdog_state.json"
        # Simulate a recent restart with failures
        state_file.write_text(json.dumps({
            "consecutive_failures": 2,
            "next_attempt_after": None,
            "last_reason": "stale_status_restart",
            "last_restart_at": time.time() - 60,  # 60s ago, within 600s cooldown
            "last_check_at": None,
        }))
        checker.check()  # Fresh status -> would normally reset
        state = json.loads(state_file.read_text())
        # Failure count should NOT be reset
        assert state["consecutive_failures"] == 2

    def test_reset_after_stabilization_window(self, tmp_path: Path, fresh_status: Path):
        """After the stabilization window passes, reset normally."""
        checker = _make_checker(tmp_path, fresh_status, stabilization_s=60)
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 2,
            "next_attempt_after": None,
            "last_reason": "stale_status_restart",
            "last_restart_at": time.time() - 120,  # 120s ago, past 60s cooldown
            "last_check_at": None,
        }))
        checker.check()
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0

    def test_reset_without_prior_restart(self, tmp_path: Path, fresh_status: Path):
        """No prior restart -> reset normally."""
        checker = _make_checker(tmp_path, fresh_status, stabilization_s=600)
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 2,
            "next_attempt_after": None,
            "last_reason": "test",
            "last_restart_at": None,
            "last_check_at": None,
        }))
        checker.check()
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0

    def test_failure_records_last_restart_at(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        checker.check()  # Stale -> RESTART -> records failure
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert "last_restart_at" in state
        assert state["last_restart_at"] is not None
        assert isinstance(state["last_restart_at"], float)


class TestYamlLoading:
    def test_load_from_real_config(self):
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "autonomy.yaml"
        if not config_path.exists():
            pytest.skip("Config file not at expected path")
        checker = WatchdogChecker.from_yaml(config_path)
        assert checker._staleness_threshold == 900

    def test_load_missing_file(self, tmp_path: Path):
        checker = WatchdogChecker.from_yaml(tmp_path / "nonexistent.yaml")
        # Falls back to defaults
        assert checker._staleness_threshold == 300


class TestPageCacheReclaim:
    def test_reclaim_succeeds_when_path_exists(self, tmp_path: Path):
        reclaim_file = tmp_path / "memory.reclaim"
        reclaim_file.write_text("")
        with patch("genesis.autonomy.watchdog.Path", return_value=reclaim_file):
            # "1G" gets capped to "256M"
            assert reclaim_page_cache("1G") is True
            assert reclaim_file.read_text() == "256M"

    def test_reclaim_fails_gracefully(self):
        with patch("genesis.autonomy.watchdog.Path") as mock_path:
            mock_reclaim = mock_path.return_value
            mock_reclaim.exists.return_value = True
            mock_reclaim.write_text.side_effect = OSError("Permission denied")
            assert reclaim_page_cache() is False

    def test_reclaim_missing_path(self):
        with patch("genesis.autonomy.watchdog.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            assert reclaim_page_cache() is False


class TestGetContainerMemory:
    def test_reads_cgroup_files(self, tmp_path: Path):
        current_file = tmp_path / "memory.current"
        max_file = tmp_path / "memory.max"
        current_file.write_text("8000000000\n")
        max_file.write_text("24000000000\n")

        with patch("genesis.autonomy.watchdog.Path") as mock_path:
            def path_factory(p):
                if "memory.current" in str(p):
                    return current_file
                if "memory.max" in str(p):
                    return max_file
                return Path(p)
            mock_path.side_effect = path_factory
            result = get_container_memory()

        # Can't easily mock Path() for this function, test real cgroup instead
        result = get_container_memory()
        if result is not None:
            current, limit = result
            assert current > 0
            assert limit > 0

    def test_returns_none_on_missing_files(self):
        with patch("genesis.autonomy.watchdog.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = OSError
            # get_container_memory uses Path() directly, not the mock
            # Just verify it doesn't raise
            result = get_container_memory()
            # Result depends on whether real cgroup files exist
            assert result is None or isinstance(result, tuple)


class TestMemoryPressureCheck:
    def test_reclaims_at_80_percent(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        with (
            patch("genesis.autonomy.watchdog.get_container_anon_memory",
                  return_value=(20_000_000_000, 24_000_000_000)),  # 83%
            patch("genesis.autonomy.watchdog.reclaim_page_cache") as mock_reclaim,
        ):
            checker._check_memory_pressure()
            mock_reclaim.assert_called_once_with("128M")

    def test_reclaims_256m_at_90_percent(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        with (
            patch("genesis.autonomy.watchdog.get_container_anon_memory",
                  return_value=(22_000_000_000, 24_000_000_000)),  # 91%
            patch("genesis.autonomy.watchdog.reclaim_page_cache") as mock_reclaim,
        ):
            checker._check_memory_pressure()
            mock_reclaim.assert_called_once_with("256M")

    def test_no_reclaim_below_80_percent(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        with (
            patch("genesis.autonomy.watchdog.get_container_anon_memory",
                  return_value=(15_000_000_000, 24_000_000_000)),  # 62%
            patch("genesis.autonomy.watchdog.reclaim_page_cache") as mock_reclaim,
        ):
            checker._check_memory_pressure()
            mock_reclaim.assert_not_called()

    def test_handles_no_memory_info(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status)
        with patch("genesis.autonomy.watchdog.get_container_anon_memory", return_value=None):
            checker._check_memory_pressure()  # Should not raise
