"""network_config — levers, kill switch, invalid-degrades, structural coercion."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from genesis.resilience import network_config, network_state


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """Point network_config at a temp resilience.yaml and clear the kill switch."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    monkeypatch.setattr(network_config, "_base_path", lambda: cfgdir / "resilience.yaml")
    monkeypatch.delenv(network_config._ENV_KILL_SWITCH, raising=False)
    # Neutralize any real ~/.genesis overlay so tests are hermetic.
    monkeypatch.setattr(network_config, "merge_local_overlay", lambda raw, path: raw)
    return cfgdir / "resilience.yaml"


def _write(path, body: str):
    path.write_text(textwrap.dedent(body))


def test_defaults_when_no_file(cfg_env):
    assert network_config.effective_parking_mode() == "shadow"
    assert network_config.backup_push_retry_mode() == "live"
    assert network_config.sentinel_enabled() is True


def test_kill_switch_overrides_config(cfg_env, monkeypatch):
    _write(cfg_env, "network:\n  enabled: true\n")
    monkeypatch.setenv(network_config._ENV_KILL_SWITCH, "1")
    assert network_config.sentinel_enabled() is False


def test_enabled_false_disables(cfg_env):
    _write(cfg_env, "network:\n  enabled: false\n")
    assert network_config.sentinel_enabled() is False
    assert network_config.effective_parking_mode() == "off"


def test_invalid_parking_mode_degrades_to_shadow(cfg_env):
    _write(cfg_env, "network:\n  parking_mode: bogus\n")
    assert network_config.effective_parking_mode() == "shadow"


def test_invalid_backup_retry_degrades_to_off(cfg_env):
    _write(cfg_env, "network:\n  backup_push_retry: bogus\n")
    assert network_config.backup_push_retry_mode() == "off"


def test_live_parking_mode_honored(cfg_env):
    _write(cfg_env, "network:\n  parking_mode: live\n")
    assert network_config.effective_parking_mode() == "live"


def test_yaml_off_boolean_honored(cfg_env):
    # unquoted `off` parses as YAML-1.1 boolean False
    _write(cfg_env, "network:\n  parking_mode: off\n")
    assert network_config.effective_parking_mode() == "off"


def test_structural_defaults_and_derived_threshold(cfg_env):
    t = network_config.structural()
    assert t.steady_cadence_s == 120
    assert t.staleness_threshold_s == 360  # 3× steady
    assert t.dns_tcp_anchors and t.ip_anchors


def test_structural_coerces_bad_ints_to_default(cfg_env):
    _write(cfg_env, "network:\n  steady_cadence_s: -5\n  probe_timeout_s: 0\n")
    t = network_config.structural()
    assert t.steady_cadence_s == 120  # bad negative → default
    assert t.probe_timeout_s == 3  # zero → default


def test_structural_overlay_updates(cfg_env):
    _write(cfg_env, "network:\n  steady_cadence_s: 60\n")
    t = network_config.structural()
    assert t.steady_cadence_s == 60
    assert t.staleness_threshold_s == 180


# ── parking_decision — the shared PR-3 consumer gate ───────────────────

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def _snap(state: str, *, age_s: float = 10.0, cause: str = "all_fail") -> dict:
    """A network_state snapshot whose last_probe_at is ``age_s`` before _NOW."""
    return {
        "state": state,
        "since": (_NOW - timedelta(minutes=5)).isoformat(),
        "cause": cause,
        "last_probe_at": (_NOW - timedelta(seconds=age_s)).isoformat(),
        "window_open": state == "OFFLINE",
        "closed_windows": [],
    }


@pytest.fixture()
def patch_state(monkeypatch):
    """Install a controllable network_state.read_state for parking_decision."""

    def _install(snapshot):
        monkeypatch.setattr(network_state, "read_state", lambda path=None: snapshot)

    return _install


def test_parking_off_when_lever_off(cfg_env, patch_state):
    _write(cfg_env, "network:\n  enabled: false\n")
    patch_state(_snap("OFFLINE"))
    assert network_config.parking_decision(now=_NOW) == "off"


def test_parking_normal_when_no_snapshot(cfg_env, patch_state):
    patch_state(None)  # absent / empty-state install
    assert network_config.parking_decision(now=_NOW) == "normal"


def test_parking_normal_when_state_normal(cfg_env, patch_state):
    patch_state(_snap("NORMAL"))
    assert network_config.parking_decision(now=_NOW) == "normal"


def test_parking_normal_when_state_degraded(cfg_env, patch_state):
    # DEGRADED (slow-but-working / dns_only) must NOT trigger parking.
    patch_state(_snap("DEGRADED", cause="dns_only"))
    assert network_config.parking_decision(now=_NOW) == "normal"


def test_parking_shadow_on_fresh_offline_default_mode(cfg_env, patch_state):
    # Default parking_mode is shadow → observe, don't act.
    patch_state(_snap("OFFLINE", age_s=30))
    assert network_config.parking_decision(now=_NOW) == "shadow"


def test_parking_park_on_fresh_offline_live_mode(cfg_env, patch_state):
    _write(cfg_env, "network:\n  parking_mode: live\n")
    patch_state(_snap("OFFLINE", age_s=30))
    assert network_config.parking_decision(now=_NOW) == "park"


def test_parking_normal_when_offline_but_stale(cfg_env, patch_state):
    # OFFLINE but snapshot older than 3× steady cadence (360s) → don't trust it.
    _write(cfg_env, "network:\n  parking_mode: live\n")
    patch_state(_snap("OFFLINE", age_s=400))
    assert network_config.parking_decision(now=_NOW) == "normal"


def test_parking_normal_when_timestamp_unparseable(cfg_env, patch_state):
    _write(cfg_env, "network:\n  parking_mode: live\n")
    snap = _snap("OFFLINE")
    snap["last_probe_at"] = "not-a-timestamp"
    patch_state(snap)
    assert network_config.parking_decision(now=_NOW) == "normal"


def test_kill_switch_forces_parking_off_despite_fresh_offline(cfg_env, patch_state, monkeypatch):
    # The env kill switch must immediately disable parking even with a fresh
    # OFFLINE snapshot + parking_mode=live on disk (don't wait for it to age out).
    _write(cfg_env, "network:\n  enabled: true\n  parking_mode: live\n")
    patch_state(_snap("OFFLINE", age_s=30))
    monkeypatch.setenv(network_config._ENV_KILL_SWITCH, "1")
    assert network_config.effective_parking_mode() == "off"
    assert network_config.parking_decision(now=_NOW) == "off"
