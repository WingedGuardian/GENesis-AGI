"""Tests for the skill-edit Critic gate lever (config + env)."""

from __future__ import annotations

import pytest

from genesis import env
from genesis.learning.skills import skill_gate_config as cfg


def test_shipped_default_is_shadow():
    """The shipped config/skill_evolution_gate.yaml must read as shadow on a
    fresh clone with no overlay (empty-state correctness)."""
    assert cfg.skill_gate_mode() == "shadow"


def test_enabled_false_forces_off(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": False, "mode": "shadow"})
    assert cfg.skill_gate_mode() == "off"


def test_invalid_mode_degrades_to_shadow(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "enforce"})
    assert cfg.skill_gate_mode() == "shadow"


def test_mode_off_honored(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "off"})
    assert cfg.skill_gate_mode() == "off"


def test_yaml_boolean_off_honored(monkeypatch):
    # Unquoted `mode: off` parses as YAML-1.1 False; honor the operator intent.
    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": False})
    assert cfg.skill_gate_mode() == "off"


def test_load_config_returns_defaults_shape():
    merged = cfg.load_config()
    assert "enabled" in merged
    assert "mode" in merged


@pytest.mark.parametrize(
    "value,expected", [("1", True), ("true", True), ("yes", True), ("", False), ("0", False)]
)
def test_env_kill_switch(monkeypatch, value, expected):
    monkeypatch.setenv("GENESIS_SKILL_EVOLUTION_GATE_OFF", value)
    assert env.skill_gate_off() is expected


def test_env_kill_switch_unset(monkeypatch):
    monkeypatch.delenv("GENESIS_SKILL_EVOLUTION_GATE_OFF", raising=False)
    assert env.skill_gate_off() is False
