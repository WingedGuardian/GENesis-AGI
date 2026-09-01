"""Federation lever — default-off, kill switch, and fail-to-off degradation."""

from __future__ import annotations

import pytest

from genesis.federation import config as fedconfig


@pytest.fixture
def cfg_file(monkeypatch, tmp_path):
    """Point the lever at an isolated tmp base config (no .local overlay beside
    it) and ensure the kill switch is off unless a test sets it."""
    path = tmp_path / "federation.yaml"
    monkeypatch.setattr(fedconfig, "_base_path", lambda: path)
    monkeypatch.delenv(fedconfig._DISABLE_ENV, raising=False)

    def _write(text: str):
        path.write_text(text)

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
