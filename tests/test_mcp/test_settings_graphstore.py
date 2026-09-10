"""graphstore settings domain: registration + the effective-state precondition.

The socket precondition is the interesting part. It exists so a dashboard toggle
whose real meaning is "log an error on every recall" cannot be made by accident —
but judging it from the INCOMING KEYS rather than the resulting state was wrong
in both directions at once, and the two callers hit one each. These tests pin the
resulting-state reading, because the two failure modes are silent in opposite
ways: one traps an install in a mode it cannot leave, the other lets it enter one
it cannot serve.
"""

from __future__ import annotations

from pathlib import Path

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_graphstore,
)


def test_graphstore_domain_registered():
    assert "graphstore" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["graphstore"]
    assert d.config_filename == "graphstore.yaml"
    assert "graphstore" in _DOMAIN_VALIDATORS


def _stored(monkeypatch, cfg: dict) -> None:
    """Pin what the CURRENT persisted config says."""
    monkeypatch.setattr(
        "genesis.memory.graphstore_config.load_config", lambda: dict(cfg), raising=True
    )


def _socket(monkeypatch, *, present: bool, tmp_path: Path) -> None:
    path = tmp_path / "falkordb.sock"
    if present:
        path.touch()
    # Patch the SOURCE, not `settings_mod`: the validator imports
    # `falkordb_socket_path` inside the function body, so the name never exists
    # as a module attribute here and patching it would create a dead one.
    monkeypatch.setattr("genesis.env.falkordb_socket_path", lambda: str(path), raising=True)


def test_shape_errors_still_fire(monkeypatch, tmp_path):
    _stored(monkeypatch, {"enabled": True, "mode": "networkx"})
    _socket(monkeypatch, present=True, tmp_path=tmp_path)

    assert _validate_graphstore({"nonsense": 1}), "an unknown key must be rejected"
    assert _validate_graphstore({"enabled": "yes"}), "'enabled' must be a real boolean"
    assert _validate_graphstore({"mode": "sideways"}), "an unrecognised mode must be rejected"


def test_selecting_falkordb_without_the_socket_is_refused(monkeypatch, tmp_path):
    """The precondition this validator exists for, unchanged."""
    _stored(monkeypatch, {"enabled": True, "mode": "networkx"})
    _socket(monkeypatch, present=False, tmp_path=tmp_path)

    errors = _validate_graphstore({"mode": "falkordb"})
    assert errors and "not armed" in errors[0]


def test_disabling_is_allowed_even_when_the_socket_has_gone(monkeypatch, tmp_path):
    """The dashboard direction. It submits the WHOLE current config, so turning
    the lever off still sends `mode: falkordb` — and refusing that save trapped
    an install in a mode it could no longer leave, which is the opposite of what
    the precondition is for.
    """
    _stored(monkeypatch, {"enabled": True, "mode": "falkordb"})
    _socket(monkeypatch, present=False, tmp_path=tmp_path)

    assert _validate_graphstore({"enabled": False, "mode": "falkordb"}) == [], (
        "disabling the lever must never require the engine it is disabling"
    )


def test_enabling_against_a_stored_falkordb_mode_still_needs_the_socket(monkeypatch, tmp_path):
    """The MCP direction. A partial update carrying only `enabled` used to skip
    the check entirely, so the lever could be armed onto an absent engine by an
    update that never mentioned `mode`.
    """
    _stored(monkeypatch, {"enabled": False, "mode": "falkordb"})
    _socket(monkeypatch, present=False, tmp_path=tmp_path)

    errors = _validate_graphstore({"enabled": True})
    assert errors and "not armed" in errors[0], (
        "the effective post-update state selects falkordb, so the socket is required"
    )


def test_enabling_against_a_stored_falkordb_mode_passes_when_armed(monkeypatch, tmp_path):
    """The same path must not become a blanket refusal — that would be a new bug
    wearing the fix's clothes.
    """
    _stored(monkeypatch, {"enabled": False, "mode": "falkordb"})
    _socket(monkeypatch, present=True, tmp_path=tmp_path)

    assert _validate_graphstore({"enabled": True}) == []


def test_a_networkx_install_never_consults_the_socket(monkeypatch, tmp_path):
    """The default install has no engine and must not be told it needs one."""
    _stored(monkeypatch, {"enabled": True, "mode": "networkx"})
    _socket(monkeypatch, present=False, tmp_path=tmp_path)

    assert _validate_graphstore({"enabled": False}) == []
    assert _validate_graphstore({"mode": "networkx"}) == []
