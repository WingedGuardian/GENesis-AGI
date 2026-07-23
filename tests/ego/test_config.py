"""Tests for the ego configuration loader."""

from __future__ import annotations

import pytest
import yaml

from genesis.ego.config import (
    load_ego_config,
    save_ego_config,
    validate_ego_config,
)
from genesis.ego.types import EgoConfig


@pytest.fixture
def tmp_config(tmp_path):
    """Return a temp config path."""
    return tmp_path / "ego.yaml"


class TestLoadEgoConfig:
    def test_returns_defaults_when_missing(self, tmp_path):
        config = load_ego_config(tmp_path / "nope.yaml")
        assert config.enabled is True
        assert config.cadence_minutes == 60
        assert config.model == "opus"
    def test_loads_from_yaml(self, tmp_config):
        tmp_config.write_text(yaml.dump({
            "cadence_minutes": 30,
            "model": "sonnet",
        }))
        config = load_ego_config(tmp_config)
        assert config.cadence_minutes == 30
        assert config.model == "sonnet"
        # Unspecified fields keep defaults
        assert config.enabled is True
        assert config.activity_threshold_minutes == 30

    def test_ignores_unknown_keys(self, tmp_config):
        tmp_config.write_text(yaml.dump({
            "cadence_minutes": 45,
            "unknown_field": "ignored",
        }))
        config = load_ego_config(tmp_config)
        assert config.cadence_minutes == 45
        assert not hasattr(config, "unknown_field")

    def test_handles_corrupt_yaml(self, tmp_config):
        tmp_config.write_text("{{invalid yaml")
        config = load_ego_config(tmp_config)
        # Falls back to defaults
        assert config.cadence_minutes == 60


class TestSaveEgoConfig:
    def test_roundtrip(self, tmp_config):
        original = EgoConfig(
            cadence_minutes=45,
            model="sonnet",
        )
        save_ego_config(original, tmp_config)

        loaded = load_ego_config(tmp_config)
        assert loaded.cadence_minutes == 45
        assert loaded.model == "sonnet"

    def test_creates_file(self, tmp_config):
        assert not tmp_config.exists()
        save_ego_config(EgoConfig(), tmp_config)
        assert tmp_config.exists()

    def test_file_has_header(self, tmp_config):
        save_ego_config(EgoConfig(), tmp_config)
        content = tmp_config.read_text()
        assert content.startswith("# Ego session configuration")


class TestValidateEgoConfig:
    def test_valid_changes(self):
        errors = validate_ego_config({
            "cadence_minutes": 30,
            "model": "sonnet",
        })
        assert errors == []

    def test_invalid_cadence(self):
        errors = validate_ego_config({"cadence_minutes": 0})
        assert len(errors) == 1
        assert "cadence_minutes" in errors[0]

    def test_invalid_model(self):
        errors = validate_ego_config({"model": "gpt-4"})
        assert len(errors) == 1
        assert "model" in errors[0]

    def test_invalid_morning_hour(self):
        errors = validate_ego_config({"morning_report_hour": 25})
        assert len(errors) == 1

    def test_invalid_board_size(self):
        errors = validate_ego_config({"board_size": 0})
        assert len(errors) == 1
        assert "board_size" in errors[0]

    def test_valid_board_size(self):
        errors = validate_ego_config({"board_size": 5})
        assert errors == []

    def test_no_proposal_expiry_validation(self):
        """proposal_expiry_minutes was removed — should not validate."""
        errors = validate_ego_config({"proposal_expiry_minutes": 240})
        assert errors == []  # unknown key, ignored

    def test_outcome_bus_capability_feed_rejects_non_bool(self):
        for bad in ("yes", 1, 0, None):
            errors = validate_ego_config({"outcome_bus_capability_feed": bad})
            assert len(errors) == 1
            assert "outcome_bus_capability_feed must be a boolean" in errors[0]

    def test_outcome_bus_capability_feed_accepts_bool(self):
        assert validate_ego_config({"outcome_bus_capability_feed": True}) == []
        assert validate_ego_config({"outcome_bus_capability_feed": False}) == []

    def test_calibration_injection_enabled_rejects_non_bool(self):
        # Sibling bool validator — equally untested before this PR.
        errors = validate_ego_config({"calibration_injection_enabled": "on"})
        assert len(errors) == 1
        assert "calibration_injection_enabled must be a boolean" in errors[0]

    def test_multiple_errors(self):
        errors = validate_ego_config({
            "cadence_minutes": -1,
            "model": "invalid",
        })
        assert len(errors) == 2

    def test_empty_changes_valid(self):
        assert validate_ego_config({}) == []

    def test_quiet_hours_enabled_rejects_non_bool(self):
        errors = validate_ego_config({"quiet_hours_enabled": "yes"})
        assert len(errors) == 1
        assert "quiet_hours_enabled must be a boolean" in errors[0]

    def test_quiet_hours_bounds(self):
        assert validate_ego_config({"quiet_hours_start": 23}) == []
        assert validate_ego_config({"quiet_hours_end": 0}) == []
        assert len(validate_ego_config({"quiet_hours_start": 24})) == 1
        assert len(validate_ego_config({"quiet_hours_end": -1})) == 1

    def test_quiet_hours_min_interval(self):
        assert validate_ego_config({"quiet_hours_min_interval_minutes": 240}) == []
        errors = validate_ego_config({"quiet_hours_min_interval_minutes": 0})
        assert len(errors) == 1
        assert "quiet_hours_min_interval_minutes must be >= 1" in errors[0]

    def test_quiet_hours_mode(self):
        assert validate_ego_config({"quiet_hours_mode": "floor"}) == []
        assert validate_ego_config({"quiet_hours_mode": "suppress"}) == []
        errors = validate_ego_config({"quiet_hours_mode": "loud"})
        assert len(errors) == 1
        assert "quiet_hours_mode must be" in errors[0]

    def test_quiet_hours_defaults(self):
        cfg = EgoConfig()
        assert cfg.quiet_hours_enabled is True
        assert cfg.quiet_hours_start == 23
        assert cfg.quiet_hours_end == 7
        assert cfg.quiet_hours_min_interval_minutes == 240

    def test_capability_improvement_enabled_rejects_non_bool(self):
        errors = validate_ego_config({"capability_improvement_enabled": "on"})
        assert len(errors) == 1
        assert "capability_improvement_enabled must be a boolean" in errors[0]

    def test_capability_weakness_threshold_bounds(self):
        assert validate_ego_config({"capability_weakness_threshold": 0.5}) == []
        assert validate_ego_config({"capability_weakness_threshold": 0.0}) == []
        assert validate_ego_config({"capability_weakness_threshold": 1.0}) == []
        assert len(validate_ego_config({"capability_weakness_threshold": 1.5})) == 1
        assert len(validate_ego_config({"capability_weakness_threshold": -0.1})) == 1
        # bool must not sneak through the numeric check
        assert len(validate_ego_config({"capability_weakness_threshold": True})) == 1

    def test_capability_min_sample_size_rejects_bad(self):
        assert validate_ego_config({"capability_improvement_min_sample_size": 3}) == []
        # negative would defeat the fluke guard (sample_size >= -1 is always true)
        assert len(validate_ego_config({"capability_improvement_min_sample_size": 0})) == 1
        assert len(validate_ego_config({"capability_improvement_min_sample_size": -1})) == 1
        assert len(validate_ego_config({"capability_improvement_min_sample_size": 2.5})) == 1

    def test_capability_max_signals_rejects_bad(self):
        assert validate_ego_config({"capability_improvement_max_signals": 3}) == []
        # negative LIMIT means "no limit" in SQLite — must be rejected
        assert len(validate_ego_config({"capability_improvement_max_signals": 0})) == 1
        assert len(validate_ego_config({"capability_improvement_max_signals": -1})) == 1

    def test_capability_improvement_defaults(self):
        cfg = EgoConfig()
        assert cfg.capability_improvement_enabled is True
        assert cfg.capability_weakness_threshold == 0.5
        assert cfg.capability_improvement_min_sample_size == 3
        assert cfg.capability_improvement_max_signals == 3


class TestCapabilityImprovementSettingsWiring:
    """The capability_improvement validators are reached by settings_update.

    Proves the settings_update('ego', {...}) path dispatches to
    validate_ego_config so an invalid capability field is rejected before it
    can reach ego.yaml (where e.g. a negative max_signals would uncap the
    SQLite LIMIT and silently disable the feature).
    """

    def test_ego_domain_dispatches_to_validate_ego_config(self):
        from genesis.mcp.health.settings import _DOMAIN_VALIDATORS

        assert "ego" in _DOMAIN_VALIDATORS
        validator = _DOMAIN_VALIDATORS["ego"]
        # Rejects a non-int max_signals (Codex's "many" case).
        assert validator({"capability_improvement_max_signals": "many"})
        # Rejects a negative max_signals (SQLite negative LIMIT = no limit).
        assert validator({"capability_improvement_max_signals": -1})
        # Rejects the truthy string "false" for the bool flag.
        assert validator({"capability_improvement_enabled": "false"})
        # Rejects an out-of-range threshold.
        assert validator({"capability_weakness_threshold": 2.0})
        # A fully-valid change set passes.
        assert validator({
            "capability_improvement_enabled": True,
            "capability_weakness_threshold": 0.4,
            "capability_improvement_min_sample_size": 5,
            "capability_improvement_max_signals": 2,
        }) == []


class TestMaxActiveEgoGoalsValidation:
    def test_valid(self):
        assert validate_ego_config({"max_active_ego_goals": 3}) == []
        assert validate_ego_config({"max_active_ego_goals": 0}) == []

    def test_invalid(self):
        assert validate_ego_config({"max_active_ego_goals": -1})
        assert validate_ego_config({"max_active_ego_goals": 2.5})
        assert validate_ego_config({"max_active_ego_goals": "5"})
