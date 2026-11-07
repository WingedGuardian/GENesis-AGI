"""genesis.util.disk_headroom: the free space a write can actually use, measured by
the disk guardian's own dg_measure (statvfs, tightened by a btrfs quota)."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from genesis.util import disk_headroom


def _fake_guardian(tmp_path, body: str):
    lib = tmp_path / "disk_guardian.sh"
    lib.write_text(f"dg_measure() {{\n{body}\n}}\n")
    return lib


def test_the_guardians_effective_figure_is_used(tmp_path, monkeypatch):
    lib = _fake_guardian(tmp_path, 'echo "1234 99999 1 - - btrfs 5"')
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", lib)
    assert disk_headroom.effective_free_bytes(tmp_path) == 1234 * 1024**2


def test_a_path_not_made_yet_is_measured_where_it_will_land(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    lib = _fake_guardian(tmp_path, f'printf %s "$1" > "{seen}"; echo "7 7 0 - - ext4 0"')
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", lib)
    assert disk_headroom.effective_free_bytes(tmp_path / "a" / "b") == 7 * 1024**2
    assert seen.read_text() == str(tmp_path)


@pytest.mark.parametrize("body", ["return 1", 'echo "not-a-number 1 0"', ":"])
def test_an_unusable_answer_falls_back_to_statvfs_and_says_so(tmp_path, monkeypatch, caplog, body):
    monkeypatch.setattr(disk_headroom, "_warned", False)
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", _fake_guardian(tmp_path, body))
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        got = disk_headroom.effective_free_bytes(tmp_path)
    assert (
        got == shutil.disk_usage(tmp_path).free
        or abs(got - shutil.disk_usage(tmp_path).free) < 64 * 1024**2
    )
    assert "statvfs" in caplog.text


def test_a_missing_guardian_library_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")
    assert disk_headroom.effective_free_bytes(tmp_path) > 0


def test_nothing_measurable_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")

    def fail(path):
        raise OSError("no statvfs")

    monkeypatch.setattr(disk_headroom.shutil, "disk_usage", fail)
    assert disk_headroom.effective_free_bytes(tmp_path) is None


def test_the_real_guardian_measures_this_filesystem(tmp_path):
    """Against the shipped scripts/lib/disk_guardian.sh: on a filesystem with no
    quota its figure is statvfs's, to the MiB it reports in."""
    got = disk_headroom.effective_free_bytes(tmp_path)
    assert got is not None and got > 0
    assert got <= shutil.disk_usage(tmp_path).free + 1024**2


def test_usage_is_the_guardians_free_and_total(tmp_path, monkeypatch):
    """When a quota binds, the guardian reports the quota as the total too; a floor
    taken as a share of the pool would refuse every write on such an install."""
    monkeypatch.setattr(
        disk_headroom, "_GUARDIAN_LIB", _fake_guardian(tmp_path, 'echo "40960 51200 1 - - btrfs 9"')
    )
    assert disk_headroom.effective_usage(tmp_path) == (40960 * 1024**2, 51200 * 1024**2)


def test_the_fallback_warns_once_per_process(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(disk_headroom, "_warned", False)
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        disk_headroom.effective_free_bytes(tmp_path)
        disk_headroom.effective_free_bytes(tmp_path)
    assert caplog.text.count("statvfs") == 1


# ── the guardian's tier, now and after a write ────────────────────────────────

_REAL_WATCHGOD = disk_headroom._WATCHGOD


def _measured(tmp_path, monkeypatch, line: str):
    """The real watchdog (its config loading, its tiers) over a dg_measure that
    reports ``line``; HOME without a watchgod config unless a test writes one."""
    watchgod = tmp_path / "watchgod.sh"
    watchgod.write_text(f'. "{_REAL_WATCHGOD}"\ndg_measure() {{ echo "{line}"; }}\n')
    monkeypatch.setattr(disk_headroom, "_WATCHGOD", watchgod)
    monkeypatch.setenv("HOME", str(tmp_path))


MB = 1024**2


@pytest.mark.parametrize(
    "free_mb,write_mb,tiers",
    [
        (20000, 1000, ("green", "green")),  # 30.8% -> 29.2%
        (6000, 0, ("yellow", "yellow")),  # 9.2%
        (6000, 1000, ("yellow", "orange")),  # -> 7.7%
        (5000, 900, ("orange", "orange")),  # 7.7%, RED at 3072 MB
        (5000, 2000, ("orange", "red")),  # -> 3000 MB
        (100, 1000, ("red", "red")),  # never below zero
    ],
)
def test_tiers_are_the_guardians_now_and_after_the_write(
    tmp_path, monkeypatch, free_mb, write_mb, tiers
):
    _measured(tmp_path, monkeypatch, f"{free_mb} 65000 0 - - ext4 1")
    assert disk_headroom.tier_after_write(tmp_path, write_mb * MB) == tiers


def test_a_partial_megabyte_counts_as_a_whole_one(tmp_path, monkeypatch):
    # 5000 MB of 65000 is ORANGE; RED starts below 3072 MB: 1928 MB plus a byte crosses it.
    _measured(tmp_path, monkeypatch, "5000 65000 0 - - ext4 1")
    assert disk_headroom.tier_after_write(tmp_path, 1928 * MB)[1] == "orange"
    assert disk_headroom.tier_after_write(tmp_path, 1928 * MB + 1)[1] == "red"


def test_the_watchgod_config_moves_the_lines(tmp_path, monkeypatch):
    """The guardian reads its thresholds from watchgod.conf; a tier judged without
    them would disagree with the watchdog."""
    _measured(tmp_path, monkeypatch, "5000 65000 0 - - ext4 1")
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text("DG_ORANGE_PCT=5\n")
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("yellow", "yellow")
    (conf / "watchgod.local.conf").write_text("DG_ORANGE_PCT=9\n")
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("orange", "orange")


def test_btrfs_metadata_pressure_is_red(tmp_path, monkeypatch):
    _measured(tmp_path, monkeypatch, "40000 65000 0 500 95 btrfs 1")
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("red", "red")


@pytest.mark.parametrize("body", ["return 1", 'echo "x 1 0 - - ext4 0"', ":"])
def test_no_tier_without_a_measurement(tmp_path, monkeypatch, caplog, body):
    watchgod = tmp_path / "watchgod.sh"
    watchgod.write_text(f'. "{_REAL_WATCHGOD}"\ndg_measure() {{\n{body}\n}}\n')
    monkeypatch.setattr(disk_headroom, "_WATCHGOD", watchgod)
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        assert disk_headroom.tier_after_write(tmp_path, 0) is None
    assert "tier" in caplog.text


def test_the_real_guardian_tiers_this_filesystem(tmp_path):
    now, after = disk_headroom.tier_after_write(tmp_path, 0)
    assert now == after and now in {"green", "yellow", "orange", "red"}


def test_a_fractional_byte_count_is_a_number_bash_can_use(tmp_path, monkeypatch):
    """A caller's size times a factor is a float; the shell's arithmetic refuses
    "151.0", and the tier read from the empty result was RED (found by E2E)."""
    _measured(tmp_path, monkeypatch, "5797 65595 0 - - ext4 1")
    assert disk_headroom.tier_after_write(tmp_path, 157968576.0) == ("yellow", "yellow")


def test_the_script_refuses_a_size_that_is_not_a_whole_number(tmp_path, monkeypatch):
    _measured(tmp_path, monkeypatch, "5797 65595 0 - - ext4 1")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            disk_headroom._TIER_SCRIPT,
            "dg",
            str(disk_headroom._WATCHGOD),
            str(tmp_path),
            "151.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0 and proc.stdout == ""


@pytest.mark.parametrize("bad", ["abc", "", "-1"])
def test_a_malformed_threshold_is_the_watchdogs_default_not_zero(tmp_path, monkeypatch, bad):
    """The watchdog resets a malformed DG_* value to its default (load_config's
    _wg_uint). Sourced raw, "abc" made the ORANGE line 0 and a copy was allowed
    almost down to a full disk (found by review)."""
    _measured(tmp_path, monkeypatch, "5000 65000 0 - - ext4 1")
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text(f"DG_ORANGE_PCT={bad}\n")
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("orange", "orange")


def test_a_leading_zero_threshold_is_decimal(tmp_path, monkeypatch):
    _measured(tmp_path, monkeypatch, "5000 65000 0 - - ext4 1")
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text("DG_ORANGE_PCT=007\n")  # 7%: 5000 of 65000 is 7.69%
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("yellow", "yellow")


def test_a_config_warning_is_ours_to_log_not_the_watchdogs(tmp_path, monkeypatch, caplog):
    """The watchdog's loader logs a malformed value to the watchdog's own log file;
    from here that line would read as the watchdog's, so it comes back to us."""
    _measured(tmp_path, monkeypatch, "5000 65000 0 - - ext4 1")
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text("DG_ORANGE_PCT=abc\n")
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        assert disk_headroom.tier_after_write(tmp_path, 0) is not None
    assert "DG_ORANGE_PCT" in caplog.text
    assert not (tmp_path / ".genesis" / "logs" / "tmp_watchgod.log").exists()


def test_the_guardian_is_found_under_the_configured_repo_root(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    try:
        reloaded = importlib.reload(disk_headroom)
        assert tmp_path / "scripts" / "tmp_watchgod.sh" == reloaded._WATCHGOD
        assert tmp_path / "scripts" / "lib" / "disk_guardian.sh" == reloaded._GUARDIAN_LIB
    finally:
        monkeypatch.delenv("GENESIS_REPO_ROOT")
        importlib.reload(disk_headroom)


def test_a_write_that_would_use_the_last_unallocated_space_is_red_after(tmp_path, monkeypatch):
    """btrfs RED also fires on metadata nearly full with little unallocated space
    left; a write can allocate chunks from that space, so the after-tier assumes
    it does (unallocated 1500 MB, metadata 85%: a 600 MB write leaves 900 MB)."""
    _measured(tmp_path, monkeypatch, "40000 65000 0 1500 85 btrfs 1")
    assert disk_headroom.tier_after_write(tmp_path, 400 * MB) == ("green", "green")
    assert disk_headroom.tier_after_write(tmp_path, 600 * MB) == ("green", "red")


# ── round 2 of the review ─────────────────────────────────────────────────────


def _state(tmp_path, eta_min, *, path=None, age_s=0):
    """The watchdog's state file, as tmp_watchgod.sh writes it."""
    poll = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=age_s)
    d = tmp_path / ".genesis"
    d.mkdir(parents=True, exist_ok=True)
    (d / "watchgod_state.json").write_text(
        json.dumps(
            {
                "disk": {str(path or tmp_path): {"tier": "x", "eta_min": eta_min}},
                "poll_at": poll.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    )


def test_the_watchdogs_time_to_full_does_not_change_the_answer(tmp_path, monkeypatch):
    """Free space decides (owner ruling 2026-10-12): the copy's own write burst would
    raise the watchdog's time-to-full tier on almost any yellow disk, so reading it
    would refuse nearly every copy; and its state file names a path, not the quota
    domain this write lands in."""
    _measured(tmp_path, monkeypatch, "6000 65000 0 - - ext4 1")
    _state(tmp_path, 5)  # full in 5 minutes by the watchdog's reading
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("yellow", "yellow")
    assert disk_headroom.tier_after_write(tmp_path, 1000 * MB) == ("yellow", "orange")


def test_the_callers_environment_cannot_move_the_lines(tmp_path, monkeypatch):
    """The watchdog runs with no DG_* in its environment; the helper borrowing its
    verdict must not take them from whoever called it (found by review: DG_ORANGE_PCT=0
    or a BASH_ENV file allowed a copy the watchdog would tier ORANGE)."""
    _measured(tmp_path, monkeypatch, "6000 65000 0 - - ext4 1")
    rc = tmp_path / "bashenv"
    rc.write_text("DG_ORANGE_PCT=0\nDG_RED_MIN_MB=0\n")
    monkeypatch.setenv("BASH_ENV", str(rc))
    monkeypatch.setenv("DG_ORANGE_PCT", "0")
    monkeypatch.setenv("DG_RED_PCT", "0")
    monkeypatch.setenv("DG_RED_MIN_MB", "0")
    assert disk_headroom.tier_after_write(tmp_path, 1000 * MB) == ("yellow", "orange")


def test_a_config_that_prints_does_not_spoil_the_answer(tmp_path, monkeypatch):
    _measured(tmp_path, monkeypatch, "6000 65000 0 - - ext4 1")
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text("echo hello\n")
    assert disk_headroom.tier_after_write(tmp_path, 0) == ("yellow", "yellow")


def test_an_absurd_size_is_no_answer_not_a_wrapped_one(tmp_path, monkeypatch):
    """Shell arithmetic wraps at 2**63: 10**30 bytes read as a gain of space."""
    _measured(tmp_path, monkeypatch, "6000 65000 0 - - ext4 1")
    assert disk_headroom.tier_after_write(tmp_path, 10**30) is None


def test_a_missing_watchdog_is_no_answer(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(disk_headroom, "_WATCHGOD", tmp_path / "missing.sh")
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        assert disk_headroom.tier_after_write(tmp_path, 0) is None


def test_asking_writes_nothing_the_watchdog_owns(tmp_path, monkeypatch):
    """Only the real dg_measure: no rate file, state file, log or alert from a
    helper call (a switch to dg_tier's rate bookkeeping would corrupt the
    watchdog's own series)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / ".genesis" / "config"
    conf.mkdir(parents=True)
    (conf / "watchgod.conf").write_text("DG_ORANGE_PCT=abc\n")
    assert disk_headroom.tier_after_write(tmp_path, 0) is not None
    assert sorted(p.relative_to(tmp_path) for p in (tmp_path / ".genesis").rglob("*")) == [
        Path(".genesis/config"),
        Path(".genesis/config/watchgod.conf"),
    ]
