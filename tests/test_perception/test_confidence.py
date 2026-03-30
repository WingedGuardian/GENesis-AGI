"""Tests for confidence gating helper."""

from __future__ import annotations

from genesis.perception.confidence import (
    ConfidenceGatesConfig,
    DeepReflectionGateConfig,
    GateConfig,
    load_config,
    should_gate,
)


def test_should_gate_none_always_passes():
    gate = GateConfig(min_confidence=0.5, shadow_mode=False)
    gated, msg = should_gate(None, gate)
    assert not gated
    assert msg == ""


def test_should_gate_below_threshold_enforced():
    gate = GateConfig(min_confidence=0.5, shadow_mode=False)
    gated, msg = should_gate(0.3, gate)
    assert gated
    assert "0.30" in msg
    assert "0.50" in msg


def test_should_gate_above_threshold():
    gate = GateConfig(min_confidence=0.5, shadow_mode=False)
    gated, msg = should_gate(0.8, gate)
    assert not gated


def test_should_gate_shadow_mode_logs_but_passes():
    gate = GateConfig(min_confidence=0.5, shadow_mode=True)
    gated, msg = should_gate(0.3, gate)
    assert not gated
    assert "[shadow]" in msg
    assert "would gate" in msg


def test_should_gate_at_exact_threshold():
    gate = GateConfig(min_confidence=0.5, shadow_mode=False)
    gated, _ = should_gate(0.5, gate)
    assert not gated  # >= threshold passes


def test_load_config_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert isinstance(cfg, ConfidenceGatesConfig)
    assert cfg.observation_write.min_confidence == 0.3
    assert cfg.deep_reflection.min_separability == 0.2


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "test_gates.yaml"
    config_file.write_text(
        "observation_write:\n"
        "  min_confidence: 0.6\n"
        "  shadow_mode: false\n"
        "memory_upsertion:\n"
        "  min_confidence: 0.4\n"
        "deep_reflection:\n"
        "  min_confidence: 0.5\n"
        "  min_separability: 0.3\n"
        "  shadow_mode: false\n"
    )
    cfg = load_config(config_file)
    assert cfg.observation_write.min_confidence == 0.6
    assert cfg.observation_write.shadow_mode is False
    assert cfg.memory_upsertion.min_confidence == 0.4
    assert cfg.memory_upsertion.shadow_mode is True  # default
    assert cfg.deep_reflection.min_confidence == 0.5
    assert cfg.deep_reflection.min_separability == 0.3


def test_deep_reflection_gate_config():
    gate = DeepReflectionGateConfig(min_confidence=0.4, min_separability=0.3, shadow_mode=False)
    gated, msg = should_gate(0.2, gate)
    assert gated
    gated, msg = should_gate(0.5, gate)
    assert not gated
