"""ego_reconcile control surface: live-read mode lever (off/shadow/live) + knob
+ settings domain registration/validator (PR-5, cc_foreground_reaper lineage)."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.ego import reconcile_config as rc
from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_ego_reconcile,
)


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially. Also
    clears the env kill switch so it can't leak in from the ambient environment.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(rc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv("GENESIS_EGO_RECONCILE_DISABLED", raising=False)
    return (
        repo_dir / "config" / "ego_reconcile.yaml",
        user_dir / "ego_reconcile.local.yaml",
    )


# ── effective_mode ───────────────────────────────────────────────────────


def test_defaults_shadow_when_no_config(config_dirs):
    """Shadow is the default: PR-5 ships observation-only."""
    assert rc.effective_mode() == "shadow"


def test_live_via_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert rc.effective_mode() == "live"


def test_off_via_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: 'off'\n")
    assert rc.effective_mode() == "off"


def test_off_via_unquoted_yaml_bool(config_dirs):
    """A hand-edited unquoted `mode: off` parses as YAML False — honored."""
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert rc.effective_mode() == "off"


def test_master_enabled_false_wins(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: false\nmode: live\n")
    assert rc.effective_mode() == "off"


def test_env_kill_switch_forces_off(config_dirs, monkeypatch):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    monkeypatch.setenv("GENESIS_EGO_RECONCILE_DISABLED", "1")
    assert rc.effective_mode() == "off"


def test_invalid_mode_degrades_to_shadow(config_dirs, caplog):
    """Invalid config degrades toward LESS write authority — never a silent
    live (unreviewed mutations), never a silent off (hidden feature)."""
    base, _ = config_dirs
    base.write_text("mode: bananas\n")
    with caplog.at_level("WARNING"):
        assert rc.effective_mode() == "shadow"
    assert any("shadow" in r.message for r in caplog.records)


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("mode: shadow\n")
    overlay.write_text("mode: live\n")
    assert rc.effective_mode() == "live"


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("{{{{not yaml")
    assert rc.effective_mode() == "shadow"


def test_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert rc.effective_mode() == "live"
    base.write_text("mode: shadow\n")
    assert rc.effective_mode() == "shadow"  # takes effect on the very next call


# ── knob accessor ────────────────────────────────────────────────────────


def test_retention_knob_from_config_and_default(config_dirs):
    base, _ = config_dirs
    base.write_text("revision_retention_days: 90\n")
    cfg = rc.load_config()
    assert rc.knob_int(cfg, "revision_retention_days") == 90


def test_retention_knob_rejects_garbage_toward_default(config_dirs):
    base, _ = config_dirs
    base.write_text("revision_retention_days: -3\n")
    cfg = rc.load_config()
    assert rc.knob_int(cfg, "revision_retention_days") == rc.DEFAULTS["revision_retention_days"]


# ── settings domain ──────────────────────────────────────────────────────


def test_domain_registered():
    assert "ego_reconcile" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["ego_reconcile"]
    assert d.readonly is False
    assert d.needs_restart is False  # re-read every ego cycle
    assert d.config_filename == "ego_reconcile.yaml"
    assert "ego_reconcile" in _DOMAIN_VALIDATORS


def test_validator_accepts_valid_changes():
    assert _validate_ego_reconcile({"enabled": False}) == []
    assert _validate_ego_reconcile({"mode": "off"}) == []
    assert _validate_ego_reconcile({"mode": "shadow"}) == []
    assert _validate_ego_reconcile({"mode": "live"}) == []
    assert _validate_ego_reconcile({"revision_retention_days": 30}) == []


def test_validator_rejects_bad_values():
    assert _validate_ego_reconcile({"mode": "propose_only"})  # not a reconcile mode
    assert _validate_ego_reconcile({"enabled": "false"})
    assert _validate_ego_reconcile({"revision_retention_days": 0})
    assert _validate_ego_reconcile({"revision_retention_days": True})
    assert _validate_ego_reconcile({"bogus": 1})


def test_base_config_file_matches_defaults():
    """The shipped config/ego_reconcile.yaml parses and matches DEFAULTS."""
    import yaml

    base = Path(__file__).parents[2] / "config" / "ego_reconcile.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg == rc.DEFAULTS
