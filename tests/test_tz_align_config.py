"""The clock-alignment lever and zone choice (genesis.tz_align_config).

An invalid or damaged lever reads as ``off`` (moving a system clock is all it
authorises), and the shipped UTC placeholder is never a chosen zone.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis import tz_align_config as tza
from genesis.mcp.health.settings import _validate_tz_align


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "tz_align.yaml"
    monkeypatch.setattr(tza, "_base_path", lambda: path)
    monkeypatch.delenv("GENESIS_TZ_ALIGN_DISABLED", raising=False)
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return path


@pytest.mark.parametrize(
    "overlay,mode",
    [
        ("mode: 'off'\n", "off"),
        ("mode: off\nbroken: [\n", "off"),  # the owner's off that failed to parse
        ("- off\n", "off"),
        ("", "live"),
    ],
)
def test_the_user_overlay(cfg_file, overlay, mode):
    cfg_file.write_text("mode: live\n")
    (cfg_file.parent / "user-config" / "tz_align.local.yaml").write_text(overlay)
    assert tza.effective_mode() == mode


@pytest.mark.parametrize(
    "body,mode",
    [
        (None, "live"),  # missing file: DEFAULTS
        ("mode: live\n", "live"),
        ("mode: 'off'\n", "off"),
        ("mode: off\n", "off"),  # YAML-1.1 False
        ("mode: sometimes\n", "off"),
        ("enabled: false\nmode: live\n", "off"),
        ('enabled: "false"\nmode: live\n', "off"),  # a quoted false is truthy
        ("enabled: 1\nmode: live\n", "off"),  # only a real true enables
        ("", "live"),  # an empty file: DEFAULTS
        ("- not a mapping\n", "off"),  # damage never authorises a move
        (":\n  bad: [\n", "off"),
    ],
)
def test_effective_mode(cfg_file, body, mode):
    if body is not None:
        cfg_file.write_text(body)
    assert tza.effective_mode() == mode


def test_the_kill_switch_forces_off(cfg_file, monkeypatch):
    cfg_file.write_text("mode: live\n")
    monkeypatch.setenv("GENESIS_TZ_ALIGN_DISABLED", "1")
    assert tza.effective_mode() == "off"


def test_the_shipped_config_is_live():
    assert tza.load_config()["mode"] == "live"


@pytest.mark.parametrize(
    "cfg,zone",
    [
        ({}, ""),
        ({"timezone": ""}, ""),
        ({"timezone": "   "}, ""),
        ({"timezone": None}, ""),
        ({"timezone": 0}, ""),
        ({"timezone": "Mars/Base"}, ""),
        ({"timezone": "UTC"}, ""),
        ({"timezone": "Etc/UTC"}, ""),
        ({"timezone": "UTC", "timezone_chosen": "yes"}, ""),
        ({"timezone": "UTC", "timezone_chosen": True}, "UTC"),
        ({"timezone": " Europe/Paris "}, "Europe/Paris"),
    ],
)
def test_chosen_zone(cfg, zone):
    with patch.object(tza, "_local_config", return_value=cfg):
        assert tza.chosen_zone() == zone


@pytest.mark.parametrize(
    "changes,ok",
    [
        ({"mode": "off"}, True),
        ({"mode": "live", "enabled": True}, True),
        ({"mode": "propose_only"}, False),
        ({"enabled": "yes"}, False),
        ({"zone": "Europe/Paris"}, False),
    ],
)
def test_settings_validator(changes, ok):
    assert (_validate_tz_align(changes) == []) is ok
