"""A lock holder found only through an unreadable fd table must last (#4001).

`live_move_repair._held_open` and `stale_index_lock._git_in_repo` counted a
same-user git whose fd table (or cwd and arguments) could not be read as the
lock's holder, from one scan. A git that is merely exiting looks like that for
milliseconds, so a stale lock was left and an update's next index write failed;
in parallel CI another worker's git did the same to these scripts' tests. Each
suspect is now re-checked: it still counts only if it stays the same live
process and stays unreadable.

These tests drive a fake /proc. The `_settle` wait between re-checks is replaced by a
step that changes the suspect, so each case is deterministic.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_confirm_under_test", ROOT / "scripts" / "lib" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lmr = _load("live_move_repair")
sil = _load("stale_index_lock")

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="running as root: EACCES cannot be produced"
)


def _stat(state: str = "S", start: str = "424242", flags: int = 0) -> str:
    # After the command name: the state (field 3), then fields 4-21; field 9 is
    # the flags and field 22 (start time) is the 20th token.
    middle = ["0"] * 18
    middle[5] = str(flags)
    return f"{os.getppid()} (git) {state} " + " ".join(middle) + f" {start}\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A /proc with one live git of this user whose fd table cannot be read."""
    proc = tmp_path / "proc"
    entry = proc / str(os.getppid())  # a pid that is really alive, for _pid_alive's kill(0)
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text(_stat())
    (entry / "comm").write_text("git\n")
    (entry / "fd").chmod(0)
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    old = 3 * 3600
    os.utime(lock, (os.path.getmtime(lock) - old,) * 2)
    monkeypatch.setattr(lmr, "_PROC", str(proc))
    yield entry, lock
    (entry / "fd").chmod(0o755)


def _steps(monkeypatch, *changes):
    """Replace _settle: the n-th re-check first applies changes[n] (if any)."""
    calls = []

    def step(_seconds):
        if len(calls) < len(changes) and changes[len(calls)] is not None:
            changes[len(calls)]()
        calls.append(_seconds)

    monkeypatch.setattr(lmr, "_settle", step)
    return calls


def test_a_lasting_unreadable_git_still_holds_the_lock(fake, monkeypatch):
    _entry, lock = fake
    calls = _steps(monkeypatch)
    assert lmr._held_open(str(lock)) is True
    assert len(calls) == lmr._CONFIRM_SCANS


def test_a_git_that_exits_during_the_recheck_does_not_hold_it(fake, monkeypatch):
    entry, lock = fake

    def exits():
        (entry / "fd").chmod(0o755)
        (entry / "stat").write_text(_stat(state="Z"))  # exited, not yet reaped

    _steps(monkeypatch, exits)
    assert lmr._held_open(str(lock)) is False


def test_a_reused_pid_is_not_the_same_suspect(fake, monkeypatch):
    entry, lock = fake
    _steps(monkeypatch, lambda: (entry / "stat").write_text(_stat(start="999999")))
    assert lmr._held_open(str(lock)) is False


def test_a_suspect_that_becomes_readable_is_judged_on_its_fds(fake, monkeypatch, tmp_path):
    entry, lock = fake

    def readable_without_lock():
        (entry / "fd").chmod(0o755)

    _steps(monkeypatch, readable_without_lock)
    assert lmr._held_open(str(lock)) is False

    (entry / "fd").chmod(0)

    def readable_with_lock():
        (entry / "fd").chmod(0o755)
        os.symlink(str(lock), entry / "fd" / "3")

    _steps(monkeypatch, readable_with_lock)
    assert lmr._held_open(str(lock)) is True


def test_no_suspect_costs_no_recheck(fake, monkeypatch):
    entry, lock = fake
    (entry / "fd").chmod(0o755)  # readable, and not holding the lock
    calls = _steps(monkeypatch)
    assert lmr._held_open(str(lock)) is False
    assert calls == []


def test_an_unreadable_git_that_exits_does_not_count_as_working_here(monkeypatch):
    # stale_index_lock's second check: a git whose cwd and arguments cannot be read.
    pid = os.getppid()
    states = iter([None, ["/elsewhere"]])  # unreadable on the scan, then readable elsewhere
    monkeypatch.setattr(sil, "_git_places", lambda _pid: next(states))
    monkeypatch.setattr(sil, "_start_time", lambda _pid: "1")
    monkeypatch.setattr(sil, "_settle", lambda _s: None)
    suspects = {pid: "1"}
    assert sil._confirmed_git(suspects, {"/repo/.git"}, lambda _p: True) is None


def test_an_unreadable_git_that_lasts_still_counts(monkeypatch):
    pid = os.getppid()
    monkeypatch.setattr(sil, "_git_places", lambda _pid: None)
    monkeypatch.setattr(sil, "_start_time", lambda _pid: "1")
    monkeypatch.setattr(sil, "_settle", lambda _s: None)
    assert sil._confirmed_git({pid: "1"}, {"/repo/.git"}, lambda _p: True) == pid


def test_the_recheck_waits_in_real_time_under_a_fake_clock(fake, monkeypatch):
    # The repair's tests replace _sleep with a fake clock. A re-check waiting
    # through it took no real time, so a git passing through on another worker
    # still looked held, and the stale lock was not retired (found by running
    # these files under a stream of short-lived, unreadable gits).
    entry, lock = fake
    faked = []
    monkeypatch.setattr(lmr, "_sleep", faked.append)
    _steps(monkeypatch, lambda: (entry / "stat").write_text(_stat(state="Z")))
    assert lmr._held_open(str(lock)) is False
    assert faked == [], "the re-check waited on the fake clock"


PF_EXITING = 0x4


def test_an_exiting_process_is_dropped_at_the_scan_with_no_wait(fake, monkeypatch):
    # The measured case: a git in the kernel's exit path (state R, PF_EXITING,
    # memory released) has unreadable fds. It can hold nothing, so no re-check.
    entry, lock = fake
    (entry / "stat").write_text(_stat(state="R", flags=0x400000 | PF_EXITING | 0x8))
    calls = _steps(monkeypatch)
    assert lmr._held_open(str(lock)) is False
    assert calls == []


def test_a_suspect_that_starts_exiting_during_the_recheck_is_dropped(fake, monkeypatch):
    entry, lock = fake
    _steps(monkeypatch, lambda: (entry / "stat").write_text(_stat(flags=PF_EXITING)))
    assert lmr._held_open(str(lock)) is False


def test_exiting_reads_only_the_flag_and_fails_toward_may_hold(tmp_path, monkeypatch):
    proc = tmp_path / "proc" / "7"
    proc.mkdir(parents=True)
    monkeypatch.setattr(lmr, "_PROC", str(tmp_path / "proc"))
    (proc / "stat").write_text(_stat(flags=0x400000))  # other flags only
    assert lmr._exiting(7) is False
    (proc / "stat").write_text(_stat(flags=PF_EXITING))
    assert lmr._exiting(7) is True
    (proc / "stat").write_text("7 (g) S 1\n")  # too short to carry the flags
    assert lmr._exiting(7) is False
    (proc / "stat").unlink()
    assert lmr._exiting(7) is False
