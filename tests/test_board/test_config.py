"""Work-board control surface: the live-read mode lever, its env kill switch,
its degrade direction, and the settings-domain registration + validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.board import config as bc
from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS, _validate_board


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay resolution into tmp dirs and clear the kill switch."""
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(bc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv(bc._DISABLE_ENV, raising=False)
    return repo_dir / "config" / "board.yaml", user_dir / "board.local.yaml"


def test_default_is_off_when_no_config(config_dirs):
    """A fresh clone has no board until its owner turns one on."""
    assert bc.effective_mode() == "off"
    assert bc.writes_allowed() is False


def test_shipped_config_file_is_off():
    """The tracked config/board.yaml itself ships `off` (not just DEFAULTS)."""
    shipped = Path(__file__).resolve().parents[2] / "config" / "board.yaml"
    import yaml

    assert yaml.safe_load(shipped.read_text())["mode"] == "off"


@pytest.mark.parametrize("mode", ["off", "propose_only", "live"])
def test_each_valid_mode_reads_back(config_dirs, mode):
    base, _ = config_dirs
    base.write_text(f"mode: '{mode}'\n")
    assert bc.effective_mode() == mode
    assert bc.writes_allowed() is (mode == "live")


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("mode: 'off'\n")
    overlay.write_text("mode: 'live'\n")
    assert bc.effective_mode() == "live"


@pytest.mark.parametrize("bad", ["'LIVE'", "'on'", "1", "'yes'", "'proposeonly'"])
def test_invalid_mode_degrades_to_propose_only_never_live(config_dirs, bad):
    """Toward LESS write authority: an invalid value can read, never write."""
    base, _ = config_dirs
    base.write_text(f"mode: {bad}\n")
    assert bc.effective_mode() == "propose_only"
    assert bc.writes_allowed() is False


def test_unquoted_yaml_off_is_off(config_dirs):
    """YAML 1.1 parses a bare `off` as boolean False; that must mean off."""
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert bc.effective_mode() == "off"


@pytest.mark.parametrize("enabled", ["false", "0", "''", "'false'", "'yes'", ""])
def test_master_switch_fails_closed_unless_literally_true(config_dirs, enabled):
    """Anything but the boolean `true` turns the board off — a hand edit that
    blanks or mistypes the master switch must never leave `live` armed (a bare
    `enabled:` parses as None, `0` as an int, quoted values as strings)."""
    base, _ = config_dirs
    base.write_text(f"enabled: {enabled}\nmode: 'live'\n")
    assert bc.effective_mode() == "off"


def test_enabled_true_keeps_the_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: true\nmode: 'live'\n")
    assert bc.effective_mode() == "live"


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_kill_switch_forces_off_even_when_live(config_dirs, monkeypatch, value):
    base, _ = config_dirs
    base.write_text("mode: 'live'\n")
    monkeypatch.setenv(bc._DISABLE_ENV, value)
    assert bc.effective_mode() == "off"
    assert bc.writes_allowed() is False


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: [unclosed\n")
    assert bc.effective_mode() == "off"


def test_read_live_no_cache(config_dirs):
    """A hand edit takes effect on the next call — no boot cache."""
    base, _ = config_dirs
    base.write_text("mode: 'propose_only'\n")
    assert bc.effective_mode() == "propose_only"
    base.write_text("mode: 'live'\n")
    assert bc.effective_mode() == "live"


# ── settings domain ─────────────────────────────────────────────────────────


def test_domain_registered_with_its_validator():
    domain = _DOMAIN_REGISTRY["board"]
    assert domain.config_filename == "board.yaml"
    assert domain.readonly is False
    assert _DOMAIN_VALIDATORS["board"] is _validate_board


def test_validator_accepts_the_non_arming_modes_and_enabled():
    for mode in ("off", "propose_only"):
        assert _validate_board({"mode": mode}) == []
    assert _validate_board({"enabled": False}) == []


def test_validator_rejects_live_so_no_session_can_arm_writes():
    """`live` arms GitHub writes; settings_update is reachable by any session,
    so arming is an owner overlay edit only (the marketing_outreach precedent)."""
    errors = _validate_board({"mode": "live"})
    assert errors and "board.local.yaml" in errors[0]


def test_validator_rejects_enabled_true_so_no_session_re_arms_a_paused_board():
    """An owner can pause a `mode: live` overlay with `enabled: false`; a session
    re-enabling it would re-arm the writes. A session may only turn it down."""
    errors = _validate_board({"enabled": True})
    assert errors and "board.local.yaml" in errors[0]


def test_owner_overlay_still_arms_live(config_dirs):
    """Rejecting `live` in the validator must not make it unreachable: the
    owner's overlay edit is the sanctioned route and still takes effect."""
    _, overlay = config_dirs
    overlay.write_text("mode: 'live'\n")
    assert bc.writes_allowed() is True


@pytest.mark.parametrize(
    "changes",
    [{"mode": "LIVE"}, {"mode": True}, {"enabled": "false"}, {"project_number": 3}],
)
def test_validator_rejects_bad_values_and_unknown_keys(changes):
    assert _validate_board(changes)
