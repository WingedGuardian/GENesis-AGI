"""91cdc681 — the durable alert-queue writers must resolve their root via
``env.alert_queue_root()`` (GENESIS_HOME-aware) so no test writes a REAL
``~/.genesis/alerts/queue`` entry the live server would drain to the owner.

Repo-wide safety net: ``test_watchdog.py`` stubs ``enqueue_alert`` locally, but
any OTHER test reaching an alert path was unprotected. These lock the central
resolver + the global ``_isolate_alert_queue`` conftest fixture.
"""

from __future__ import annotations

from pathlib import Path

import genesis.env as env
from genesis.autonomy.watchdog import WatchdogChecker


def _build_checker(tmp_path: Path) -> WatchdogChecker:
    """Minimal real WatchdogChecker (mirrors test_watchdog._make_checker)."""
    return WatchdogChecker(
        status_file=str(tmp_path / "status.json"),
        staleness_threshold_s=300,
        max_restart_attempts=5,
        backoff_initial_s=1,
        backoff_max_s=10,
        config_validation=False,
        secrets_path=str(tmp_path / "secrets.env"),
        state_file=str(tmp_path / "watchdog_state.json"),
        stabilization_s=600,
        flap_window_s=21600,
        flap_threshold=3,
        flap_backoff_max_s=7200,
    )


def test_conftest_isolates_alert_queue_off_real_home():
    """The global autouse fixture must redirect the resolver away from real home."""
    real = Path.home() / ".genesis" / "alerts" / "queue"
    assert env.alert_queue_root() != real


def test_alert_flap_writes_through_resolver(tmp_path, monkeypatch):
    """_alert_flap must enqueue under env.alert_queue_root(), not a hardcoded home."""
    # Contain any Path.home() fallback so a RED run (buggy code) never pollutes
    # the real ~/.genesis; assert on a distinct resolver path.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    queue = tmp_path / "iso" / "alerts" / "queue"
    monkeypatch.setattr(env, "alert_queue_root", lambda: queue)

    checker = _build_checker(tmp_path)
    checker._alert_flap("repro-reason", 3, 60.0)

    assert list(queue.glob("*.json")), (
        "alert must be written under env.alert_queue_root(), not a hardcoded "
        "Path.home()/.genesis/alerts/queue"
    )
    # Real home must be untouched.
    real = Path(tmp_path / "home") / ".genesis" / "alerts" / "queue"
    assert not (real.exists() and list(real.glob("*.json")))


def test_alert_starved_writes_through_resolver(tmp_path, monkeypatch):
    """_alert_starved must also route through the resolver."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    queue = tmp_path / "iso" / "alerts" / "queue"
    monkeypatch.setattr(env, "alert_queue_root", lambda: queue)

    checker = _build_checker(tmp_path)
    checker._alert_starved("repro-reason", {"lag_ms": 1200, "sample_age_s": 3}, 1)

    assert list(queue.glob("*.json")), "starved alert must be written under env.alert_queue_root()"
