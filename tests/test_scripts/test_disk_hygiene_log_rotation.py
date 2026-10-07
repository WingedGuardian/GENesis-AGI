"""rotate_log in scripts/disk_hygiene.sh: copytruncate rotation of the shared
MCP logs in ~/tmp.

~/tmp/mcp_health.log is appended to by every session's MCP server through a
FileHandler opened once, so it is never renamed: it is copied to FILE.1 and
truncated in place. These tests source the script (main is guarded) and call
the function, the house pattern for disk_hygiene steps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _rotate(f: Path, max_bytes: int = 1000, keep: int = 2) -> subprocess.CompletedProcess:
    # Paths travel as positional parameters, never interpolated into the script.
    return subprocess.run(
        ["bash", "-c", 'source "$1"; rotate_log "$2" "$3" "$4"', "_",
         str(_HYGIENE), str(f), str(max_bytes), str(keep)],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )


def test_a_small_log_is_left_alone(tmp_path):
    f = tmp_path / "mcp_health.log"
    f.write_text("x" * 500)
    _rotate(f)
    assert f.read_text() == "x" * 500
    assert not (tmp_path / "mcp_health.log.1").exists()


def test_a_large_log_is_copied_then_truncated_in_place(tmp_path):
    f = tmp_path / "mcp_health.log"
    f.write_text("old line\n" * 200)
    inode = f.stat().st_ino
    result = _rotate(f)
    assert "rotated" in result.stdout
    assert (tmp_path / "mcp_health.log.1").read_text() == "old line\n" * 200
    assert f.stat().st_size == 0
    assert f.stat().st_ino == inode, "renamed instead of truncated: writers would lose the file"


def test_an_open_append_writer_keeps_writing_to_the_live_file(tmp_path):
    """The property copytruncate depends on: a handle opened in append mode
    (what logging.FileHandler uses) writes at the NEW end after the truncate,
    so the file restarts small instead of becoming sparse at the old offset."""
    f = tmp_path / "mcp_health.log"
    with open(f, "a") as writer:
        writer.write("before\n" * 300)
        writer.flush()
        _rotate(f)
        writer.write("after\n")
        writer.flush()
    assert f.read_text() == "after\n"
    assert f.stat().st_size == len("after\n")


def test_older_copies_shift_and_the_oldest_drops(tmp_path):
    f = tmp_path / "mcp_health.log"
    (tmp_path / "mcp_health.log.1").write_text("gen1")
    (tmp_path / "mcp_health.log.2").write_text("gen2")
    f.write_text("y" * 2000)
    _rotate(f, keep=2)
    assert (tmp_path / "mcp_health.log.2").read_text() == "gen1"
    assert (tmp_path / "mcp_health.log.1").read_text() == "y" * 2000
    assert not (tmp_path / "mcp_health.log.3").exists()


def test_a_missing_log_or_a_symlink_is_a_noop(tmp_path):
    _rotate(tmp_path / "absent.log")
    target = tmp_path / "elsewhere.log"
    target.write_text("z" * 5000)
    link = tmp_path / "mcp_health.log"
    os.symlink(target, link)
    _rotate(link)
    assert target.read_text() == "z" * 5000
    assert not (tmp_path / "mcp_health.log.1").exists()


def test_main_rotates_both_shared_logs():
    src = _HYGIENE.read_text()
    assert 'rotate_log "$HOME/tmp/mcp_health.log"' in src
    assert 'rotate_log "$HOME/tmp/turnstile_debug.log"' in src


def test_a_planted_symlink_at_the_temp_name_is_never_written_through(tmp_path):
    """Security review: cp writes THROUGH a symlink at its destination, so a
    fixed FILE.1.tmp planted as a link overwrote its target with the log. The
    temp is now a fresh mktemp file; the planted link and its target survive."""
    f = tmp_path / "mcp_health.log"
    f.write_text("log line\n" * 200)
    victim = tmp_path / "victim.sh"
    victim.write_text("original")
    os.symlink(victim, tmp_path / "mcp_health.log.1.tmp")
    _rotate(f)
    assert victim.read_text() == "original"
    assert (tmp_path / "mcp_health.log.1").read_text() == "log line\n" * 200
    assert not (tmp_path / "mcp_health.log.1").is_symlink()
    assert not list(tmp_path.glob(".mcp_health.log.rotate.*"))


def test_a_failed_copy_keeps_every_retained_rotation(tmp_path):
    """Codex round 1: the rotations used to shift before the copy could fail, so
    a full disk lost the oldest kept copy while saying nothing had changed."""
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    (tmp_path / "mcp_health.log.1").write_text("one")
    (tmp_path / "mcp_health.log.2").write_text("two")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "cp").write_text("#!/bin/sh\nexit 1\n")  # a copy that fails, as on a full disk
    (bindir / "cp").chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; PATH="$2:$PATH"; rotate_log "$3" 1000 2', "_",
         str(_HYGIENE), str(bindir), str(f)],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    assert "left as is" in result.stdout
    assert (tmp_path / "mcp_health.log.1").read_text() == "one"
    assert (tmp_path / "mcp_health.log.2").read_text() == "two"
    assert f.read_text() == "current\n" * 200
    assert not list(tmp_path.glob(".mcp_health.log.rotate.*"))


def _assert_left_as_is(tmp_path: Path, f: Path, result: subprocess.CompletedProcess) -> None:
    assert "rotated" not in result.stdout
    assert "left as is" in result.stdout
    assert f.read_text() == "current\n" * 200, "the live log was truncated"
    assert not list(tmp_path.glob(".mcp_health.log.rotate.*"))


@pytest.mark.parametrize("dir_slot, file_slot", [(1, None), (1, 2), (2, None)])
def test_a_directory_in_any_rotation_slot_stops_the_rotation(tmp_path, dir_slot, file_slot):
    """Codex P2 / Devin round 1: mv onto a directory moves the copy INSIDE it
    and reports success, so the truncate followed a move that archived nothing
    in its slot, and later runs piled copies into that directory."""
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    slot_dir = tmp_path / f"mcp_health.log.{dir_slot}"
    slot_dir.mkdir()
    if file_slot is not None:
        (tmp_path / f"mcp_health.log.{file_slot}").write_text("kept")
    if dir_slot == 2:
        (tmp_path / "mcp_health.log.1").write_text("kept")
    result = _rotate(f)
    _assert_left_as_is(tmp_path, f, result)
    assert list(slot_dir.iterdir()) == [], "a copy was moved into the directory"
    for slot in (1, 2):
        p = tmp_path / f"mcp_health.log.{slot}"
        if p.is_file():
            assert p.read_text() == "kept"


def test_a_symlink_to_a_directory_in_a_slot_stops_the_rotation(tmp_path):
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / "mcp_health.log.1")
    result = _rotate(f)
    _assert_left_as_is(tmp_path, f, result)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a mode-0444 file, so there is no failure to observe")
def test_a_log_that_cannot_be_truncated_is_not_rotated(tmp_path):
    """Codex P2 / Devin round 1: a read-only live log (its writers keep their
    append handles) was copied, the history shifted, the truncate failed
    unchecked, and the job still printed "rotated"."""
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    (tmp_path / "mcp_health.log.1").write_text("one")
    (tmp_path / "mcp_health.log.2").write_text("two")
    f.chmod(0o444)
    result = _rotate(f)
    _assert_left_as_is(tmp_path, f, result)
    assert (tmp_path / "mcp_health.log.1").read_text() == "one"
    assert (tmp_path / "mcp_health.log.2").read_text() == "two"


def _rotate_with_cp_side_effect(tmp_path: Path, f: Path, after_copy: str) -> subprocess.CompletedProcess:
    """Run rotate_log with a cp that copies for real and then runs `after_copy`,
    so the tree changes AFTER the up-front checks, as a race would."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    real_cp = shutil.which("cp")
    (bindir / "cp").write_text(f'#!/bin/sh\n"{real_cp}" "$@" || exit $?\n{after_copy}\n')
    (bindir / "cp").chmod(0o755)
    return subprocess.run(
        ["bash", "-c", 'source "$1"; PATH="$2:$PATH"; rotate_log "$3" 1000 2', "_",
         str(_HYGIENE), str(bindir), str(f)],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )


def test_a_directory_appearing_after_the_checks_never_receives_the_copy(tmp_path):
    """The mv -T backstop: a directory created at FILE.1 between the slot check
    and the move must make the move fail, not swallow the copy."""
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    slot = tmp_path / "mcp_health.log.1"
    result = _rotate_with_cp_side_effect(tmp_path, f, f'mkdir "{slot}"')
    assert "rotated" not in result.stdout
    assert list(slot.iterdir()) == [], "the copy was moved into the directory"
    survivors = [f, *tmp_path.glob(".mcp_health.log.rotate.*")]
    assert any(p.read_text() == "current\n" * 200 for p in survivors), "the log's content was lost"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a mode-0444 file, so there is no failure to observe")
def test_a_truncate_that_fails_after_the_checks_keeps_every_rotation(tmp_path):
    """Architect review: the log turning read-only after the -w check made the
    truncate fail only after the history had shifted, dropping the oldest copy."""
    f = tmp_path / "mcp_health.log"
    f.write_text("current\n" * 200)
    (tmp_path / "mcp_health.log.1").write_text("one")
    (tmp_path / "mcp_health.log.2").write_text("two")
    result = _rotate_with_cp_side_effect(tmp_path, f, f'chmod 444 "{f}"')
    assert "rotated" not in result.stdout
    assert (tmp_path / "mcp_health.log.1").read_text() == "one"
    assert (tmp_path / "mcp_health.log.2").read_text() == "two"
    assert f.read_text() == "current\n" * 200
    assert not list(tmp_path.glob(".mcp_health.log.rotate.*"))
