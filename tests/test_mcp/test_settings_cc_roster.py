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


def test_validate_accepts_native_default():
    # claude is native — no auth_env needed.
    assert _validate_cc_roster({"default": "claude"}) == []


# Hermetic peer. The shipped config deliberately contains NO peers (they are
# install-specific, configured in ~/.genesis/config/cc_roster.local.yaml), so a
# test must supply its own. `_validate_cc_roster` merges `changes["models"]` over
# the loaded roster before validating, which is exactly the "add a peer and make
# it default in one call" path — so this exercises more than the old version did.
_TEST_PEER = {
    "test-peer": {
        "anthropic_base_url": "https://example.invalid/api/anthropic",
        "model_id": "test-model",
        "auth_env": "GENESIS_TEST_ROSTER_KEY",
    }
}


def test_validate_accepts_routed_default_when_auth_present(monkeypatch):
    monkeypatch.setenv("GENESIS_TEST_ROSTER_KEY", "sk-test")
    assert _validate_cc_roster({"default": "test-peer", "models": _TEST_PEER}) == []


def test_validate_rejects_routed_default_when_auth_missing(monkeypatch):
    # no-silent-degrade: setting a routed default with no key must be loud.
    monkeypatch.delenv("GENESIS_TEST_ROSTER_KEY", raising=False)
    errs = _validate_cc_roster({"default": "test-peer", "models": _TEST_PEER})
    assert errs and "GENESIS_TEST_ROSTER_KEY" in errs[0]


def test_validate_rejects_unknown_default():
    errs = _validate_cc_roster({"default": "nonexistent-model"})
    assert errs and "not a roster model" in errs[0]


def test_validate_rejects_nonstring_default():
    assert _validate_cc_roster({"default": 123})


def test_validate_ignores_unrelated_changes():
    assert _validate_cc_roster({"models": {"x": {}}}) == []
