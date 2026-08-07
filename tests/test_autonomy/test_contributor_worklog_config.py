"""Contributor Work-Log control surface: live-read mode lever + knobs + env
kill switch + settings domain registration/validator (repo_pulse lineage)."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.autonomy import contributor_worklog_config as cwc
from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_contributor_worklog,
)


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs, and clear the
    kill-switch env so tests start from a clean lever."""
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(cwc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv(cwc._DISABLE_ENV, raising=False)
    return (
        repo_dir / "config" / "contributor_worklog.yaml",
        user_dir / "contributor_worklog.local.yaml",
    )


# ── effective_mode ───────────────────────────────────────────────────────


def test_defaults_propose_only_when_no_config(config_dirs):
    """Shipped posture: propose + approve, but NEVER auto-post."""
    assert cwc.effective_mode() == "propose_only"


def test_off_via_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: 'off'\n")
    assert cwc.effective_mode() == "off"


def test_off_via_unquoted_yaml_bool(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert cwc.effective_mode() == "off"


def test_master_enabled_false_wins(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: false\nmode: live\n")
    assert cwc.effective_mode() == "off"


def test_live_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert cwc.effective_mode() == "live"


def test_invalid_mode_degrades_to_propose_only(config_dirs, caplog):
    """Invalid config never silently posts to a public repo — degrades to
    propose_only, not live."""
    base, _ = config_dirs
    base.write_text("mode: bananas\n")
    with caplog.at_level("WARNING"):
        assert cwc.effective_mode() == "propose_only"
    assert any("propose_only" in r.message for r in caplog.records)


def test_env_kill_switch_forces_off(config_dirs, monkeypatch):
    """The kill switch overrides even an explicit `mode: live`."""
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert cwc.effective_mode() == "live"
    monkeypatch.setenv(cwc._DISABLE_ENV, "1")
    assert cwc.effective_mode() == "off"


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("mode: live\n")
    overlay.write_text("mode: 'off'\n")
    assert cwc.effective_mode() == "off"


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("{{{{not yaml")
    assert cwc.effective_mode() == "propose_only"


def test_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert cwc.effective_mode() == "live"
    base.write_text("mode: 'off'\n")
    assert cwc.effective_mode() == "off"  # next call


# ── knob accessors ───────────────────────────────────────────────────────


def test_knobs_from_config_and_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("retention_days: 7\n")
    cfg = cwc.load_config()
    assert cwc.knob_int(cfg, "retention_days") == 7
    assert cwc.knob_int(cfg, "max_held") == cwc.DEFAULTS["max_held"]


def test_knobs_reject_garbage_toward_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("retention_days: -3\nmax_held: 'many'\n")
    cfg = cwc.load_config()
    assert cwc.knob_int(cfg, "retention_days") == cwc.DEFAULTS["retention_days"]
    assert cwc.knob_int(cfg, "max_held") == cwc.DEFAULTS["max_held"]


# ── constants (imported by the MCP tool, drain, and approve-all exclusion) ─


def test_shared_constants_stable():
    assert cwc.CONTRIBUTOR_ISSUE_ACTION_TYPE == "contributor_issue_post"
    assert cwc.CELL_DOMAIN == "github"
    assert cwc.CELL_VERB == "issue_create"
    assert cwc.CELL_RISK_CLASS == "public_write"


# ── settings domain ──────────────────────────────────────────────────────


def test_domain_registered():
    assert "contributor_worklog" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["contributor_worklog"]
    assert d.readonly is False
    assert d.needs_restart is False
    assert d.config_filename == "contributor_worklog.yaml"
    assert "contributor_worklog" in _DOMAIN_VALIDATORS


def test_validator_accepts_valid_changes():
    assert _validate_contributor_worklog({"enabled": False}) == []
    assert _validate_contributor_worklog({"mode": "off"}) == []
    assert _validate_contributor_worklog({"mode": "propose_only"}) == []
    assert _validate_contributor_worklog({"mode": "live"}) == []
    assert _validate_contributor_worklog({"retention_days": 60, "max_held": 5}) == []


def test_validator_rejects_bad_values():
    assert _validate_contributor_worklog({"mode": "shadow"})
    assert _validate_contributor_worklog({"enabled": "false"})
    assert _validate_contributor_worklog({"retention_days": 0})
    assert _validate_contributor_worklog({"max_held": True})
    assert _validate_contributor_worklog({"bogus": 1})


def test_base_config_file_matches_defaults():
    """The shipped config/contributor_worklog.yaml parses and matches DEFAULTS."""
    import yaml

    base = Path(__file__).parents[2] / "config" / "contributor_worklog.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg == cwc.DEFAULTS
