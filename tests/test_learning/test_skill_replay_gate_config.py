"""Tests for the held-out replay gate lever (skill_gate_config.replay) + its
settings validator. load_config is monkeypatched for hermeticity."""

from __future__ import annotations

from genesis.learning.skills import skill_gate_config as sgc
from genesis.mcp.health.settings import _validate_skill_evolution_gate


def _cfg(monkeypatch, cfg):
    monkeypatch.setattr(sgc, "load_config", lambda: cfg)


def test_default_replay_mode_is_shadow():
    # The shipped DEFAULTS must be safe/observable on a fresh clone.
    assert sgc.DEFAULTS["replay"]["mode"] == "shadow"


def test_replay_mode_shadow(monkeypatch):
    _cfg(monkeypatch, {"enabled": True, "replay": {"mode": "shadow"}})
    assert sgc.skill_replay_mode() == "shadow"


def test_master_disabled_forces_off(monkeypatch):
    _cfg(monkeypatch, {"enabled": False, "replay": {"mode": "shadow"}})
    assert sgc.skill_replay_mode() == "off"


def test_replay_mode_off(monkeypatch):
    _cfg(monkeypatch, {"enabled": True, "replay": {"mode": "off"}})
    assert sgc.skill_replay_mode() == "off"


def test_yaml_boolean_off_honored(monkeypatch):
    # Unquoted `mode: off` parses as YAML-1.1 boolean False.
    _cfg(monkeypatch, {"enabled": True, "replay": {"mode": False}})
    assert sgc.skill_replay_mode() == "off"


def test_invalid_replay_mode_degrades_to_shadow(monkeypatch):
    _cfg(monkeypatch, {"enabled": True, "replay": {"mode": "enforce"}})
    assert sgc.skill_replay_mode() == "shadow"


def test_missing_replay_block_degrades_to_shadow(monkeypatch):
    _cfg(monkeypatch, {"enabled": True})
    assert sgc.skill_replay_mode() == "shadow"


def test_replay_config_reads_knobs(monkeypatch):
    _cfg(
        monkeypatch, {"enabled": True, "replay": {"mode": "shadow", "epsilon": 0.1, "min_pairs": 8}}
    )
    cfg = sgc.skill_replay_config()
    assert cfg == {"mode": "shadow", "epsilon": 0.1, "min_pairs": 8}


def test_replay_config_clamps_and_defaults(monkeypatch):
    # min_pairs 0 clamps to 1; a non-numeric epsilon falls back to the default.
    _cfg(monkeypatch, {"enabled": True, "replay": {"epsilon": "nope", "min_pairs": 0}})
    cfg = sgc.skill_replay_config()
    assert cfg["min_pairs"] == 1
    assert cfg["epsilon"] == 0.05


# ── settings validator ──────────────────────────────────────────────────


def test_validator_accepts_valid_replay():
    errs = _validate_skill_evolution_gate(
        {"replay": {"mode": "shadow", "epsilon": 0.05, "min_pairs": 5}}
    )
    assert errs == []


def test_validator_rejects_bad_replay_mode():
    errs = _validate_skill_evolution_gate({"replay": {"mode": "enforce"}})
    assert any("replay.mode" in e for e in errs)


def test_validator_rejects_epsilon_out_of_range():
    errs = _validate_skill_evolution_gate({"replay": {"epsilon": 1.5}})
    assert any("replay.epsilon" in e for e in errs)


def test_validator_rejects_min_pairs_zero():
    errs = _validate_skill_evolution_gate({"replay": {"min_pairs": 0}})
    assert any("replay.min_pairs" in e for e in errs)


def test_validator_rejects_unknown_replay_key():
    errs = _validate_skill_evolution_gate({"replay": {"bogus": 1}})
    assert any("replay.bogus" in e for e in errs)


def test_validator_rejects_non_mapping_replay():
    errs = _validate_skill_evolution_gate({"replay": "shadow"})
    assert any("must be a mapping" in e for e in errs)


def test_validator_still_accepts_critic_mode():
    assert _validate_skill_evolution_gate({"enabled": True, "mode": "shadow"}) == []
