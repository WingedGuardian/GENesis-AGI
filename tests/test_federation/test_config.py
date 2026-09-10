"""Federation lever — default-off, kill switch, and fail-to-off degradation."""

from __future__ import annotations

import pytest

from genesis.federation import config as fedconfig


@pytest.fixture
def cfg_file(monkeypatch, tmp_path):
    """Point the lever at an isolated tmp base config AND an isolated overlay dir
    (so a real ~/.genesis/config/federation.local.yaml can never leak into the
    test — hermetic). Returns a base-writer with a ``.overlay(text)`` helper.
    Kill switch is cleared unless a test sets it."""
    from genesis import _config_overlay

    base = tmp_path / "federation.yaml"
    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()
    monkeypatch.setattr(fedconfig, "_base_path", lambda: base)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: overlay_dir)
    monkeypatch.delenv(fedconfig._DISABLE_ENV, raising=False)

    def _write(text: str):
        base.write_text(text)

    _write.overlay = lambda text: (overlay_dir / "federation.local.yaml").write_text(text)
    return _write


def test_default_is_off_when_no_config(cfg_file):
    # no file written → DEFAULTS → off
    assert fedconfig.effective_mode() == "off"
    assert fedconfig.is_active() is False


def test_propose_only_activates(cfg_file):
    cfg_file("enabled: true\nmode: propose_only\n")
    assert fedconfig.effective_mode() == "propose_only"
    assert fedconfig.is_active() is True


def test_kill_switch_forces_off(cfg_file, monkeypatch):
    cfg_file("enabled: true\nmode: live\n")
    monkeypatch.setenv(fedconfig._DISABLE_ENV, "1")
    assert fedconfig.effective_mode() == "off"


def test_enabled_false_is_off(cfg_file):
    cfg_file("enabled: false\nmode: propose_only\n")
    assert fedconfig.effective_mode() == "off"


def test_invalid_mode_degrades_to_off(cfg_file):
    # least-authority: an unknown mode must NOT silently receive/send
    cfg_file("enabled: true\nmode: wideopen\n")
    assert fedconfig.effective_mode() == "off"


def test_yaml_boolean_off_is_honored(cfg_file):
    # unquoted `mode: off` parses as YAML-1.1 boolean False
    cfg_file("enabled: true\nmode: off\n")
    assert fedconfig.effective_mode() == "off"


def test_knob_int_rejects_bad_values(cfg_file):
    for bad in (0, -5, True, "nope", None):
        assert (
            fedconfig.knob_int({"retention_days": bad}, "retention_days")
            == (fedconfig.DEFAULTS["retention_days"])
        )
    assert fedconfig.knob_int({"retention_days": 30}, "retention_days") == 30


def test_shipped_config_default_is_off():
    # the repo's committed config/federation.yaml must ship dark (no accidental
    # live) — read the base file directly so a local overlay can't mask it
    import yaml

    shipped = yaml.safe_load(fedconfig._base_path().read_text())
    assert shipped.get("mode") in (False, "off")


def test_non_boolean_enabled_degrades_to_off(cfg_file):
    """A truthy non-boolean `enabled` (e.g. the string "false") must NOT keep the
    master switch on — that would leave a cross-owner channel active against the
    user's apparent intent."""
    # NB: bare `yes`/`on` are YAML-1.1 booleans (true) — use quoted strings + ints
    for enabled in ('"false"', '"true"', "0", "1", '"on"'):
        cfg_file(f"enabled: {enabled}\nmode: propose_only\n")
        assert fedconfig.effective_mode() == "off", f"enabled: {enabled} should be off"
    # a real boolean true still activates
    cfg_file("enabled: true\nmode: propose_only\n")
    assert fedconfig.effective_mode() == "propose_only"


def test_overlay_corrupt_yaml_forces_off(cfg_file):
    """A present-but-unparseable overlay must force off — NOT silently fall back to
    an active base config (the overlay is where the user's own `mode: off` lives).
    merge_local_overlay swallows the parse error and returns base, so the loader
    must validate the overlay itself; this writes a REAL malformed file."""
    cfg_file("enabled: true\nmode: live\n")  # base config is ACTIVE
    cfg_file.overlay("mode: {unclosed brace and : : :")  # malformed YAML
    assert fedconfig.effective_mode() == "off"


def test_overlay_non_mapping_forces_off(cfg_file):
    """An overlay that parses to a non-mapping (list/scalar) also forces off."""
    cfg_file("enabled: true\nmode: live\n")
    cfg_file.overlay("- just\n- a\n- list\n")
    assert fedconfig.effective_mode() == "off"


def test_overlay_valid_dict_applies(cfg_file):
    """A valid overlay is honored — the user can turn the channel off (or on) via
    their local overlay without editing the committed base."""
    cfg_file("enabled: true\nmode: live\n")
    cfg_file.overlay("mode: off\n")
    assert fedconfig.effective_mode() == "off"
    cfg_file.overlay("mode: propose_only\n")
    assert fedconfig.effective_mode() == "propose_only"
