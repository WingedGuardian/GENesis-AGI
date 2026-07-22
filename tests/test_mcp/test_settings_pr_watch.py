"""pr_watch settings validator (PR-watch inline surface lever)."""

from __future__ import annotations

from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS

_validate = _DOMAIN_VALIDATORS["pr_watch"]


def test_domain_registered_with_config_file():
    dom = _DOMAIN_REGISTRY["pr_watch"]
    assert dom.config_filename == "pr_watch.yaml"
    assert dom.readonly is False
    assert dom.needs_restart is False


def test_valid_changes_pass():
    assert _validate({"enabled": True}) == []
    assert _validate({"enabled": False, "resurface_days": 5}) == []
    assert _validate({"lookback_days": 30, "max_surface": 3}) == []


def test_unknown_key_rejected_and_lists_valid():
    (err,) = _validate({"bogus": 1})
    assert "Unknown key" in err
    assert "enabled" in err and "resurface_days" in err


def test_enabled_must_be_bool():
    assert _validate({"enabled": "yes"}) != []
    assert _validate({"enabled": 1}) != []


def test_knobs_must_be_positive_int():
    assert _validate({"max_surface": 0}) != []
    assert _validate({"lookback_days": -1}) != []
    assert _validate({"resurface_days": True}) != []  # bool is not a valid int
    assert _validate({"resurface_days": 1.5}) != []
