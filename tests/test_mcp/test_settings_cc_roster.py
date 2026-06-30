"""cc_roster settings domain: registration + default validator (architect P3-D)."""
from __future__ import annotations

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_cc_roster,
)


def test_cc_roster_domain_registered():
    assert "cc_roster" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["cc_roster"]
    assert d.readonly is False
    assert d.needs_restart is False  # read live per-invocation
    assert d.config_filename == "cc_roster.yaml"
    assert "cc_roster" in _DOMAIN_VALIDATORS


def test_validate_accepts_known_default():
    # config/cc_roster.yaml ships claude + glm-5.2.
    assert _validate_cc_roster({"default": "claude"}) == []
    assert _validate_cc_roster({"default": "glm-5.2"}) == []


def test_validate_rejects_unknown_default():
    errs = _validate_cc_roster({"default": "nonexistent-model"})
    assert errs and "not a roster model" in errs[0]


def test_validate_rejects_nonstring_default():
    assert _validate_cc_roster({"default": 123})


def test_validate_ignores_unrelated_changes():
    assert _validate_cc_roster({"models": {"x": {}}}) == []
