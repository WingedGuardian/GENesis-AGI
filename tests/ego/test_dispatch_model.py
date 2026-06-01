"""Tests for dispatch model override selection in ego proposal dispatch."""

from __future__ import annotations

import pytest
import yaml

from genesis.cc.types import CCModel
from genesis.ego.config import load_ego_config, validate_ego_config
from genesis.ego.session import _infer_profile
from genesis.ego.types import EgoConfig


class TestDispatchModelOverridesConfig:
    """EgoConfig.dispatch_model_overrides loading and validation."""

    def test_default_is_empty_dict(self):
        config = EgoConfig()
        assert config.dispatch_model_overrides == {}

    def test_loads_from_yaml(self, tmp_path):
        cfg = tmp_path / "ego.yaml"
        cfg.write_text(yaml.dump({
            "dispatch_model_overrides": {"investigate": "opus"},
        }))
        config = load_ego_config(cfg)
        assert config.dispatch_model_overrides == {"investigate": "opus"}

    def test_roundtrip_with_overrides(self, tmp_path):
        from genesis.ego.config import save_ego_config

        original = EgoConfig(
            dispatch_model_overrides={"investigate": "opus", "outreach": "sonnet"},
        )
        path = tmp_path / "ego.yaml"
        save_ego_config(original, path)
        loaded = load_ego_config(path)
        assert loaded.dispatch_model_overrides == {"investigate": "opus", "outreach": "sonnet"}

    def test_missing_key_defaults_empty(self, tmp_path):
        cfg = tmp_path / "ego.yaml"
        cfg.write_text(yaml.dump({"model": "opus"}))
        config = load_ego_config(cfg)
        assert config.dispatch_model_overrides == {}

    def test_null_value_defaults_to_empty_dict(self, tmp_path):
        """YAML null should not crash — falls back to default empty dict."""
        cfg = tmp_path / "ego.yaml"
        cfg.write_text("dispatch_model_overrides: null\n")
        config = load_ego_config(cfg)
        assert config.dispatch_model_overrides == {}
        # Must be callable with .get() — the crash site
        assert config.dispatch_model_overrides.get("investigate") is None


class TestDispatchModelOverridesValidation:
    """Validation of dispatch_model_overrides via validate_ego_config."""

    def test_valid_overrides(self):
        errors = validate_ego_config({
            "dispatch_model_overrides": {"investigate": "opus"},
        })
        assert errors == []

    def test_invalid_model_in_overrides(self):
        errors = validate_ego_config({
            "dispatch_model_overrides": {"investigate": "gpt-4"},
        })
        assert len(errors) == 1
        assert "dispatch_model_overrides" in errors[0]

    def test_not_a_dict(self):
        errors = validate_ego_config({
            "dispatch_model_overrides": "opus",
        })
        assert len(errors) == 1
        assert "must be a dict" in errors[0]

    def test_multiple_entries_one_invalid(self):
        errors = validate_ego_config({
            "dispatch_model_overrides": {
                "investigate": "opus",
                "outreach": "invalid",
            },
        })
        assert len(errors) == 1
        assert "outreach" in errors[0]


class TestModelSelectionLogic:
    """Test the model selection logic that _sweep_approved_inner uses.

    Extracted to a pure function test to avoid needing the full EgoSession.
    """

    @staticmethod
    def _select_model(action_type: str, overrides: dict) -> CCModel:
        """Replicate the model selection logic from _sweep_approved_inner."""
        profile = _infer_profile(action_type)
        model_override = overrides.get(action_type)
        if model_override:
            return CCModel(model_override)
        elif profile == "interact":
            return CCModel.OPUS
        else:
            return CCModel.SONNET

    def test_investigate_defaults_to_sonnet_without_override(self):
        assert self._select_model("investigate", {}) == CCModel.SONNET

    def test_investigate_with_opus_override(self):
        assert self._select_model("investigate", {"investigate": "opus"}) == CCModel.OPUS

    def test_interact_types_always_opus(self):
        # interact types get Opus regardless of overrides
        assert self._select_model("outreach", {}) == CCModel.OPUS
        assert self._select_model("dispatch", {}) == CCModel.OPUS
        assert self._select_model("publish", {}) == CCModel.OPUS

    def test_interact_override_to_sonnet(self):
        # An explicit override can downgrade interact types
        assert self._select_model("outreach", {"outreach": "sonnet"}) == CCModel.SONNET

    def test_observe_defaults_to_sonnet(self):
        assert self._select_model("monitor", {}) == CCModel.SONNET

    def test_observe_with_override(self):
        assert self._select_model("monitor", {"monitor": "opus"}) == CCModel.OPUS

    def test_empty_action_type(self):
        assert self._select_model("", {}) == CCModel.SONNET

    def test_override_haiku(self):
        assert self._select_model("investigate", {"investigate": "haiku"}) == CCModel.HAIKU
