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
        assert config.daily_budget_cap_usd == 10.0

    def test_loads_from_yaml(self, tmp_config):
        tmp_config.write_text(yaml.dump({
            "cadence_minutes": 30,
            "model": "sonnet",
            "daily_budget_cap_usd": 5.0,
        }))
        config = load_ego_config(tmp_config)
        assert config.cadence_minutes == 30
        assert config.model == "sonnet"
        assert config.daily_budget_cap_usd == 5.0
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
            daily_budget_cap_usd=5.0,
        )
        save_ego_config(original, tmp_config)

        loaded = load_ego_config(tmp_config)
        assert loaded.cadence_minutes == 45
        assert loaded.model == "sonnet"
        assert loaded.daily_budget_cap_usd == 5.0

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
            "daily_budget_cap_usd": 5.0,
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

    def test_invalid_budget(self):
        errors = validate_ego_config({"daily_budget_cap_usd": -1})
        assert len(errors) == 1

    def test_invalid_morning_hour(self):
        errors = validate_ego_config({"morning_report_hour": 25})
        assert len(errors) == 1

    def test_multiple_errors(self):
        errors = validate_ego_config({
            "cadence_minutes": -1,
            "model": "invalid",
            "daily_budget_cap_usd": -5,
        })
        assert len(errors) == 3

    def test_empty_changes_valid(self):
        assert validate_ego_config({}) == []
