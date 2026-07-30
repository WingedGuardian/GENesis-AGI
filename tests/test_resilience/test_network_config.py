"""network_config — levers, kill switch, invalid-degrades, structural coercion."""

from __future__ import annotations

import textwrap

import pytest

from genesis.resilience import network_config


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
