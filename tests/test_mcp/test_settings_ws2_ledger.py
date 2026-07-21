"""ws2_ledger settings validator + arbitration_mode reader (WS-2 P2b/P4)."""

from __future__ import annotations

import pytest

from genesis.ledger import ws2_ledger_config as cfg_mod
from genesis.mcp.health.settings import _DOMAIN_VALIDATORS

_validate = _DOMAIN_VALIDATORS["ws2_ledger"]


class TestValidator:
    def test_valid_changes_pass(self):
        assert _validate({"enabled": True, "autonomy_feed": "shadow"}) == []
        assert _validate({"arbitration": "off"}) == []
        assert _validate({"arbitration": "shadow"}) == []
        assert _validate({"arbitration": "enforce"}) == []

    def test_unknown_key_rejected_and_lists_arbitration(self):
        (err,) = _validate({"bogus": 1})
        assert "arbitration" in err and "Unknown key" in err

    def test_arbitration_rejects_non_modes(self):
        (err,) = _validate({"arbitration": "live"})  # live is autonomy_feed-only
        assert "arbitration" in err
        assert _validate({"arbitration": True}) != []

    def test_autonomy_feed_rejects_enforce(self):
        (err,) = _validate({"autonomy_feed": "enforce"})  # enforce is arbitration-only
        assert "autonomy_feed" in err

    def test_enabled_must_be_bool(self):
        (err,) = _validate({"enabled": "yes"})
        assert "boolean" in err


class TestArbitrationModeReader:
    @pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
    def test_valid_modes_pass_through(self, monkeypatch, mode):
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"enabled": True, "arbitration": mode}
        )
        assert cfg_mod.arbitration_mode() == mode

    def test_master_disabled_forces_off(self, monkeypatch):
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"enabled": False, "arbitration": "enforce"}
        )
        assert cfg_mod.arbitration_mode() == "off"

    def test_invalid_degrades_to_shadow(self, monkeypatch):
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"enabled": True, "arbitration": "banana"}
        )
        assert cfg_mod.arbitration_mode() == "shadow"

    def test_missing_key_degrades_to_shadow(self, monkeypatch):
        monkeypatch.setattr(cfg_mod, "load_config", lambda: {"enabled": True})
        assert cfg_mod.arbitration_mode() == "shadow"

    def test_yaml_false_means_off(self, monkeypatch):
        # Unquoted `arbitration: off` parses as YAML-1.1 boolean False.
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"enabled": True, "arbitration": False}
        )
        assert cfg_mod.arbitration_mode() == "off"

    def test_shipped_default_is_shadow(self):
        assert cfg_mod.DEFAULTS["arbitration"] == "shadow"
