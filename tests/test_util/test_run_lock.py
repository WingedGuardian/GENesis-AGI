"""Tests for genesis.util.run_lock — the eval runners' whole-run locks (#2612)."""

from __future__ import annotations

import fcntl
import logging
import os

import pytest

from genesis.util import run_lock as rl


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    ghome = tmp_path / "genesis-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(ghome))
    return home, ghome


def _old_runner_lock(path):
    """What the previous release's runners did: open "w" + non-blocking flock."""
    fh = open(path, "w")  # noqa: SIM115
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_lock_lives_under_genesis_locks_and_excludes_a_second_run(homes):
    _home, ghome = homes
    lock = rl.acquire_run_lock("bench.lock")
    try:
        assert (ghome / "locks" / "bench.lock").is_file()
        with pytest.raises(BlockingIOError):
            rl.acquire_run_lock("bench.lock")
    finally:
        rl.release_run_lock(lock)
    rl.release_run_lock(rl.acquire_run_lock("bench.lock"))  # free again


def test_takes_the_old_tmp_lock_too(homes):
    home, _ghome = homes
    lock = rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
    try:
        assert len(lock.handles) == 2
        # An old-release runner trying its ~/tmp lock is refused.
        with pytest.raises(BlockingIOError):
            _old_runner_lock(home / "tmp" / ".bench.lock")
    finally:
        rl.release_run_lock(lock)
    _old_runner_lock(home / "tmp" / ".bench.lock").close()  # released


def test_an_old_runner_holding_the_tmp_lock_refuses_and_leaves_nothing_held(homes):
    home, _ghome = homes
    old = _old_runner_lock(home / "tmp" / ".bench.lock")
    try:
        with pytest.raises(BlockingIOError) as excinfo:
            rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
        # Keep the exception alive, as a caller's ``raise ... from e`` does:
        # its traceback pins the frame, so an unreleased handle would still be
        # open (and locked) here. The new lock must already be free.
        assert excinfo.value is not None
        rl.release_run_lock(rl.acquire_run_lock("bench.lock"))
    finally:
        old.close()


def test_missing_tmp_dir_is_created_like_the_old_code_did(homes):
    # The previous code did mkdir + open, so a missing ~/tmp is created here
    # too: an old runner starting at the same moment then meets the same file.
    home, _ghome = homes
    (home / "tmp").rmdir()
    lock = rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
    try:
        assert len(lock.handles) == 2
        with pytest.raises(BlockingIOError):
            _old_runner_lock(home / "tmp" / ".bench.lock")
    finally:
        rl.release_run_lock(lock)


def test_tmp_that_is_not_a_directory_skips_the_old_lock(homes, caplog):
    home, _ghome = homes
    (home / "tmp").rmdir()
    (home / "tmp").write_text("not a directory")
    with caplog.at_level(logging.WARNING, logger=rl.__name__):
        lock = rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
    try:
        assert len(lock.handles) == 1
        assert "skipping the old lock" in caplog.text
    finally:
        rl.release_run_lock(lock)


def test_old_lock_mtime_is_refreshed_each_run(homes):
    # Keeps a lock in regular use out of the ~/tmp age prune, as the previous
    # code's "w" open did.
    home, _ghome = homes
    old = home / "tmp" / ".bench.lock"
    old.write_text("")
    os.utime(old, (1_000_000, 1_000_000))
    rl.release_run_lock(rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock"))
    assert old.stat().st_mtime > 1_000_000 + 86400


def test_unwritable_tmp_dir_skips_the_old_lock_with_a_warning(homes, caplog):
    home, _ghome = homes
    (home / "tmp").chmod(0o500)
    try:
        with caplog.at_level(logging.WARNING, logger=rl.__name__):
            lock = rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
        try:
            assert len(lock.handles) == 1
            assert "skipping the old lock" in caplog.text
        finally:
            rl.release_run_lock(lock)
    finally:
        (home / "tmp").chmod(0o700)


def test_release_is_idempotent_and_accepts_none(homes):
    lock = rl.acquire_run_lock("x.lock", legacy_name=".x.lock")
    rl.release_run_lock(lock)
    rl.release_run_lock(lock)
    rl.release_run_lock(None)
    assert lock.handles == []


def test_existing_lock_file_is_not_truncated(homes):
    _home, ghome = homes
    path = ghome / "locks" / "keep.lock"
    path.parent.mkdir(parents=True)
    path.write_text("note")
    rl.release_run_lock(rl.acquire_run_lock("keep.lock"))
    assert path.read_text() == "note"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x"])
def test_names_are_plain_file_names(homes, bad):
    with pytest.raises(ValueError):
        rl.acquire_run_lock(bad)
    with pytest.raises(ValueError) as excinfo:
        rl.acquire_run_lock("ok.lock", legacy_name=bad)
    # A bad legacy name is refused before anything is taken: with the
    # exception still alive, the primary lock is free.
    assert excinfo.value is not None
    rl.release_run_lock(rl.acquire_run_lock("ok.lock"))


def test_each_runner_maps_busy_to_its_own_error(homes):
    from genesis.eval import gauntlet as G
    from genesis.eval.bench import runner as bench
    from genesis.eval.skill_replay import runner as replay

    home, ghome = homes
    cases = [
        (
            bench._acquire_lock,
            bench._release_lock,
            bench.BenchBusyError,
            "bench.lock",
            ".bench.lock",
        ),
        (
            replay._acquire_lock,
            replay._release_lock,
            replay.SkillReplayBusyError,
            "skill_replay.lock",
            ".skill_replay.lock",
        ),
        (
            lambda: G._acquire_lock("m/1"),
            G._release_lock,
            G.GauntletBusyError,
            "gauntlet-m_1.lock",
            ".gauntlet-m_1.lock",
        ),
    ]
    for acquire, release, busy, name, legacy in cases:
        held = acquire()
        try:
            assert (ghome / "locks" / name).is_file()
            with pytest.raises(busy):
                acquire()
        finally:
            release(held)
        # An old-release runner holding the ~/tmp file also refuses the run.
        old = _old_runner_lock(home / "tmp" / legacy)
        try:
            with pytest.raises(busy):
                acquire()
        finally:
            old.close()
        release(acquire())


def test_read_only_old_lock_is_still_checked(homes):
    # Devin review of #2703: an old runner may hold a lock file whose
    # permissions changed after it opened it. A read-only file is checked
    # read-only (flock works on any descriptor), so a free one proceeds...
    home, _ghome = homes
    old = home / "tmp" / ".bench.lock"
    old.write_text("")
    old.chmod(0o444)
    lock = rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
    try:
        assert len(lock.handles) == 2
    finally:
        rl.release_run_lock(lock)


def test_read_only_old_lock_held_by_an_old_runner_is_busy(homes):
    # ...and one an old runner holds refuses the run.
    home, _ghome = homes
    old = home / "tmp" / ".bench.lock"
    holder = _old_runner_lock(old)
    try:
        old.chmod(0o444)
        with pytest.raises(BlockingIOError) as excinfo:
            rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
        assert excinfo.value is not None
        rl.release_run_lock(rl.acquire_run_lock("bench.lock"))  # nothing left held
    finally:
        holder.close()


def test_old_lock_that_exists_but_cannot_be_opened_refuses_the_run(homes):
    home, _ghome = homes
    old = home / "tmp" / ".bench.lock"
    old.write_text("")
    old.chmod(0o000)
    try:
        with pytest.raises(OSError, match="cannot check the old run lock") as excinfo:
            rl.acquire_run_lock("bench.lock", legacy_name=".bench.lock")
        assert not isinstance(excinfo.value, BlockingIOError)
        rl.release_run_lock(rl.acquire_run_lock("bench.lock"))  # nothing left held
    finally:
        old.chmod(0o600)
