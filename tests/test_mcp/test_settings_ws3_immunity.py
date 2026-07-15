"""ws3_immunity settings domain: registration + validator (WS-3 B0).

Validator-level coverage only, mirroring test_settings_cc_roster.py — that
file (the model for this one) does not exercise settings_update either, so
the merged-preview/dry_run path is covered by the shared settings_update
tests rather than duplicated per-domain here.
"""
from __future__ import annotations

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_ws3_immunity,
)


def test_ws3_immunity_domain_registered():
    assert "ws3_immunity" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["ws3_immunity"]
    assert d.readonly is False
    assert d.needs_restart is False  # read live per-call by genesis.security.immunity
    assert d.config_filename == "ws3_immunity.yaml"
    assert "auto_demote_state" in d.hidden_fields
    assert "ws3_immunity" in _DOMAIN_VALIDATORS


def test_validate_rejects_unknown_top_key():
    errs = _validate_ws3_immunity({"procedures": {"mode": "shadow"}})
    assert errs and "Unknown key 'procedures'" in errs[0]


def test_validate_rejects_invalid_gate_mode():
    errs = _validate_ws3_immunity({"procedure": {"mode": "block"}})
    assert errs and "'procedure.mode'" in errs[0]


def test_validate_rejects_non_mapping_gate():
    assert _validate_ws3_immunity({"identity": "enforce"})


def test_validate_rejects_non_bool_enabled():
    errs = _validate_ws3_immunity({"enabled": "false"})
    assert errs and "'enabled' must be a boolean" in errs[0]


def test_validate_rejects_non_positive_auto_demote_ints():
    assert _validate_ws3_immunity({"auto_demote": {"window_minutes": 0}})
    assert _validate_ws3_immunity({"auto_demote": {"would_block_threshold": -1}})
    assert _validate_ws3_immunity({"auto_demote": {"window_minutes": "60"}})


def test_validate_accepts_master_off():
    assert _validate_ws3_immunity({"enabled": False}) == []


def test_validate_accepts_gate_enforce():
    # WS-3 B4 honesty guard: enforce is accepted ONLY for the gates whose
    # enforce branch is built (autonomy + injection). procedure/identity have
    # no enforce path, so accepting mode=enforce would let the config lie.
    assert _validate_ws3_immunity({"injection": {"mode": "enforce"}}) == []
    assert _validate_ws3_immunity({"autonomy": {"mode": "enforce"}}) == []


def test_validate_rejects_enforce_for_unimplemented_gates():
    for gate in ("procedure", "identity"):
        errors = _validate_ws3_immunity({gate: {"mode": "enforce"}})
        assert errors and "does not implement enforce" in errors[0]
    # shadow / off remain valid for those gates.
    assert _validate_ws3_immunity({"procedure": {"mode": "shadow"}}) == []
    assert _validate_ws3_immunity({"identity": {"mode": "off"}}) == []


def test_validate_accepts_auto_demote_state_passthrough():
    # Written by immunity.record_demotion via the same overlay; accepted
    # opaquely (hidden from the UI).
    changes = {
        "auto_demote_state": {
            "identity": {
                "demoted_at": "2026-07-10T00:00:00+00:00",
                "from_mode": "enforce",
                "reason": "would-block spike",
            }
        }
    }
    assert _validate_ws3_immunity(changes) == []


def test_validate_accepts_valid_auto_demote():
    assert _validate_ws3_immunity(
        {"auto_demote": {"enabled": True, "window_minutes": 30,
                         "would_block_threshold": 3}}
    ) == []


def test_validate_rejects_bool_for_auto_demote_ints():
    # bool is an int subclass in Python — {"window_minutes": true} would
    # otherwise validate and write `window_minutes: true` to the overlay.
    errors = _validate_ws3_immunity({"auto_demote": {"window_minutes": True}})
    assert errors and "positive int" in errors[0]
