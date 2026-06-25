"""Unit tests for the pure ambient-health evaluator.

Covers the alert-decision logic (the testable core); the SSH read + scheduler
wiring are verified on-device (they need the edge + a running scheduler).
"""
from datetime import UTC, datetime, timedelta

import pytest

from genesis.observability import ambient_health
from genesis.observability.ambient_health import (
    AmbientRemoteConfig,
    AmbientRemoteConfigError,
    evaluate_ambient_health,
    load_ambient_remote_config,
)

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def _snapshot(**overrides) -> dict:
    base = {
        "ts": NOW.isoformat(),
        "active_connections": 1,
        "diar_enabled": True,
        "diar_worker_alive": True,
    }
    base.update(overrides)
    return base


def test_healthy_snapshot_is_ok():
    assert evaluate_ambient_health(_snapshot(), now=NOW).status == "ok"


def test_none_data_is_unknown():
    # Transient SSH failure must not be reported as a hard "down".
    assert evaluate_ambient_health(None, now=NOW).status == "unknown"


def test_stale_heartbeat_is_down():
    stale = (NOW - timedelta(minutes=10)).isoformat()
    verdict = evaluate_ambient_health(_snapshot(ts=stale), now=NOW)
    assert verdict.status == "down"
    assert any("stale" in r for r in verdict.reasons)


def test_missing_ts_is_down():
    assert evaluate_ambient_health(_snapshot(ts=None), now=NOW).status == "down"


def test_device_offline_is_not_a_fault():
    # Device absent (active_connections=0) with a fresh heartbeat + live worker is
    # NOT a software bug — must NOT alert (policy: only software failures alert).
    assert evaluate_ambient_health(_snapshot(active_connections=0), now=NOW).status == "ok"


def test_device_offline_does_not_mask_software_failure():
    # A real software failure (dead diar worker) still fires even if the device
    # happens to be offline at the same time.
    verdict = evaluate_ambient_health(
        _snapshot(active_connections=0, diar_worker_alive=False), now=NOW,
    )
    assert verdict.status == "degraded"


def test_dead_diar_worker_is_degraded():
    verdict = evaluate_ambient_health(_snapshot(diar_worker_alive=False), now=NOW)
    assert verdict.status == "degraded"


def test_quiet_room_old_last_ts_is_still_ok():
    # No recent utterance (quiet room) must NOT be treated as a fault.
    old = (NOW - timedelta(hours=3)).isoformat()
    assert evaluate_ambient_health(_snapshot(last_ts=old), now=NOW).status == "ok"


def test_diar_disabled_does_not_degrade():
    # If diarization is off, a False worker flag is irrelevant.
    snap = _snapshot(diar_enabled=False, diar_worker_alive=False)
    assert evaluate_ambient_health(snap, now=NOW).status == "ok"


# --- load_ambient_remote_config: absent/disabled -> None; present-but-malformed -> raise ---


def _point_config_at(tmp_path, monkeypatch, text):
    cfg = tmp_path / "ambient_remote.yaml"
    cfg.write_text(text)
    monkeypatch.setattr(ambient_health, "_CONFIG_PATH", cfg)


def test_load_absent_returns_none(tmp_path, monkeypatch):
    # No config file -> legit no-op (install without an ambient edge).
    monkeypatch.setattr(ambient_health, "_CONFIG_PATH", tmp_path / "absent.yaml")
    assert load_ambient_remote_config() is None


def test_load_disabled_returns_none(tmp_path, monkeypatch):
    # enabled: false -> intentional disable, also a legit no-op (no raise).
    _point_config_at(tmp_path, monkeypatch, "enabled: false\nhost_ip: x\nhost_user: y\n")
    assert load_ambient_remote_config() is None


def test_load_valid_returns_config(tmp_path, monkeypatch):
    _point_config_at(tmp_path, monkeypatch, "host_ip: 192.0.2.9\nhost_user: edge\nenabled: true\n")
    cfg = load_ambient_remote_config()
    assert isinstance(cfg, AmbientRemoteConfig)
    assert cfg.host_ip == "192.0.2.9"
    assert cfg.host_user == "edge"


def test_load_missing_host_user_raises(tmp_path, monkeypatch):
    # Present + enabled but missing host_user -> misconfigured: MUST raise (visible),
    # not silently return None and look identical to "not configured".
    _point_config_at(tmp_path, monkeypatch, "host_ip: 192.0.2.9\nenabled: true\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()


def test_load_missing_host_ip_raises(tmp_path, monkeypatch):
    # The other branch of the `not host_ip or not host_user` guard.
    _point_config_at(tmp_path, monkeypatch, "host_user: edge\nenabled: true\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()


def test_load_unparseable_raises(tmp_path, monkeypatch):
    # Broken YAML (unterminated flow sequence) -> raise, not silent None.
    _point_config_at(tmp_path, monkeypatch, "host_ip: [1, 2\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()
