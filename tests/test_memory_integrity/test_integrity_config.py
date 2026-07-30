"""memory_integrity_config mode-lever + knob-coercion tests (sync)."""

from __future__ import annotations

from genesis.memory import integrity_config as ic


def test_default_mode_is_passive(monkeypatch):
    # PR-1 ships the repair lane but keeps the DEFAULT passive until the
    # follow-up adds the per-id lock. Both DEFAULTS and the repo yaml agree.
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    assert ic.DEFAULTS["mode"] == "passive"
    assert ic.effective_mode() == "passive"


def test_active_mode_honored_not_coerced(monkeypatch):
    # The KEY PR-1 capability: 'active' is fully implemented and must run as
    # 'active' when explicitly set (pre-Phase-1 it was coerced to passive).
    monkeypatch.delenv(ic._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(ic, "load_config", lambda: {**ic.DEFAULTS, "mode": "active"})
    assert ic.effective_mode() == "active"


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


def test_settings_validator_rejects_invalid():
    """settings_update must reject bad writes instead of silently persisting them
    (P2) — e.g. a string 'false' that is truthy at runtime."""
    from genesis.mcp.health.settings import _validate_memory_integrity

    assert _validate_memory_integrity({"mode": "bogus"})  # bad enum
    assert _validate_memory_integrity({"enabled": "false"})  # string, not bool
    assert _validate_memory_integrity({"rerank": "false"})  # string, not bool
    assert _validate_memory_integrity({"unknown_key": 1})  # unknown key
    assert _validate_memory_integrity({"drift_band": 5.0})  # out of 0..1
    assert _validate_memory_integrity({"severe_min_count": -1})  # not positive
    # a fully-valid change set passes
    assert not _validate_memory_integrity(
        {
            "enabled": True,
            "mode": "passive",
            "rerank": False,
            "drift_band": 0.2,
            "severe_min_count": 5,
            "rerank_timeout_s": 10.0,
        }
    )


def test_defaults_are_fresh_install_safe():
    # A fresh clone with no overlay resolves to passive (repair opt-in until the
    # follow-up) + sane knobs. The repo yaml and DEFAULTS must agree.
    cfg = ic.load_config()
    assert cfg["enabled"] is True
    assert cfg["mode"] == "passive"
    assert cfg["sample_fraction"] == 1.0
    assert cfg["repair_min_age_seconds"] == 3600
    assert cfg["max_repairs_per_run"] == 500
