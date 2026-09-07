"""Desktop-takeover arming lever — every degradation path walked end to end.

The property under test is one-directional: no invalid, missing, corrupt or
half-set config may ever produce ``live``. Each case asserts the RESULTING MODE
rather than the absence of an exception, because a lever that raises and a
lever that quietly arms are both failures and only one of them looks like one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.autonomy import desktop_takeover_config as dtc


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(dtc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv(dtc.DISABLE_ENV, raising=False)
    return (
        repo_dir / "config" / "desktop_takeover.yaml",
        user_dir / "desktop_takeover.local.yaml",
    )


# ── the shipped posture ──────────────────────────────────────────────────


def test_shipped_config_is_not_armed(monkeypatch):
    """A fresh clone must never be able to touch the operator's machine.

    Reads the REAL config/desktop_takeover.yaml, not a fixture — the shipped
    file is the thing that would arm a clone, so a fixture proves nothing here.
    """
    monkeypatch.delenv(dtc.DISABLE_ENV, raising=False)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: Path("/nonexistent"))
    cfg = dtc.load_config()
    assert cfg["mode"] == "shadow"
    assert cfg["live_opt_in"] is False
    assert dtc.effective_mode() == "shadow"


def test_defaults_shadow_when_no_config(config_dirs):
    assert dtc.effective_mode() == "shadow"


# ── the ladder: every rung refuses to arm ────────────────────────────────


def test_mode_live_alone_is_not_armed(config_dirs):
    """The whole point of the second key: one edited line is not consent."""
    base, _ = config_dirs
    base.write_text("mode: live\n")
    assert dtc.effective_mode() == "shadow"


def test_live_opt_in_alone_is_not_armed(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: shadow\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "shadow"


def test_both_keys_arm(config_dirs):
    """The positive control. Without this passing, every refusal above could
    be an inert check that never had a live path to refuse."""
    base, _ = config_dirs
    base.write_text("mode: live\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "live"


def test_overlay_alone_can_arm(config_dirs):
    """Arming through the gitignored overlay is the SUPPORTED path — the base
    file stays at the shipped posture and the operator's local file opts in."""
    base, overlay = config_dirs
    base.write_text("mode: shadow\nlive_opt_in: false\n")
    overlay.write_text("mode: live\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "live"


def test_string_false_enabled_reads_as_off_not_live(config_dirs):
    """`enabled: 'false'` is a truthy Python string. A plain `if not enabled`
    would read the most disabling-looking value as ARMED."""
    base, _ = config_dirs
    base.write_text("enabled: 'false'\nmode: live\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "off"


def test_master_enabled_false_wins_over_live(config_dirs):
    base, _ = config_dirs
    base.write_text("enabled: false\nmode: live\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "off"


def test_invalid_mode_degrades_to_shadow(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: liv\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "shadow"


def test_unquoted_yaml_bool_off_is_honoured(config_dirs):
    """YAML 1.1 parses a bare `mode: off` as boolean False. The intent is
    unambiguous, so it is honoured rather than degraded to shadow."""
    base, _ = config_dirs
    base.write_text("mode: off\n")
    assert dtc.effective_mode() == "off"


def test_corrupt_config_degrades_to_shadow(config_dirs):
    base, _ = config_dirs
    base.write_text("mode: live\nlive_opt_in: [unclosed\n")
    assert dtc.effective_mode() == "shadow"


def test_env_kill_switch_beats_an_armed_config(config_dirs, monkeypatch):
    base, _ = config_dirs
    base.write_text("mode: live\nlive_opt_in: true\n")
    monkeypatch.setenv(dtc.DISABLE_ENV, "1")
    assert dtc.effective_mode() == "off"


def test_env_kill_switch_does_not_read_the_config(monkeypatch, tmp_path):
    """The stop must work when the config cannot be read at all — checked
    BEFORE any YAML load, so an unparseable file is not a way past it."""
    monkeypatch.setenv(dtc.DISABLE_ENV, "1")

    def _boom():
        raise AssertionError("effective_mode read config before the kill switch")

    monkeypatch.setattr(dtc, "load_config", _boom)
    assert dtc.effective_mode() == "off"


# ── the two bounded windows ──────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["0", "-5", "'abc'", "true", "null"])
def test_unusable_ttl_falls_back_to_the_default_not_to_no_bound(config_dirs, raw):
    """A mistyped duration must not become an unbounded grant. Note `true`:
    bool is an int subclass, so a naive int() would read it as 1 minute."""
    base, _ = config_dirs
    base.write_text(f"grant_ttl_minutes: {raw}\naction_ttl_seconds: {raw}\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]
    assert dtc.action_ttl_seconds() == dtc.DEFAULTS["action_ttl_seconds"]


def test_ttls_are_configurable(config_dirs):
    base, _ = config_dirs
    base.write_text("grant_ttl_minutes: 5\naction_ttl_seconds: 12\n")
    assert dtc.grant_ttl_minutes() == 5
    assert dtc.action_ttl_seconds() == 12


# ── the arming keys are not reachable from the API surface ───────────────


def test_not_registered_as_a_settings_domain():
    """Arming desktop input must be a conscious file edit, never one
    unconfirmed settings_update()/dashboard call."""
    from genesis.mcp.health.settings import _DOMAIN_REGISTRY

    assert "desktop_takeover" not in _DOMAIN_REGISTRY
    assert not any(
        getattr(d, "config_filename", "") == "desktop_takeover.yaml"
        for d in _DOMAIN_REGISTRY.values()
    )
