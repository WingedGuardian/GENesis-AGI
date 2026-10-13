"""Retention for the browser-profile copies genesis.browser.profile_backup makes
(scripts/disk_hygiene.sh ``prune_browser_backups``, deciding through
scripts/prune_browser_backups.py).

Per profile, a copy is named ``<profile>.pre-v<version>-<UTC time>``, and its label is
``v<version>``, the build that last opened the profile before the copy. The rules:
- the newest copy overall is always kept;
- the newest copy of each label is kept for 90 days, because going back to that
  build (a pin-back) needs it, and the pin-back itself makes a newer copy;
- older copies of a label go after 14 days;
- an unfinished ``*.tmp`` copy goes after a day.

Sources the script (which only defines functions when sourced) and calls the function
on a fixture dir. Ages are set with os.utime.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_HYGIENE = _SCRIPTS / "disk_hygiene.sh"
DAY = 86400


def _copy(base: Path, name: str, age_days: float) -> Path:
    d = base / name
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "cookies.sqlite").write_bytes(b"x")
    t = time.time() - age_days * DAY
    for p in (d / "sub" / "cookies.sqlite", d / "sub", d):
        os.utime(p, (t, t))
    return d


def _prune(base: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_browser_backups '{base}'"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


CAM = "camoufox-profile.pre-v135.0.1-beta.24_20250315105650"


def test_newest_copy_is_kept_however_old(tmp_path):
    only = _copy(tmp_path, f"{CAM}-20260101T000000", 400)
    r = _prune(tmp_path)
    assert r.returncode == 0, r.stderr
    assert only.is_dir()


def test_older_copies_of_a_label_go_after_14_days(tmp_path):
    old = _copy(tmp_path, f"{CAM}-20260101T000000", 30)
    middle = _copy(tmp_path, f"{CAM}-20260201T000000", 20)
    newest = _copy(tmp_path, f"{CAM}-20260301T000000-1", 16)
    _prune(tmp_path)
    assert not old.exists() and not middle.exists()
    assert newest.is_dir()


def test_ages_are_exact_not_whole_days(tmp_path):
    """find -mtime +14 means 15 whole days; the retention is 14 days and 1 day."""
    older = _copy(tmp_path, "camoufox-profile.pre-v2-20260201T000000", 14.5)
    _copy(tmp_path, "camoufox-profile.pre-v2-20260301T000000", 1)
    unfinished = _copy(tmp_path, "camoufox-profile.pre-v3-20260301T000000.abc.tmp", 1.5)
    _prune(tmp_path)
    assert not older.exists() and not unfinished.exists()


def test_a_pin_back_keeps_the_copy_of_the_older_build(tmp_path):
    """Upgrade 135->156 copied the 135 profile; weeks later a pin back to 135 copies
    the 156 profile first. The 135 copy is the way back to 135 and must survive."""
    way_back = _copy(tmp_path, f"{CAM}-20260101T000000", 30)
    pin_back = _copy(
        tmp_path, "camoufox-profile.pre-v156.0.1-beta.36_20261006020805-20260201T000000", 1
    )
    _prune(tmp_path)
    assert way_back.is_dir() and pin_back.is_dir()


def test_a_labels_newest_copy_goes_after_90_days(tmp_path):
    stale = _copy(tmp_path, "camoufox-profile.pre-v1-20250101T000000", 91)
    kept = _copy(tmp_path, "camoufox-profile.pre-v2-20250301T000000", 89)
    _copy(tmp_path, "camoufox-profile.pre-v3-20260301T000000", 1)
    _prune(tmp_path)
    assert not stale.exists() and kept.is_dir()


def test_recent_older_copy_is_kept(tmp_path):
    recent = _copy(tmp_path, "camoufox-profile.pre-v1-20260101T000000", 5)
    newest = _copy(tmp_path, "camoufox-profile.pre-v1-20260201T000000", 1)
    _prune(tmp_path)
    assert recent.is_dir() and newest.is_dir()


def test_unfinished_copy_goes_after_a_day(tmp_path):
    stale = _copy(tmp_path, "camoufox-profile.pre-v1-20260101T000000.abc123.tmp", 2)
    fresh = _copy(tmp_path, "camoufox-profile.pre-v1-20260102T000000.def456.tmp", 0.1)
    _prune(tmp_path)
    assert not stale.exists()
    assert fresh.is_dir()  # may still be copying


def test_an_unfinished_copy_is_never_the_newest_kept(tmp_path):
    complete = _copy(tmp_path, "camoufox-profile.pre-v1-20260101T000000", 400)
    _copy(tmp_path, "camoufox-profile.pre-v2-20260301T000000.abc.tmp", 20)
    _prune(tmp_path)
    assert complete.is_dir()


def test_each_profile_is_pruned_on_its_own(tmp_path):
    cam_old = _copy(tmp_path, f"{CAM}-20260101T000000", 30)
    cam_new = _copy(tmp_path, f"{CAM}-20260201T000000", 20)
    chrome_old = _copy(tmp_path, "browser-profile.pre-v145.0.7632.6-20251201T000000", 50)
    chrome = _copy(tmp_path, "browser-profile.pre-v145.0.7632.6-20260101T000000", 40)
    _prune(tmp_path)
    assert not cam_old.exists() and cam_new.is_dir()
    assert not chrome_old.exists() and chrome.is_dir()


def test_profiles_and_unrelated_dirs_are_never_touched(tmp_path):
    keep = [
        _copy(tmp_path, "camoufox-profile", 900),
        _copy(tmp_path, "browser-profile", 900),
        _copy(tmp_path, "camoufox-profile.bak", 900),
        _copy(tmp_path, "camoufox-profile.pre-0.5-20260101", 900),  # not a profile_backup name
        _copy(tmp_path, "camoufox-profile.pre-v1-not-a-time", 900),
        _copy(tmp_path, "other.pre-v1-20260101T000000", 900),
    ]
    (tmp_path / "camoufox-profile.pre-v7-20250101T000000").symlink_to(keep[0])
    _copy(tmp_path, "camoufox-profile.pre-v9-20260901T000000", 0)
    _prune(tmp_path)
    assert all(p.is_dir() for p in keep)
    assert (tmp_path / "camoufox-profile.pre-v7-20250101T000000").is_symlink()


def test_missing_base_is_a_no_op(tmp_path):
    r = _prune(tmp_path / "absent")
    assert r.returncode == 0, r.stderr


def test_main_prunes_the_genesis_dir():
    src = _HYGIENE.read_text()
    assert 'prune_browser_backups "$HOME/.genesis"' in src


def _decider():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prune_browser_backups", _SCRIPTS / "prune_browser_backups.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        "camoufox-profile.pre-v1-20260101T000000\n",
        "camoufox-profile.pre-v1-20260101T000000.abc123.tmp\n",
        "camoufox-profile.pre-v1-20260101T000000.ab\ncd.tmp",
    ],
)
def test_a_name_with_a_newline_is_never_a_backup(tmp_path, name):
    """`$` matches before a final newline: such a directory is no name this code
    makes, so it must never reach the deleter (found by review)."""
    old = _copy(tmp_path, name, 400)
    _copy(tmp_path, "camoufox-profile.pre-v1-20260301T000000", 1)
    assert old not in _decider().doomed(tmp_path, time.time())
