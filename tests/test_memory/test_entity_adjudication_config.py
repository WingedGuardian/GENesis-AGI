"""entity_adjudication config lever — mode degradation + env kill switch."""

from __future__ import annotations

import pytest

from genesis.memory import entity_adjudication_config as cfg


def test_defaults_are_shadow():
    assert cfg.DEFAULTS["mode"] == "propose_only"
    assert cfg.DEFAULTS["enabled"] is True


def test_effective_mode_reads_file(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "live"})
    assert cfg.effective_mode() == "live"


def test_effective_mode_disabled_is_off(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": False, "mode": "live"})
    assert cfg.effective_mode() == "off"


def test_effective_mode_invalid_degrades_to_propose_only(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "bogus"})
    assert cfg.effective_mode() == "propose_only"


def test_effective_mode_yaml_false_is_off(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": False})
    assert cfg.effective_mode() == "off"


def test_env_kill_switch_forces_off(monkeypatch):
    monkeypatch.setenv(cfg._ENV_KILL_SWITCH, "1")
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "live"})
    assert cfg.effective_mode() == "off"


@pytest.mark.parametrize(
    "value,expected",
    [
        (5, 5),
        (0, 20),
        (-3, 20),
        (True, 20),
        ("x", 20),
        (None, 20),
    ],
)
def test_knob_int_falls_back_on_damage(value, expected):
    assert cfg.knob_int({"drain_budget": value}, "drain_budget") == expected


def test_load_config_returns_defaults_shape():
    merged = cfg.load_config()
    for k in cfg.DEFAULTS:
        assert k in merged
