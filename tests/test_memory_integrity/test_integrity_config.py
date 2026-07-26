"""memory_integrity_config mode-lever + knob-coercion tests (sync)."""

from __future__ import annotations

from genesis.memory import integrity_config as ic


def test_default_mode_is_passive(monkeypatch):
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    assert ic.effective_mode() == "passive"


def test_active_coerced_to_passive(monkeypatch):
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(ic, "load_config", lambda: {**ic.DEFAULTS, "mode": "active"})
    assert ic.effective_mode() == "passive"


def test_invalid_mode_degrades_to_passive(monkeypatch):
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(ic, "load_config", lambda: {**ic.DEFAULTS, "mode": "bogus"})
    assert ic.effective_mode() == "passive"


def test_env_kill_switch_forces_off(monkeypatch):
    monkeypatch.setenv(ic._ENV_KILL_SWITCH, "1")
    assert ic.effective_mode() == "off"


def test_enabled_false_is_off(monkeypatch):
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(ic, "load_config", lambda: {**ic.DEFAULTS, "enabled": False})
    assert ic.effective_mode() == "off"


def test_yaml_off_boolean_honored(monkeypatch):
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    # unquoted `mode: off` parses to boolean False under YAML 1.1
    monkeypatch.setattr(ic, "load_config", lambda: {**ic.DEFAULTS, "mode": False})
    assert ic.effective_mode() == "off"


def test_knob_coercion_on_damaged_values():
    cfg = {"severe_min_count": -3, "drift_band": 5.0, "rerank_timeout_s": 0}
    assert ic.knob_int(cfg, "severe_min_count") == ic.DEFAULTS["severe_min_count"]
    assert ic.knob_float01(cfg, "drift_band") == ic.DEFAULTS["drift_band"]
    assert ic.knob_float(cfg, "rerank_timeout_s") == ic.DEFAULTS["rerank_timeout_s"]


def test_defaults_are_fresh_install_safe():
    # A fresh clone with no overlay must resolve to passive + sane knobs.
    cfg = ic.load_config()
    assert cfg["enabled"] is True
    assert cfg["mode"] == "passive"
    assert cfg["sample_fraction"] == 1.0
