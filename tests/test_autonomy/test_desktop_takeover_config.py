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
    """A corrupt BASE degrades to shadow — observable, never a silent off.

    Correct precisely because the tracked base holds only values DEFAULTS
    already reproduces, so falling back to them loses nothing the operator
    wrote. Contrast the overlay cases below, where it does."""
    base, _ = config_dirs
    base.write_text("mode: live\nlive_opt_in: [unclosed\n")
    assert dtc.effective_mode() == "shadow"


# ── a damaged OVERLAY is a different fact from a damaged base ────────────


def test_an_unparseable_overlay_forces_off_not_shadow(config_dirs):
    """The overlay is the ONLY home for an operator's settings, so damage there
    discards them — and ``merge_local_overlay`` returns the base unchanged,
    which is indistinguishable from a clean load at the call site."""
    base, overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    overlay.write_text("enabled: [unclosed\n")
    assert dtc.effective_mode() == "off"


def test_a_wrong_shape_overlay_forces_off(config_dirs):
    """Valid YAML, wrong ROOT shape — parses fine and merges nothing. The one
    malformed case an exception handler never sees."""
    base, overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    overlay.write_text("- enabled: false\n- mode: off\n")
    assert dtc.effective_mode() == "off"


def test_a_damaged_overlay_does_not_silently_resume_a_disabled_capability(config_dirs):
    """The acceptance bar — the real defect this exists for.

    The base ships ``enabled: true``, so an operator's disable can only live in
    the overlay. Degrading a damaged overlay to defaults re-enables a
    capability they switched off, and reports a healthy config while doing it."""
    base, overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    overlay.write_text("enabled: false\n")
    assert dtc.effective_mode() == "off", "control: the disable is honoured while readable"

    overlay.write_text("enabled: false\nmode: [unclosed\n")
    assert dtc.effective_mode() == "off", "and is not lost when the file is damaged"


def test_an_ABSENT_overlay_is_not_damage(config_dirs):
    """The negative control. Absent is the common case — a fresh install has no
    overlay at all — and a check that cannot tell it from corruption would
    force every install to off and look like it was working."""
    base, overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    assert not overlay.exists()
    assert dtc.effective_mode() == "shadow"


def test_a_VALID_overlay_still_applies(config_dirs):
    """The other negative control: the damage check must not swallow a healthy
    overlay. Without this, "forces off" could be unconditional and every test
    above would still pass."""
    base, overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\nlive_opt_in: false\n")
    overlay.write_text("mode: live\nlive_opt_in: true\n")
    assert dtc.effective_mode() == "live"


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


def test_an_overlay_that_is_a_DIRECTORY_is_damage_not_absence(config_dirs):
    """`merge_local_overlay` tests `exists()`, so a directory takes its read
    branch, raises IsADirectoryError, and returns base with the overlay
    dropped. An `is_file()` check in the damage probe would answer "not
    damaged" for that same path and report shadow on a load where every
    operator override was discarded.

    It has to be the REPO-RELATIVE sibling, not the user-dir path. A review
    finding named the user-dir path, and it does not reproduce there:
    `_resolve_overlay_path` tests `is_file()` itself, so a directory in the
    user dir is diverted to the sibling and never reaches either predicate.
    The sibling is returned by the fallback branch UNGUARDED, so it does."""
    base, user_overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    assert not user_overlay.exists(), "the user-dir path must stay absent"
    (base.parent / "desktop_takeover.local.yaml").mkdir()
    assert dtc.effective_mode() == "off"


def test_a_directory_in_the_USER_dir_is_diverted_not_damage(config_dirs):
    """The control for the test above, and the correction to the finding that
    prompted it. A directory at the user-dir path means there is no overlay
    FILE there, `_resolve_overlay_path` falls back to a sibling that does not
    exist, and no operator setting was lost — so `shadow` is right."""
    base, user_overlay = config_dirs
    base.write_text("enabled: true\nmode: shadow\n")
    user_overlay.mkdir()
    assert dtc.effective_mode() == "shadow"


def test_an_absurd_ttl_falls_back_rather_than_granting_the_longest_window(config_dirs):
    """The lever is described as "bounded" and was bounded on ONE side: any
    positive value was accepted, so a typo'd 30000 was a 20-day grant, and a
    large enough value made `timedelta(minutes=...)` raise OverflowError out of
    the gate rather than refuse.

    It degrades to the DEFAULT rather than clamping to the ceiling, because
    every degradation path here moves toward less authority — clamping would
    make a mistyped duration grant the longest window the code allows."""
    base, _ = config_dirs
    base.write_text("grant_ttl_minutes: 30000\naction_ttl_seconds: 99999\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]
    assert dtc.action_ttl_seconds() == dtc.DEFAULTS["action_ttl_seconds"]

    base.write_text("grant_ttl_minutes: 999999999999999999999\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]


def test_a_ttl_at_the_ceiling_is_still_honoured(config_dirs):
    """The control: the bound must reject the absurd without capping the merely
    generous."""
    base, _ = config_dirs
    base.write_text("grant_ttl_minutes: 1440\naction_ttl_seconds: 300\n")
    assert dtc.grant_ttl_minutes() == 1440
    assert dtc.action_ttl_seconds() == 300


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " y "])
def test_the_kill_switch_honours_the_usual_truthy_spellings(config_dirs, monkeypatch, caplog, raw):
    """It honoured the literal "1" only, so `=true` / `=yes` / `=on` disabled
    nothing and warned about nothing — on the one control the module documents
    as unreachable-around.

    Asserts SILENCE as well as the mode, and that is what makes the test
    isolate the truthy set. The unrecognised-value fallback also returns "off",
    so a mutation shrinking the set back to `== "1"` still yields off for every
    spelling here — it just does it by calling them typos. Without the
    no-warning assertion this test passes with the thing it names deleted."""
    import logging

    base, _ = config_dirs
    base.write_text("enabled: true\nmode: live\nlive_opt_in: true\n")
    monkeypatch.setenv(dtc.DISABLE_ENV, raw)
    with caplog.at_level(logging.WARNING):
        assert dtc.effective_mode() == "off", raw
    assert not [r for r in caplog.records if "not a recognised boolean" in r.getMessage()], (
        f"{raw!r} is a normal spelling and must not be reported as a typo"
    )


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_an_explicitly_falsy_kill_switch_does_not_disable(config_dirs, monkeypatch, raw):
    """The control. Without it, "treat unrecognised as set" could be
    unconditional and every test above would still pass."""
    base, _ = config_dirs
    base.write_text("enabled: true\nmode: live\nlive_opt_in: true\n")
    monkeypatch.setenv(dtc.DISABLE_ENV, raw)
    assert dtc.effective_mode() == "live", raw


def test_an_unrecognised_kill_switch_value_stops_the_capability(config_dirs, monkeypatch):
    """Someone typed a value into the kill switch. The safe reading is that
    they meant to stop it, and it is logged so the typo is visible."""
    base, _ = config_dirs
    base.write_text("enabled: true\nmode: live\nlive_opt_in: true\n")
    monkeypatch.setenv(dtc.DISABLE_ENV, "maybe")
    assert dtc.effective_mode() == "off"


def test_a_non_finite_ttl_falls_back_rather_than_crashing(config_dirs):
    """`.inf` is valid YAML and PyYAML yields a float infinity, on which `int()`
    raises OverflowError — which was not in the handler's tuple, so BOTH TTL
    readers could crash a gate check instead of returning their documented safe
    default. The upper bound added last round cannot help: it is only reached
    once the conversion has already succeeded.

    Same class as the pid and timestamp coercions elsewhere in this change —
    found by enumerating every `int()` over an external value, after the
    previous round patched one member and left the rest."""
    base, _ = config_dirs
    base.write_text("grant_ttl_minutes: .inf\naction_ttl_seconds: -.inf\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]
    assert dtc.action_ttl_seconds() == dtc.DEFAULTS["action_ttl_seconds"]

    base.write_text("grant_ttl_minutes: .nan\naction_ttl_seconds: .nan\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]
    assert dtc.action_ttl_seconds() == dtc.DEFAULTS["action_ttl_seconds"]


def test_a_fractional_ttl_is_refused_rather_than_truncated(config_dirs):
    """`int()` TRUNCATES, so 30.9 minutes would silently become 30 — a duration
    the operator did not write, from a value that was malformed."""
    base, _ = config_dirs
    base.write_text("grant_ttl_minutes: 30.9\naction_ttl_seconds: 45.5\n")
    assert dtc.grant_ttl_minutes() == dtc.DEFAULTS["grant_ttl_minutes"]
    assert dtc.action_ttl_seconds() == dtc.DEFAULTS["action_ttl_seconds"]

    # The control: an integral float is a legitimate spelling of a duration.
    base.write_text("grant_ttl_minutes: 45.0\naction_ttl_seconds: 20.0\n")
    assert dtc.grant_ttl_minutes() == 45
    assert dtc.action_ttl_seconds() == 20
