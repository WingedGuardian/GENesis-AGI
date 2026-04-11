"""Tests for the updates settings domain validator.

Verifies the policy guards (no auto-apply of unsafe impacts, type
strictness, range bounds) added in the post-merge fix.
"""

from __future__ import annotations

from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _validate_updates


def test_domain_registered() -> None:
    """The 'updates' domain should be in the settings registry."""
    assert "updates" in _DOMAIN_REGISTRY
    domain = _DOMAIN_REGISTRY["updates"]
    assert domain.config_filename == "updates.yaml"
    assert domain.readonly is False


class TestValidChanges:

    def test_full_valid_config(self) -> None:
        errors = _validate_updates({
            "check": {"enabled": True, "interval_hours": 6},
            "notify": {"enabled": True, "channel": "telegram"},
            "auto_apply": {"enabled": False, "allowed_impacts": ["none", "informational"]},
            "backup_before_update": True,
        })
        assert errors == []

    def test_partial_change(self) -> None:
        errors = _validate_updates({"check": {"enabled": False}})
        assert errors == []

    def test_interval_at_bounds(self) -> None:
        assert _validate_updates({"check": {"interval_hours": 1}}) == []
        assert _validate_updates({"check": {"interval_hours": 168}}) == []


class TestSectionTypeStrict:
    """Non-dict sections must be rejected, not silently accepted."""

    def test_non_dict_check_rejected(self) -> None:
        errors = _validate_updates({"check": "broken"})
        assert any("check must be a mapping" in e for e in errors)

    def test_non_dict_notify_rejected(self) -> None:
        errors = _validate_updates({"notify": []})
        assert any("notify must be a mapping" in e for e in errors)

    def test_non_dict_auto_apply_rejected(self) -> None:
        errors = _validate_updates({"auto_apply": 42})
        assert any("auto_apply must be a mapping" in e for e in errors)


class TestUnsafeAutoApplyImpacts:
    """auto_apply.allowed_impacts must reject action_needed and breaking."""

    def test_breaking_impact_rejected(self) -> None:
        errors = _validate_updates({"auto_apply": {"allowed_impacts": ["breaking"]}})
        assert any("'breaking' not allowed" in e for e in errors)

    def test_action_needed_impact_rejected(self) -> None:
        errors = _validate_updates({"auto_apply": {"allowed_impacts": ["action_needed"]}})
        assert any("'action_needed' not allowed" in e for e in errors)

    def test_safe_impacts_allowed(self) -> None:
        errors = _validate_updates({
            "auto_apply": {"allowed_impacts": ["none", "informational"]},
        })
        assert errors == []

    def test_mixed_safe_and_unsafe_partial_reject(self) -> None:
        errors = _validate_updates({
            "auto_apply": {"allowed_impacts": ["none", "breaking"]},
        })
        assert len(errors) == 1
        assert "'breaking' not allowed" in errors[0]


class TestRangeAndTypeChecks:

    def test_interval_below_range(self) -> None:
        errors = _validate_updates({"check": {"interval_hours": 0}})
        assert any("between 1 and 168" in e for e in errors)

    def test_interval_above_range(self) -> None:
        errors = _validate_updates({"check": {"interval_hours": 200}})
        assert any("between 1 and 168" in e for e in errors)

    def test_interval_non_integer(self) -> None:
        errors = _validate_updates({"check": {"interval_hours": "many"}})
        assert any("must be an integer" in e for e in errors)

    def test_enabled_must_be_bool(self) -> None:
        errors = _validate_updates({"check": {"enabled": "yes"}})
        assert any("check.enabled must be a boolean" in e for e in errors)

    def test_unknown_top_key_rejected(self) -> None:
        errors = _validate_updates({"bogus": True})
        assert any("Unknown key 'bogus'" in e for e in errors)

    def test_invalid_channel_rejected(self) -> None:
        errors = _validate_updates({"notify": {"channel": "discord"}})
        assert any("must currently be 'telegram'" in e for e in errors)

    def test_backup_before_update_must_be_bool(self) -> None:
        errors = _validate_updates({"backup_before_update": "always"})
        assert any("backup_before_update must be a boolean" in e for e in errors)
