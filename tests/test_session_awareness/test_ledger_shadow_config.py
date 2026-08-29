"""session_ledger_shadow control surface: live-read mode lever + settings
domain registration/validator (session-manager PR-3, ws3_immunity lineage)."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _validate_session_ledger_shadow,
)
from genesis.session_awareness import ledger_shadow_config as lsc


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(lsc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return (
        repo_dir / "config" / "session_ledger_shadow.yaml",
        user_dir / "session_ledger_shadow.local.yaml",
    )


# ── effective_mode ───────────────────────────────────────────────────────


def test_defaults_shadow_when_no_config(config_dirs):
    assert lsc.effective_mode() == "shadow"


def test_off_via_mode(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: 'off'\n")
    assert lsc.effective_mode() == "off"


def test_off_via_unquoted_yaml_bool(config_dirs):
    """A hand-edited unquoted `mode: off` parses as YAML False — honored."""
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert lsc.effective_mode() == "off"


def test_master_enabled_false_wins(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: false\nmode: shadow\n")
    assert lsc.effective_mode() == "off"


def test_live_is_honoured(config_dirs):
    """`live` was reserved until the write path existed. It exists now."""
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert lsc.effective_mode() == "live"


def test_nothing_degrades_INTO_live(config_dirs, caplog):
    """The failure direction must always be toward LESS write authority.

    `live` promotes rows into the real ledger, so it must be reachable ONLY by
    someone typing it. Every malformed, missing or unexpected value has to land
    on shadow or off — a config typo must never be able to grant write
    authority. Asserting the invalid case degrades to *shadow* is not enough on
    its own: that assertion passes just as happily if the mode were ignored
    entirely, so the live case above is its necessary partner.
    """
    base, _ = config_dirs
    for bogus in ("LIVE", "Live", " live", "liveness", "true", "1", "", "yes"):
        base.write_text(f"mode: {bogus!r}\n")
        with caplog.at_level("WARNING"):
            assert lsc.effective_mode() != "live", (
                f"mode {bogus!r} reached live without being spelled exactly"
            )


def test_invalid_mode_degrades_to_shadow(config_dirs, caplog):
    base, _ = config_dirs
    base.write_text("mode: bananas\n")
    with caplog.at_level("WARNING"):
        assert lsc.effective_mode() == "shadow"


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("mode: shadow\n")
    overlay.write_text("mode: 'off'\n")
    assert lsc.effective_mode() == "off"


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("{{{{not yaml")
    assert lsc.effective_mode() == "shadow"


def test_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: shadow\n")
    assert lsc.effective_mode() == "shadow"
    base.write_text("mode: 'off'\n")
    assert lsc.effective_mode() == "off"  # takes effect on the very next call


# ── settings domain ──────────────────────────────────────────────────────


def test_domain_registered():
    assert "session_ledger_shadow" in _DOMAIN_REGISTRY
    d = _DOMAIN_REGISTRY["session_ledger_shadow"]
    assert d.readonly is False
    assert d.needs_restart is False  # each worker run is a fresh process
    assert d.config_filename == "session_ledger_shadow.yaml"
    assert "session_ledger_shadow" in _DOMAIN_VALIDATORS


def test_validator_accepts_valid_changes():
    assert _validate_session_ledger_shadow({"enabled": False}) == []
    assert _validate_session_ledger_shadow({"mode": "off"}) == []
    assert _validate_session_ledger_shadow({"mode": "live"}) == []  # reserved but settable


def test_validator_rejects_bad_values():
    assert _validate_session_ledger_shadow({"mode": "block"})
    assert _validate_session_ledger_shadow({"enabled": "false"})
    assert _validate_session_ledger_shadow({"bogus": 1})


def test_base_config_file_ships_shadow_not_live():
    """The shipped config must not promote until the evidence earns it.

    Pinned deliberately: this one line decides whether a fresh install writes
    autonomous rows into the user's ledger, and an accidental edit changes
    behaviour on every install with no other signal. The write path is complete
    and tested — that is precisely why the config value needs its own guard.
    """
    import yaml

    base = Path(__file__).parents[2] / "config" / "session_ledger_shadow.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg == {"enabled": True, "mode": "shadow"}
    assert cfg["mode"] in lsc.MODES
