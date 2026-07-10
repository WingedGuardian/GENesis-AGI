"""WS-3 immunity control surface — live-read kill switch + gate helpers.

Covers ``genesis.security.immunity``: defaults with no config at all, master
short-circuit, per-gate modes, live re-read (NO cache — an overlay edit takes
effect on the very next call in the same process), the never-block-owner
invariant, and the auto-demote scaffold.

Both config locations are redirected to tmp_path:
- the base file  → monkeypatched ``immunity.repo_root`` (bound name in the
  module namespace via ``from genesis.env import repo_root``)
- the overlay    → ``_user_config_dir``, monkeypatched in BOTH namespaces
  that resolve it: ``genesis._config_overlay`` (used by
  ``merge_local_overlay`` inside ``load_immunity_config``) and
  ``genesis.security.immunity`` (bound import, used by ``_overlay_path`` /
  ``record_demotion``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from genesis.security import immunity


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(immunity, "repo_root", lambda: repo_dir)
    monkeypatch.setattr(
        "genesis._config_overlay._user_config_dir", lambda: user_dir,
    )
    monkeypatch.setattr(immunity, "_user_config_dir", lambda: user_dir)

    return (
        repo_dir / "config" / "ws3_immunity.yaml",
        user_dir / "ws3_immunity.local.yaml",
    )


def _write(path: Path, data: dict) -> None:
    # yaml.safe_dump quotes 'off' so it round-trips as the string "off",
    # not YAML 1.1 boolean False.
    path.write_text(yaml.safe_dump(data))


# ─── defaults / failure posture ──────────────────────────────────────────────


def test_defaults_with_no_files_at_all(config_dirs):
    cfg = immunity.load_immunity_config()
    assert cfg["enabled"] is True
    for gate in immunity.GATES:
        assert immunity.gate_mode(gate) == "shadow"
    assert cfg["auto_demote"]["enabled"] is True


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text(":: not yaml ::[")
    assert immunity.gate_mode("procedure") == "shadow"


# ─── master switch + per-gate modes ─────────────────────────────────────────


def test_master_off_short_circuits_every_gate(config_dirs):
    base, _ = config_dirs
    _write(base, {"enabled": False, "procedure": {"mode": "enforce"}})
    for gate in immunity.GATES:
        assert immunity.gate_mode(gate) == "off"


def test_per_gate_off(config_dirs):
    base, _ = config_dirs
    _write(base, {"procedure": {"mode": "off"}})
    assert immunity.gate_mode("procedure") == "off"
    assert immunity.gate_mode("identity") == "shadow"  # untouched gate keeps default


def test_unknown_gate_raises(config_dirs):
    with pytest.raises(ValueError, match="unknown immunity gate"):
        immunity.gate_mode("telepathy")


def test_invalid_mode_value_degrades_to_shadow(config_dirs):
    base, _ = config_dirs
    _write(base, {"procedure": {"mode": "block"}})
    assert immunity.gate_mode("procedure") == "shadow"


# ─── live read (no cache) ───────────────────────────────────────────────────


def test_overlay_edit_takes_effect_without_reload(config_dirs):
    """gate_mode re-reads the merged config on EVERY call: rewriting the
    .local.yaml overlay mid-process flips the answer immediately."""
    base, overlay = config_dirs
    _write(base, {"procedure": {"mode": "shadow"}})
    assert immunity.gate_mode("procedure") == "shadow"

    _write(overlay, {"procedure": {"mode": "enforce"}})
    assert immunity.gate_mode("procedure") == "enforce"  # same process, no reload


# ─── never-block invariant ──────────────────────────────────────────────────


def test_owner_and_first_party_are_never_blockable():
    assert immunity.is_blockable("owner") is False
    assert immunity.is_blockable("first_party") is False


@pytest.mark.parametrize("value", ["external_untrusted", None, "garbage"])
def test_external_unknown_and_missing_are_blockable(value):
    assert immunity.is_blockable(value) is True


def test_effective_origin_class_fails_closed():
    assert immunity.effective_origin_class(None) == "external_untrusted"
    assert immunity.effective_origin_class("garbage") == "external_untrusted"
    assert immunity.effective_origin_class("owner") == "owner"
    assert immunity.effective_origin_class("first_party") == "first_party"


# ─── auto-demote scaffold ───────────────────────────────────────────────────


def test_record_demotion_writes_overlay_and_takes_effect(config_dirs):
    base, overlay = config_dirs
    _write(base, {"identity": {"mode": "enforce"}})
    assert immunity.gate_mode("identity") == "enforce"

    immunity.record_demotion("identity", "test spike")

    written = yaml.safe_load(overlay.read_text())
    assert written["identity"]["mode"] == "shadow"
    state = written["auto_demote_state"]["identity"]
    assert state["from_mode"] == "enforce"
    assert state["reason"] == "test spike"
    assert state["demoted_at"]  # ISO timestamp present

    # Effective on the very next call — overlay wins over the base enforce.
    assert immunity.gate_mode("identity") == "shadow"


def test_record_demotion_preserves_existing_overlay_keys(config_dirs):
    _, overlay = config_dirs
    _write(overlay, {"procedure": {"mode": "enforce"}})

    immunity.record_demotion("identity", "spike")

    written = yaml.safe_load(overlay.read_text())
    assert written["procedure"]["mode"] == "enforce"  # untouched
    assert written["identity"]["mode"] == "shadow"


def test_record_demotion_unknown_gate_raises(config_dirs):
    _, overlay = config_dirs
    with pytest.raises(ValueError, match="unknown immunity gate"):
        immunity.record_demotion("telepathy", "nope")
    assert not overlay.exists()  # nothing written


def test_hand_edited_unquoted_off_is_honored(config_dirs):
    """YAML-1.1 parses an unquoted `mode: off` as boolean False — a hand
    edit with that intent is unambiguous and must mean 'off', not degrade
    to shadow. (`mode: on` is NOT a mode and still degrades to shadow.)"""
    base, _ = config_dirs
    base.write_text("enabled: true\nprocedure:\n  mode: off\nidentity:\n  mode: on\n")
    assert immunity.gate_mode("procedure") == "off"
    assert immunity.gate_mode("identity") == "shadow"
