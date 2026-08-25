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


def test_max_posts_per_day_knob(config_dirs):
    """The rate cap reads through knob_int: honored when a valid positive int,
    else coerced to the safe default — a bad value can NEVER uncap the poster."""
    base, _ = config_dirs
    base.write_text("max_posts_per_day: 5\n")
    assert cwc.knob_int(cwc.load_config(), "max_posts_per_day") == 5
    for bad in ("max_posts_per_day: 0", "max_posts_per_day: -1", "max_posts_per_day: true"):
        base.write_text(bad + "\n")
        assert (
            cwc.knob_int(cwc.load_config(), "max_posts_per_day")
            == cwc.DEFAULTS["max_posts_per_day"]
        )


# ── require_approval (fail-CLOSED: only an explicit boolean False disables) ──


def test_require_approval_defaults_true(config_dirs):
    """A fresh clone with no config keeps the human gate ON — never auto-posts."""
    assert cwc.require_approval() is True


def test_require_approval_explicit_false_disables(config_dirs):
    base, _ = config_dirs
    base.write_text("require_approval: false\n")
    assert cwc.require_approval() is False


def test_require_approval_fail_closed_on_garbage(config_dirs):
    """Only an explicit boolean False disables the gate. A typo, a non-bool int,
    or a stray string all keep human approval ON (fail-closed) — a public-repo
    post must never go un-gated by a config accident."""
    base, _ = config_dirs
    for val in ("require_approval: maybe", "require_approval: 0", "require_approval: 'false'"):
        base.write_text(val + "\n")
        assert cwc.require_approval() is True, val


def test_require_approval_overlay_disables(config_dirs):
    """This install's overlay is how autonomous posting is opted into."""
    base, overlay = config_dirs
    base.write_text("mode: live\n")  # base keeps the default (approval required)
    overlay.write_text("require_approval: false\n")
    assert cwc.require_approval() is False


def test_require_approval_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("require_approval: false\n")
    assert cwc.require_approval() is False
    base.write_text("require_approval: true\n")
    assert cwc.require_approval() is True  # next call re-reads


# ── constants (imported by the MCP tool, drain, and approve-all exclusion) ─


def test_shared_constants_stable():
    assert cwc.CONTRIBUTOR_ISSUE_ACTION_TYPE == "contributor_issue_post"
    assert cwc.CELL_DOMAIN == "github"
    assert cwc.CELL_VERB == "issue_create"
    assert cwc.CELL_RISK_CLASS == "bulk"


def test_cell_constants_are_valid():
    """The risk class MUST be a real RiskClass member — an off-enum value would
    hard-fail the shared capability-grants machinery (CHECK constraint + severity
    / prior-weight dicts) the moment the enforce stage touches this cell. This
    guard would have caught the original 'public_write' (not a RiskClass) value."""
    from genesis.autonomy.types import RiskClass

    assert cwc.CELL_RISK_CLASS in {r.value for r in RiskClass}


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
    assert _validate_contributor_worklog({"require_approval": False}) == []
    assert _validate_contributor_worklog({"require_approval": True, "max_posts_per_day": 3}) == []


def test_validator_rejects_bad_values():
    assert _validate_contributor_worklog({"mode": "shadow"})
    assert _validate_contributor_worklog({"enabled": "false"})
    assert _validate_contributor_worklog({"retention_days": 0})
    assert _validate_contributor_worklog({"max_held": True})
    assert _validate_contributor_worklog({"bogus": 1})
    assert _validate_contributor_worklog({"require_approval": "yes"})  # must be a boolean
    assert _validate_contributor_worklog({"max_posts_per_day": 0})  # positive int only
    assert _validate_contributor_worklog({"max_posts_per_day": True})


def test_base_config_file_matches_defaults():
    """The shipped config/contributor_worklog.yaml parses and matches DEFAULTS."""
    import yaml

    base = Path(__file__).parents[2] / "config" / "contributor_worklog.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg == cwc.DEFAULTS
