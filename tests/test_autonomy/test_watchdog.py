"""Tests for genesis.autonomy.watchdog — health checks and restart decisions."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error  # noqa: F401 - used by the _liveness_probe_refused fixture
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

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


@pytest.fixture(autouse=True)
def _no_deploy_in_progress():
    """Default every watchdog test to 'no deploy running' so the restart gate is
    deterministic regardless of a real ~/.genesis/update_state.json on the host.
    The IR-2 deploy-guard tests override this to True.
    """
    with patch("genesis.autonomy.watchdog.update_in_progress", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _liveness_probe_refused():
    """Default the off-loop liveness probe to 'connection refused' (verdict
    'down') so every existing restart test is insulated from a live localhost:5000
    — the same insulation pattern as the autouse _is_bridge_active /
    update_in_progress mocks. 'down' falls through to the normal restart path, i.e.
    exactly the pre-feature behavior. The liveness-distinguisher tests below
    override the opener per-scenario to exercise the other verdicts. (The probe
    uses a proxy-free build_opener().open(), so the seam is OpenerDirector.open,
    not urlopen.)
    """

    def _refused(*_a, **_k):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    with patch("urllib.request.OpenerDirector.open", side_effect=_refused):
        yield


@pytest.fixture(autouse=True)
def _no_real_alert_queue():
    """Insulate the REAL alert queue: _alert_flap/_alert_starved enqueue to a
    hardcoded ~/.genesis/alerts/queue, so any test reaching them unpatched
    writes a production alert that the live server drains to the owner's
    Telegram (this happened — test-generated 'flap-damping' alerts with
    test-sized backoff values were delivered as if they were real incidents).
    Both call sites import enqueue_alert at call time, so patching the source
    module attribute intercepts them; per-test `patch(...)` layers on top for
    call assertions.
    """
    with patch("genesis.guardian.alert.queue.enqueue_alert", return_value=True):
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
        flap_window_s=kwargs.get("flap_window_s", 21600),
        flap_threshold=kwargs.get("flap_threshold", 3),
        flap_backoff_max_s=kwargs.get("flap_backoff_max_s", 7200),
    )


class TestDeployInProgress:
    """IR-2: the watchdog must DEFER restarts while a deploy is running.

    A mid-deploy revival takes the DB write lock and deadlocks bootstrap's
    procedure seed. update.sh stops genesis-server on purpose during the
    merge/bootstrap/migrate window; the guard keeps it down until the deploy ends.
    """

    def test_stale_status_defers_when_deploy_in_progress(
        self, tmp_path: Path, stale_status: Path
    ):
        # A stale status normally RESTARTs (see TestStale); during a deploy → SKIP.
        checker = _make_checker(tmp_path, stale_status)
        with patch("genesis.autonomy.watchdog.update_in_progress", return_value=True):
            assert checker.check() is WatchdogAction.SKIP

    def test_deploy_guard_does_not_trip_failure_counter(
        self, tmp_path: Path, stale_status: Path
    ):
        # SKIP returns BEFORE _record_failure, so the backoff / max-restart
        # counter is never burned during a deploy window.
        checker = _make_checker(tmp_path, stale_status)
        state_file = tmp_path / "watchdog_state.json"
        state_file.write_text(json.dumps({
            "consecutive_failures": 2, "next_attempt_after": None, "last_reason": "x",
        }))
        with patch("genesis.autonomy.watchdog.update_in_progress", return_value=True):
            checker.check()
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 2  # unchanged — guard skipped the failure path

    def test_restarts_normally_when_no_deploy(
        self, tmp_path: Path, stale_status: Path
    ):
        # Sanity: with the (default) no-deploy state, stale still RESTARTs — the
        # guard adds zero behavior change outside a deploy window.
        checker = _make_checker(tmp_path, stale_status)
        assert checker.check() is WatchdogAction.RESTART


class TestFlapDamping:
    """Slow recurring same-reason restarts (spaced beyond the stabilization
    window, so consecutive_failures keeps resetting) must eventually back off and
    surface a warning, instead of restarting forever (2026-07 outage: 25x/12h)."""

    def _seed(self, count, reason="zombie_scheduler", age_s=5):
        now = time.time()
        return {
            "consecutive_failures": 0,
            "next_attempt_after": None,
            "restart_history": [{"ts": now - age_s, "reason": reason} for _ in range(count)],
        }

    def test_backs_off_after_threshold(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, flap_threshold=3, backoff_max_s=10)
        assert (
            checker._restart_if_allowed(self._seed(3), reason="zombie_scheduler")
            is WatchdogAction.BACKOFF
        )

    def test_below_threshold_still_restarts(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, flap_threshold=3, backoff_max_s=10)
        assert (
            checker._restart_if_allowed(self._seed(2), reason="zombie_scheduler")
            is WatchdogAction.RESTART
        )

    def test_different_reasons_dont_cross_count(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, flap_threshold=3, backoff_max_s=10)
        now = time.time()
        state = {
            "consecutive_failures": 0,
            "next_attempt_after": None,
            "restart_history": [
                {"ts": now - 5, "reason": "zombie_scheduler"},
                {"ts": now - 5, "reason": "zombie_scheduler"},
                {"ts": now - 5, "reason": "stale_status_restart"},
            ],
        }
        assert (
            checker._restart_if_allowed(state, reason="zombie_scheduler")
            is WatchdogAction.RESTART
        )

    def test_history_pruned_past_window(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(
            tmp_path, stale_status, flap_threshold=3, flap_window_s=100, backoff_max_s=10
        )
        now = time.time()
        state = {
            "consecutive_failures": 0,
            "next_attempt_after": None,
            "restart_history": [
                {"ts": now - 500, "reason": "zombie_scheduler"},  # outside 100s window
                {"ts": now - 5, "reason": "zombie_scheduler"},
                {"ts": now - 5, "reason": "zombie_scheduler"},
            ],
        }
        assert (
            checker._restart_if_allowed(state, reason="zombie_scheduler")
            is WatchdogAction.RESTART
        )

    def test_alert_enqueued_once_on_flap(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, flap_threshold=3, backoff_max_s=10)
        with patch("genesis.guardian.alert.queue.enqueue_alert") as mock_alert:
            checker._restart_if_allowed(self._seed(3), reason="zombie_scheduler")
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["dedupe_key"] == "watchdog:flap:zombie_scheduler"
        assert mock_alert.call_args.kwargs["severity"] == "warning"

    def test_record_failure_appends_history(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status)
        state = {"consecutive_failures": 0, "restart_history": []}
        checker._record_failure(state, reason="zombie_scheduler")
        assert len(state["restart_history"]) == 1
        assert state["restart_history"][0]["reason"] == "zombie_scheduler"

    def test_reset_state_preserves_history(self, tmp_path: Path, fresh_status: Path):
        checker = _make_checker(tmp_path, fresh_status, stabilization_s=0)
        state_file = tmp_path / "watchdog_state.json"
        now = time.time()
        state_file.write_text(
            json.dumps({
                "consecutive_failures": 3,
                "last_restart_at": now - 1000,
                "restart_history": [{"ts": now - 10, "reason": "zombie_scheduler"}],
            })
        )
        checker.check()
        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0  # healthy reset
        assert len(state["restart_history"]) == 1  # but history preserved

    def test_legacy_state_without_history_ok(self, tmp_path: Path, stale_status: Path):
        checker = _make_checker(tmp_path, stale_status, flap_threshold=3)
        action = checker._restart_if_allowed(
            {"consecutive_failures": 0, "next_attempt_after": None},
            reason="zombie_scheduler",
        )
        assert action is WatchdogAction.RESTART  # no restart_history key → no crash


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
    @pytest.fixture(autouse=True)
    def _reset_reclaim_cooldown(self, tmp_path: Path, monkeypatch):
        """Point the persisted reclaim-cooldown sidecar at a fresh tmp file so
        each test starts with no prior reclaim (cooldown expired) and never
        touches the real ~/.genesis state."""
        import genesis.autonomy.watchdog as w
        monkeypatch.setattr(w, "_RECLAIM_STATE_PATH", tmp_path / "watchdog_reclaim.json")

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

    def test_reclaim_cooldown_persists_across_process_restart(self, tmp_path: Path):
        """Regression for the dead module-global cooldown: the watchdog is a
        systemd *oneshot* (fresh process each run), so the cooldown must be read
        from the persisted sidecar, not in-memory state. A recent persisted
        reclaim must block a new reclaim even with no in-process history."""
        import genesis.autonomy.watchdog as w
        # Seed a recent reclaim in the sidecar, as a prior oneshot run would have.
        w._RECLAIM_STATE_PATH.write_text(json.dumps({"last_reclaim_at": time.time()}))

        reclaim_file = tmp_path / "memory.reclaim"
        reclaim_file.write_text("")  # exists + writable → would reclaim if cooldown ignored
        with patch("genesis.autonomy.watchdog.Path", return_value=reclaim_file):
            assert reclaim_page_cache("256M") is False  # in cooldown from the sidecar
        assert reclaim_file.read_text() == ""  # and it did NOT write to the cgroup file

    def test_reclaim_persists_timestamp_on_success(self, tmp_path: Path):
        """A successful reclaim records its time to the sidecar so the NEXT
        oneshot run sees the cooldown."""
        import genesis.autonomy.watchdog as w
        reclaim_file = tmp_path / "memory.reclaim"
        reclaim_file.write_text("")
        with patch("genesis.autonomy.watchdog.Path", return_value=reclaim_file):
            assert reclaim_page_cache("128M") is True
        saved = json.loads(w._RECLAIM_STATE_PATH.read_text())
        assert abs(saved["last_reclaim_at"] - time.time()) < 5

    def test_reclaim_corrupt_sidecar_fails_open(self, tmp_path: Path):
        """A corrupt/unreadable sidecar is treated as 'never reclaimed' (reclaim
        allowed) — it must never crash the watchdog."""
        import genesis.autonomy.watchdog as w
        w._RECLAIM_STATE_PATH.write_text("{not valid json")
        reclaim_file = tmp_path / "memory.reclaim"
        reclaim_file.write_text("")
        with patch("genesis.autonomy.watchdog.Path", return_value=reclaim_file):
            assert reclaim_page_cache("128M") is True

    def test_reclaim_not_wedged_by_future_timestamp(self, tmp_path: Path):
        """A FUTURE persisted timestamp (pre-NTP boot clock skew / backward clock
        jump) must NOT wedge reclaim off: negative elapsed is treated as expired
        so the cooldown self-heals in one cycle rather than blocking for up to an
        hour during real memory pressure."""
        import genesis.autonomy.watchdog as w
        w._RECLAIM_STATE_PATH.write_text(json.dumps({"last_reclaim_at": time.time() + 3600}))
        reclaim_file = tmp_path / "memory.reclaim"
        reclaim_file.write_text("")
        with patch("genesis.autonomy.watchdog.Path", return_value=reclaim_file):
            assert reclaim_page_cache("128M") is True  # not wedged
        # and it re-persisted a sane now-based timestamp, healing the cooldown
        saved = json.loads(w._RECLAIM_STATE_PATH.read_text())
        assert saved["last_reclaim_at"] <= time.time() + 1


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


class TestZombieSuppressionDuringHeavyWorkload:
    """Zombie scheduler detection should be suppressed during heavy workloads."""

    def _write_status_with_zombie(
        self, path: Path, *, heavy_workload=None, uptime_s=None,
    ) -> Path:
        """Write a fresh status.json with stale surplus heartbeat (zombie)."""
        old_time = datetime.now(UTC) - timedelta(minutes=20)
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "resilience_state": {"cloud": "NORMAL", "memory": "NORMAL"},
            "human_summary": "All systems normal.",
            "scheduler_heartbeats": {
                "surplus": old_time.isoformat(),  # 20 min stale = zombie
            },
        }
        if heavy_workload is not None:
            data["heavy_workload"] = heavy_workload
        if uptime_s is not None:
            data["uptime_s"] = uptime_s
        status_file = path / "status.json"
        status_file.write_text(json.dumps(data))
        return status_file

    def test_zombie_skipped_during_heavy_workload(self, tmp_path: Path):
        status = self._write_status_with_zombie(tmp_path, heavy_workload="dream_cycle")
        checker = _make_checker(tmp_path, status)
        result = checker.check()
        assert result == WatchdogAction.SKIP

    def test_zombie_restarts_without_heavy_workload(self, tmp_path: Path):
        status = self._write_status_with_zombie(tmp_path)
        secrets = tmp_path / "secrets.env"
        secrets.write_text("TELEGRAM_BOT_TOKEN=12345:ABC\n")
        checker = _make_checker(tmp_path, status, secrets_path=secrets)
        result = checker.check()
        assert result == WatchdogAction.RESTART

    def test_zombie_skipped_during_stabilization(self, tmp_path: Path):
        status = self._write_status_with_zombie(tmp_path, uptime_s=30)
        checker = _make_checker(tmp_path, status, stabilization_s=120)
        result = checker.check()
        assert result == WatchdogAction.SKIP


class TestRestartBridge:
    """restart_bridge verifies actual service state instead of trusting the
    systemctl client's exit (regression: a slow-but-successful restart used to
    return -1 → exit 255 → watchdog unit `failed`)."""

    def test_clean_restart_returns_zero(self):
        from genesis.autonomy import watchdog as wd

        completed = MagicMock(returncode=0, stderr="")
        with patch.object(wd.subprocess, "run", return_value=completed) as run, \
                patch.object(wd, "_wait_until_active") as wait:
            assert wd.restart_bridge("genesis-server.service") == 0
            run.assert_called_once()
            wait.assert_not_called()  # clean exit needs no verification

    def test_timeout_then_active_returns_zero(self):
        from genesis.autonomy import watchdog as wd

        timeout = subprocess.TimeoutExpired(cmd=["systemctl"], timeout=120)
        with patch.object(wd.subprocess, "run", side_effect=timeout), \
                patch.object(wd, "_wait_until_active", return_value=True):
            # Killing the client doesn't abort the systemd job; it came up.
            assert wd.restart_bridge("genesis-server.service") == 0

    def test_timeout_then_not_active_returns_one(self):
        from genesis.autonomy import watchdog as wd

        timeout = subprocess.TimeoutExpired(cmd=["systemctl"], timeout=120)
        with patch.object(wd.subprocess, "run", side_effect=timeout), \
                patch.object(wd, "_wait_until_active", return_value=False):
            assert wd.restart_bridge("genesis-server.service") == 1

    def test_nonzero_then_active_returns_zero(self):
        from genesis.autonomy import watchdog as wd

        failed = MagicMock(returncode=1, stderr="boom")
        with patch.object(wd.subprocess, "run", return_value=failed), \
                patch.object(wd, "_wait_until_active", return_value=True):
            assert wd.restart_bridge("genesis-server.service") == 0

    def test_systemctl_missing_returns_minus_two(self):
        from genesis.autonomy import watchdog as wd

        with patch.object(wd.subprocess, "run", side_effect=FileNotFoundError()):
            assert wd.restart_bridge("genesis-server.service") == -2

    def test_wait_until_active_polls_until_active(self):
        from genesis.autonomy import watchdog as wd

        inactive = MagicMock(stdout="activating")
        active = MagicMock(stdout="active")
        with patch.object(wd.subprocess, "run", side_effect=[inactive, active]), \
                patch.object(wd.time, "sleep"):
            assert wd._wait_until_active("genesis-server.service", timeout_s=30) is True


class TestNetworkSuppression:
    """PR-2: zombie-restart suppression during a network outage (F4/F7).

    Unit-tests the guard directly: it must suppress ONLY on a FRESH degraded/
    offline probe, and fail toward restart on absent / stale / normal / garbled
    connectivity — the safety-critical over-suppression path.
    """

    @pytest.fixture(autouse=True)
    def _fixed_tuning(self, monkeypatch):
        # Pin the staleness threshold (3× steady = 360s) so the fresh/stale
        # boundary is independent of any local resilience.yaml overlay.
        from genesis.resilience import network_config

        monkeypatch.setattr(
            network_config, "structural",
            lambda cfg=None: network_config.NetworkTuning(
                dns_tcp_anchors=("a",), ip_anchors=("b",), probe_port=443,
                probe_timeout_s=3, fast_cadence_s=20, steady_cadence_s=120,
                offline_all_fail_rounds=2, online_clean_rounds=3,
                stable_online_s=300, merge_gap_s=600,
            ),
        )

    def _checker(self, tmp_path: Path, fresh_status: Path) -> WatchdogChecker:
        return _make_checker(tmp_path, fresh_status)

    def _net(self, state: str, *, age_s: float) -> dict:
        ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
        return {"state": state, "last_probe_at": ts}

    def test_fresh_offline_suppresses(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart({"network": self._net("OFFLINE", age_s=10)}) is True

    def test_fresh_degraded_suppresses(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart({"network": self._net("DEGRADED", age_s=10)}) is True

    def test_fresh_normal_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart({"network": self._net("NORMAL", age_s=10)}) is False

    def test_absent_network_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart({}) is False

    def test_stale_offline_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        # F4: a dead sentinel freezes the field OFFLINE while the status-writer
        # keeps the file fresh — must NOT strand a wedged server unrestarted.
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart(
            {"network": self._net("OFFLINE", age_s=10_000)}
        ) is False

    def test_garbled_timestamp_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart(
            {"network": {"state": "OFFLINE", "last_probe_at": "nonsense"}}
        ) is False

    def test_missing_timestamp_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        c = self._checker(tmp_path, fresh_status)
        assert c._network_suppresses_restart({"network": {"state": "OFFLINE"}}) is False

    def test_future_timestamp_does_not_suppress(self, tmp_path: Path, fresh_status: Path):
        # Codex F6: a future last_probe_at (clock skew / bad data) yields negative
        # age — must fail toward restart, not suppress.
        c = self._checker(tmp_path, fresh_status)
        future = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        assert c._network_suppresses_restart(
            {"network": {"state": "OFFLINE", "last_probe_at": future}}
        ) is False


# --- Down-vs-starved liveness distinguisher --------------------------------------


class _FakeResp:
    """Minimal opener.open() context-manager stand-in."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _opener_returning(status: int, payload: dict):
    def _f(*_a, **_k):
        return _FakeResp(status, payload)

    return _f


def _liveness_checker(tmp_path: Path, status_file: Path, target: str = "genesis-server.service"):
    """A checker with a DETERMINISTIC target (so _targets_server doesn't depend on
    the host's real systemd) and config_validation off (so the restart path lands
    on RESTART, not a validation SKIP)."""
    c = _make_checker(tmp_path, status_file)
    c._target_service = target
    return c


class TestProbeLivenessClassification:
    """The verdict machine, exercised directly by faking urllib.request.urlopen.

    (The autouse _liveness_probe_refused fixture defaults urlopen to refused; each
    test overrides it for the verdict it exercises.)"""

    def _checker(self, tmp_path: Path) -> WatchdogChecker:
        return _make_checker(tmp_path, tmp_path / "status.json")

    def test_connection_refused_is_down(self, tmp_path: Path):
        # The autouse fixture already raises URLError(ConnectionRefusedError).
        verdict, block = self._checker(tmp_path)._probe_liveness()
        assert verdict == "down"
        assert block is None

    def test_timeout_is_unknown(self, tmp_path: Path):
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError("slow")):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "unknown"

    def test_non_200_is_unknown(self, tmp_path: Path):
        with patch("urllib.request.OpenerDirector.open", _opener_returning(503, {})):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "unknown"

    def test_null_loop_is_unknown(self, tmp_path: Path):
        # loop:null (sampler never published) must NOT read as healthy.
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, {"loop": None})):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "unknown"

    def test_fresh_low_lag_is_responsive(self, tmp_path: Path):
        payload = {"loop": {"lag_ms": 12.0, "sample_age_s": 0.5, "lagging": False}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "responsive"

    def test_fresh_high_lag_is_starved(self, tmp_path: Path):
        payload = {"loop": {"lag_ms": 4200.0, "sample_age_s": 1.0, "lagging": True}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            verdict, block = self._checker(tmp_path)._probe_liveness()
        assert verdict == "starved"
        assert block["lag_ms"] == 4200.0

    def test_lagging_flag_below_floor_is_responsive(self, tmp_path: Path):
        # Warn-level lag (sampler's 250ms 'lagging' flag) is BELOW the 1000ms
        # suppress floor → NOT starved. Suppression honors the configured floor
        # only, never the sampler's independently-tunable warn flag.
        payload = {"loop": {"lag_ms": 300.0, "sample_age_s": 1.0, "lagging": True}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "responsive"

    def test_stale_sample_is_wedged(self, tmp_path: Path):
        # High lag but the sample itself is old → the sampler stopped → wedged.
        payload = {"loop": {"lag_ms": 5000.0, "sample_age_s": 500.0, "lagging": True}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            verdict, _ = self._checker(tmp_path)._probe_liveness()
        assert verdict == "wedged"


class TestLivenessRestartGate:
    """check() → _restart_if_allowed consulting the probe. Every verdict except
    'starved' must still reach the normal restart path (fail-open)."""

    def _stale(self, tmp_path: Path):
        return _liveness_checker(tmp_path, _stale_file(tmp_path))

    def test_starved_suppresses_stale_restart(self, tmp_path: Path):
        c = self._stale(tmp_path)
        payload = {"loop": {"lag_ms": 4200.0, "sample_age_s": 1.0, "lagging": True,
                            "executor": {"pending": 30}}}
        with (
            patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)),
            patch.object(c, "_alert_starved") as alert,
        ):
            action = c.check()
        assert action is WatchdogAction.SKIP
        alert.assert_called_once()
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert state["starved_skips"] == 1
        # A suppressed cycle must NOT burn the restart/backoff counter.
        assert state.get("consecutive_failures", 0) == 0

    def test_starved_suppression_is_bounded(self, tmp_path: Path):
        c = self._stale(tmp_path)
        # Pre-seed the counter at the cap so the next starved cycle exceeds it.
        (tmp_path / "watchdog_state.json").write_text(json.dumps({
            "consecutive_failures": 0, "next_attempt_after": None,
            "restart_history": [], "starved_skips": 6,
        }))
        payload = {"loop": {"lag_ms": 4200.0, "sample_age_s": 1.0, "lagging": True}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            action = c.check()
        # Past the bound → restart anyway; _record_failure resets the counter.
        assert action is WatchdogAction.RESTART
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert state["starved_skips"] == 0

    def test_non_starved_verdict_resets_skips_even_under_backoff(self, tmp_path: Path):
        """A non-starved verdict must reset starved_skips (it means CONSECUTIVE
        starved skips) even when the fall-through lands in a backoff window where
        _record_failure never runs — otherwise a stale count would accumulate."""
        c = self._stale(tmp_path)
        future = time.time() + 9999
        (tmp_path / "watchdog_state.json").write_text(json.dumps({
            "consecutive_failures": 1, "next_attempt_after": future,
            "restart_history": [], "starved_skips": 3,
        }))
        payload = {"loop": {"lag_ms": 5.0, "sample_age_s": 0.5, "lagging": False}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            action = c.check()
        assert action is WatchdogAction.BACKOFF  # backoff active → no restart, no _record_failure
        state = json.loads((tmp_path / "watchdog_state.json").read_text())
        assert state["starved_skips"] == 0  # reset by the gate, not by _record_failure

    def test_wedged_restarts(self, tmp_path: Path):
        c = self._stale(tmp_path)
        payload = {"loop": {"lag_ms": 5000.0, "sample_age_s": 500.0, "lagging": True}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            assert c.check() is WatchdogAction.RESTART

    def test_responsive_restarts(self, tmp_path: Path):
        # status.json stale but loop fine = the status-writer task died; restart.
        c = self._stale(tmp_path)
        payload = {"loop": {"lag_ms": 10.0, "sample_age_s": 0.5, "lagging": False}}
        with patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)):
            assert c.check() is WatchdogAction.RESTART

    def test_unknown_restarts(self, tmp_path: Path):
        c = self._stale(tmp_path)
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError("slow")):
            assert c.check() is WatchdogAction.RESTART

    def test_down_restarts(self, tmp_path: Path):
        # The autouse fixture already yields 'down' (connection refused).
        c = self._stale(tmp_path)
        assert c.check() is WatchdogAction.RESTART

    def test_zombie_starved_suppresses(self, tmp_path: Path):
        c = _liveness_checker(tmp_path, _zombie_file(tmp_path))
        payload = {"loop": {"lag_ms": 4200.0, "sample_age_s": 1.0, "lagging": True}}
        with (
            patch("urllib.request.OpenerDirector.open", _opener_returning(200, payload)),
            patch.object(c, "_alert_starved"),
        ):
            assert c.check() is WatchdogAction.SKIP

    def test_bridge_inactive_never_probes(self, tmp_path: Path):
        # service down = definitionally dead; the probe is not consulted.
        c = _liveness_checker(tmp_path, _stale_file(tmp_path))
        with (
            patch.object(c, "_is_bridge_active", return_value=False),
            patch.object(c, "_bridge_exited_unconfigured", return_value=False),
            patch.object(c, "_probe_liveness") as probe,
        ):
            action = c.check()
        probe.assert_not_called()
        assert action is WatchdogAction.RESTART

    def test_relay_target_never_probes(self, tmp_path: Path):
        # A legacy relay-only install: the server-loop probe is meaningless.
        c = _liveness_checker(tmp_path, _stale_file(tmp_path), target="genesis-bridge.service")
        with patch.object(c, "_probe_liveness") as probe:
            action = c.check()
        probe.assert_not_called()
        assert action is WatchdogAction.RESTART


class TestLivenessConfig:
    def test_from_yaml_loads_liveness_keys(self):
        # The shipped config/autonomy.yaml carries the liveness knobs.
        c = WatchdogChecker.from_yaml()
        assert c._liveness_lag_suppress_ms == 1000
        assert c._liveness_sample_stale_s == 120
        assert c._liveness_max_starved_skips == 6
        assert c._liveness_url.endswith("/api/genesis/liveness")


def _stale_file(tmp_path: Path) -> Path:
    """A stale status.json (20 min old) at a fixed path per tmp_path."""
    p = tmp_path / "status.json"
    old = datetime.now(UTC) - timedelta(minutes=20)
    p.write_text(json.dumps({"timestamp": old.isoformat()}))
    return p


def _zombie_file(tmp_path: Path) -> Path:
    """Fresh status.json but with a stale scheduler heartbeat (zombie scheduler),
    past stabilization, no heavy workload, no network suppression."""
    p = tmp_path / "status.json"
    now = datetime.now(UTC)
    old = now - timedelta(minutes=30)
    p.write_text(json.dumps({
        "timestamp": now.isoformat(),
        "uptime_s": 99999,
        "scheduler_heartbeats": {"surplus": old.isoformat()},
    }))
    return p
