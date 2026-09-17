"""repo_pulse control surface: live-read mode lever + knobs + settings
domain registration/validator (session-manager PR-4a, ledger_shadow lineage)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_repo_pulse,
)
from genesis.session_awareness import repo_pulse_config as rpc


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(rpc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return (
        repo_dir / "config" / "repo_pulse.yaml",
        user_dir / "repo_pulse.local.yaml",
    )


# ── effective_mode ───────────────────────────────────────────────────────


def test_defaults_live_when_no_config(config_dirs):
    """Live is the default: the lever only gates the reversible exact tier."""
    assert rpc.effective_mode() == "live"


def test_off_via_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: 'off'\n")
    assert rpc.effective_mode() == "off"


def test_off_via_unquoted_yaml_bool(config_dirs):
    """A hand-edited unquoted `mode: off` parses as YAML False — honored."""
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert rpc.effective_mode() == "off"


def test_master_enabled_false_wins(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: false\nmode: live\n")
    assert rpc.effective_mode() == "off"


def test_propose_only_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: propose_only\n")
    assert rpc.effective_mode() == "propose_only"


def test_invalid_mode_degrades_to_propose_only(config_dirs, caplog):
    """Invalid config degrades toward LESS write authority — never a silent
    off (dead pulse), never a silent live."""
    base, _ = config_dirs
    base.write_text("mode: bananas\n")
    with caplog.at_level("WARNING"):
        assert rpc.effective_mode() == "propose_only"
    assert any("propose_only" in r.message for r in caplog.records)


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("mode: live\n")
    overlay.write_text("mode: 'off'\n")
    assert rpc.effective_mode() == "off"


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("{{{{not yaml")
    assert rpc.effective_mode() == "live"


def test_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert rpc.effective_mode() == "live"
    base.write_text("mode: 'off'\n")
    assert rpc.effective_mode() == "off"  # takes effect on the very next call


# ── knob accessors ───────────────────────────────────────────────────────


def test_knobs_from_config_and_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("min_interval_minutes: 5\nmax_prs: 100\n")
    cfg = rpc.load_config()
    assert rpc.knob_int(cfg, "min_interval_minutes") == 5
    assert rpc.knob_int(cfg, "max_prs") == 100
    assert rpc.knob_int(cfg, "lookback_days") == rpc.DEFAULTS["lookback_days"]
    assert rpc.knob_float01(cfg, "inject_confidence_floor") == 0.7


def test_knobs_reject_garbage_toward_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text(
        "min_interval_minutes: -3\nmax_prs: 'many'\nlookback_days: true\n"
        "inject_confidence_floor: 1.5\n"
    )
    cfg = rpc.load_config()
    assert rpc.knob_int(cfg, "min_interval_minutes") == rpc.DEFAULTS["min_interval_minutes"]
    assert rpc.knob_int(cfg, "max_prs") == rpc.DEFAULTS["max_prs"]
    assert rpc.knob_int(cfg, "lookback_days") == rpc.DEFAULTS["lookback_days"]
    assert rpc.knob_float01(cfg, "inject_confidence_floor") == 0.7


# ── settings domain ──────────────────────────────────────────────────────


def test_domain_registered():
    assert "repo_pulse" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["repo_pulse"]
    assert d.readonly is False
    assert d.needs_restart is False  # each worker run is a fresh process
    assert d.config_filename == "repo_pulse.yaml"
    assert "repo_pulse" in _DOMAIN_VALIDATORS


def test_validator_accepts_valid_changes():
    assert _validate_repo_pulse({"enabled": False}) == []
    assert _validate_repo_pulse({"mode": "off"}) == []
    assert _validate_repo_pulse({"mode": "propose_only"}) == []
    assert _validate_repo_pulse({"mode": "live"}) == []
    assert _validate_repo_pulse({"min_interval_minutes": 60, "max_prs": 300}) == []
    assert _validate_repo_pulse({"inject_confidence_floor": 0.5}) == []


def test_validator_rejects_bad_values():
    assert _validate_repo_pulse({"mode": "shadow"})  # not a pulse mode
    assert _validate_repo_pulse({"enabled": "false"})
    assert _validate_repo_pulse({"min_interval_minutes": 0})
    assert _validate_repo_pulse({"max_prs": True})
    assert _validate_repo_pulse({"inject_confidence_floor": 1.5})
    assert _validate_repo_pulse({"bogus": 1})


def test_base_config_file_is_valid_live():
    """The shipped config/repo_pulse.yaml parses and matches DEFAULTS."""
    import yaml

    base = Path(__file__).parents[2] / "config" / "repo_pulse.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg == rpc.DEFAULTS


# ── Open-PR lane knobs (session-manager PR-4c) ──────────────────────────────


def test_open_pr_knob_defaults():
    cfg = dict(rpc.DEFAULTS)
    assert cfg["open_pr_enabled"] is True
    assert rpc.knob_int(cfg, "open_pr_stale_days") == 7
    assert rpc.knob_int(cfg, "max_open_prs") == 50
    assert rpc.knob_int(cfg, "open_pr_resurface_days") == 7
    assert rpc.knob_int(cfg, "open_pr_max_surface") == 5


def test_validator_accepts_open_pr_knobs():
    assert _validate_repo_pulse({"open_pr_enabled": True}) == []
    assert _validate_repo_pulse({"open_pr_enabled": False}) == []
    assert _validate_repo_pulse({"open_pr_stale_days": 3, "max_open_prs": 100}) == []
    assert _validate_repo_pulse({"open_pr_resurface_days": 5, "open_pr_max_surface": 3}) == []


def test_validator_rejects_bad_open_pr_knobs():
    assert _validate_repo_pulse({"open_pr_enabled": "yes"})  # bool required
    assert _validate_repo_pulse({"open_pr_stale_days": True})  # bool is not a valid int
    assert _validate_repo_pulse({"max_open_prs": 0})  # must be positive
    assert _validate_repo_pulse({"open_pr_max_surface": -1})


# ── knob_bool: a mistyped master switch must not fail SILENTLY ───────────────
#
# The bare `cfg.get(key, True)` this replaces is a truthiness test, and the
# values an operator actually mistypes are truthy STRINGS: quoted "false", "no"
# and "off" all read as ENABLED, so the switch does the opposite of what was
# asked and says nothing at all. The fix is not to guess the intent — it is to
# stop being silent, matching this module's stated rule for an invalid `mode`
# ("degrades ... never silently off").


def test_knob_bool_passes_real_booleans_through():
    assert rpc.knob_bool({"verification_enabled": False}, "verification_enabled") is False
    assert rpc.knob_bool({"verification_enabled": True}, "verification_enabled") is True


@pytest.mark.parametrize("truthy_string", ["false", "no", "off", "0", "None"])
def test_knob_bool_warns_instead_of_silently_enabling(truthy_string, caplog):
    """THE defect: every one of these is truthy, so the old check enabled the lane."""
    assert bool(truthy_string) is True, "precondition: the mistyped value IS truthy"

    with caplog.at_level(logging.WARNING):
        result = rpc.knob_bool({"verification_enabled": truthy_string}, "verification_enabled")

    assert result is rpc.DEFAULTS["verification_enabled"]
    assert "not a boolean" in caplog.text, "a mistyped master switch must say so"
    assert truthy_string in caplog.text, "the warning must quote the offending value"


def test_knob_bool_is_silent_when_the_key_is_absent(caplog):
    """An unset knob is not a mistake — only a PRESENT wrong-typed one warns."""
    with caplog.at_level(logging.WARNING):
        assert rpc.knob_bool({}, "verification_enabled") is True
    assert "not a boolean" not in caplog.text, "an absent key must not warn on every run"


# ── The bool-knob registry is a CHOKEPOINT, not a convention ────────────────
# A lane switch shipped that `config/repo_pulse.yaml` advertised and the
# settings API rejected as an unknown key: the validator carried its own
# hand-written tuple of boolean names and nobody updated it (Codex P2, #1836).
# The population fix is one shared `_BOOL_KNOBS`; these lock it, so adding a
# bool to DEFAULTS without registering it fails HERE rather than at an
# operator's keyboard.


def test_every_boolean_default_is_a_registered_bool_knob():
    """VERIFY-RED: drop any name from `_BOOL_KNOBS` and this fails."""
    bool_defaults = {k for k, v in rpc.DEFAULTS.items() if isinstance(v, bool)}
    assert bool_defaults == set(rpc._BOOL_KNOBS), (
        "every boolean knob in DEFAULTS must be registered in _BOOL_KNOBS — "
        "the settings validator derives its valid keys from it"
    )


def test_the_settings_validator_accepts_every_registered_bool_knob():
    """The lever the config file advertises must be operable through the API."""
    for knob in rpc._BOOL_KNOBS:
        assert _validate_repo_pulse({knob: False}) == [], f"{knob} must be settable"
        assert _validate_repo_pulse({knob: True}) == []


def test_the_settings_validator_still_rejects_a_non_boolean():
    """Registering a key must not weaken its TYPE check."""
    errors = _validate_repo_pulse({"verification_enabled": "false"})
    assert errors and "must be a boolean" in errors[0]


def test_the_settings_validator_still_rejects_an_unknown_key():
    errors = _validate_repo_pulse({"no_such_knob": True})
    assert errors and "Unknown key" in errors[0]
