"""ledger_escalation settings validator (undisposed-ledger escalation sweep)."""

from __future__ import annotations

from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS

_validate = _DOMAIN_VALIDATORS["ledger_escalation"]


def test_domain_registered_with_config_file():
    dom = _DOMAIN_REGISTRY["ledger_escalation"]
    assert dom.config_filename == "ledger_escalation.yaml"
    assert dom.readonly is False
    assert dom.needs_restart is False


def test_valid_changes_pass():
    assert _validate({"enabled": True}) == []
    assert _validate({"stale_days": 7, "quiet_days": 3}) == []
    assert _validate({"max_per_run": 10, "priority": "critical"}) == []
    assert _validate({"escalate_added_by": ["foreground", "pulse"]}) == []


def test_unknown_key_rejected_and_lists_valid():
    (err,) = _validate({"bogus": 1})
    assert "Unknown key" in err
    assert "enabled" in err and "stale_days" in err


def test_enabled_must_be_bool():
    assert _validate({"enabled": "yes"}) != []
    assert _validate({"enabled": 1}) != []


def test_knobs_must_be_positive_int():
    assert _validate({"max_per_run": 0}) != []
    assert _validate({"stale_days": -1}) != []
    assert _validate({"quiet_days": True}) != []  # bool is not a valid int
    assert _validate({"stale_days": 1.5}) != []


def test_priority_must_be_a_known_level():
    assert _validate({"priority": "urgent"}) != []
    assert _validate({"priority": "high"}) == []


def test_escalate_added_by_must_be_a_non_empty_list_of_known_provenance():
    """The allow-list gates who may create work for a human, so an unknown
    value must be REFUSED rather than silently matching nothing."""
    assert _validate({"escalate_added_by": []}) != []
    assert _validate({"escalate_added_by": "foreground"}) != []
    (err,) = _validate({"escalate_added_by": ["foreground", "nonsense"]})
    assert "nonsense" in err
    assert "foreground" in err  # the message names what IS valid
