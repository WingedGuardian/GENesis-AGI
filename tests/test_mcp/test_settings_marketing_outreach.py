"""marketing_outreach settings domain: registration + validator.

Mirrors ``test_settings_memory_recall.py``. The lever's ``mode`` accepts
``off``/``observe`` via ``settings_update``, but ``live`` is REJECTED — arming
autonomous cold sending is deliberately overlay-file-only so a foreground/injected
model cannot self-elevate past the observe gate via the model-reachable
``settings_update`` path (the owner arms live by editing
``config/marketing_outreach.local.yaml`` directly). Same shape as
``_validate_memory_recall``'s reservation of ``entity_lane.mode: live``.
"""

from __future__ import annotations

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_marketing_outreach,
)


def test_marketing_outreach_domain_registered():
    assert "marketing_outreach" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["marketing_outreach"]
    assert d.readonly is False
    assert "marketing_outreach" in _DOMAIN_VALIDATORS


def test_off_and_observe_are_settable():
    assert _validate_marketing_outreach({"mode": "off"}) == []
    assert _validate_marketing_outreach({"mode": "observe"}) == []
    assert _validate_marketing_outreach({"enabled": True}) == []
    assert _validate_marketing_outreach({"enabled": False, "mode": "observe"}) == []


def test_mode_live_is_rejected_via_settings():
    # The core of the fix: a settings_update can never flip mode to live.
    errs = _validate_marketing_outreach({"mode": "live"})
    assert errs
    assert "live" in errs[0]
    # The message must point the owner at the overlay file (the only live path).
    assert "marketing_outreach.local.yaml" in errs[0]


def test_mode_live_rejected_even_alongside_other_keys():
    # A whole-object write that includes mode=live is still rejected (the enabled
    # key alone would validate, but the live mode must not slip through).
    errs = _validate_marketing_outreach({"enabled": True, "mode": "live"})
    assert errs
    assert any("live" in e for e in errs)


def test_unknown_key_rejected():
    errs = _validate_marketing_outreach({"bogus": 1})
    assert errs and "bogus" in errs[0]


def test_enabled_must_be_bool():
    assert _validate_marketing_outreach({"enabled": "yes"})


def test_invalid_mode_rejected():
    errs = _validate_marketing_outreach({"mode": "banana"})
    assert errs and "banana" in errs[0]
