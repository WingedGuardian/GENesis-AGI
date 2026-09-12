"""dream_shield config lever — enable/kill-switch + knob degradation."""

from __future__ import annotations

import pytest

from genesis.memory import dream_shield_config as cfg


def test_defaults_shape_and_values():
    assert cfg.DEFAULTS["enabled"] is True
    assert cfg.DEFAULTS["activation_percentile"] == 0.90
    assert cfg.DEFAULTS["centrality_percentile"] == 0.90
    assert cfg.DEFAULTS["confidence_floor"] == 0.98
    assert cfg.DEFAULTS["deprecated_edge_prune_days"] == 30


def test_shield_enabled_default_true(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: dict(cfg.DEFAULTS))
    assert cfg.shield_enabled() is True


def test_shield_enabled_false_when_disabled(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": False})
    assert cfg.shield_enabled() is False


def test_shield_enabled_false_when_yaml_false(monkeypatch):
    # YAML-1.1 unquoted `enabled: no` → boolean False; honor the intent.
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": False})
    assert cfg.shield_enabled() is False


def test_env_kill_switch_forces_disabled(monkeypatch):
    monkeypatch.setenv(cfg._ENV_KILL_SWITCH, "1")
    monkeypatch.setattr(cfg, "load_config", lambda: dict(cfg.DEFAULTS))
    assert cfg.shield_enabled() is False


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.75, 0.75),
        (0.0, 0.0),
        (1.0, 1.0),
        (1.5, 0.90),  # out of [0,1] → default
        (-0.1, 0.90),
        (True, 0.90),  # bool is not a valid float knob
        ("x", 0.90),
        (None, 0.90),
    ],
)
def test_knob_float01_falls_back_on_damage(value, expected):
    assert cfg.knob_float01({"activation_percentile": value}, "activation_percentile") == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (14, 14),
        (0, 30),
        (-3, 30),
        (True, 30),
        ("x", 30),
        (None, 30),
    ],
)
def test_knob_int_falls_back_on_damage(value, expected):
    assert (
        cfg.knob_int({"deprecated_edge_prune_days": value}, "deprecated_edge_prune_days")
        == expected
    )


def test_load_config_returns_defaults_shape():
    merged = cfg.load_config()
    for k in cfg.DEFAULTS:
        assert k in merged
