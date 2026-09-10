"""Tests for atomic file writes (genesis.util.atomic)."""

from __future__ import annotations

import pytest

from genesis.util.atomic import atomic_write_text


def test_writes_content(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'


def test_overwrites_existing(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "data")
    # The tmp+rename must not leave stray temp files behind.
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "dir" / "out.txt"
    atomic_write_text(target, "x")
    assert target.read_text() == "x"


def test_a_failed_write_leaves_no_temp_behind(tmp_path):
    """The `except BaseException:` cleanup branch -- untested until now, and the
    branch every fix in the atomic-write class is about to depend on.

    Triggered by a REAL failure rather than a monkeypatch: renaming onto an
    existing DIRECTORY raises IsADirectoryError from os.replace, after the temp
    has already been created and written. A patched os.replace would test the
    handler against a fiction; this exercises the path the operating system
    actually takes.
    """
    target = tmp_path / "iam_a_dir"
    target.mkdir()

    with pytest.raises(OSError):
        atomic_write_text(target, "hello")

    # The directory itself survives, and nothing else was left beside it.
    assert [p.name for p in tmp_path.iterdir()] == ["iam_a_dir"]


def test_the_original_survives_a_failed_write(tmp_path):
    """A failed atomic write must not damage what was already there -- that is
    the whole reason for the temp+rename dance, and no test asserted it."""
    target = tmp_path / "out.txt"
    target.write_text("original")

    # A non-str reaches `f.write` and raises TypeError from the file object.
    # Asserting the SPECIFIC type matters: `pytest.raises(Exception)` would also
    # pass if the write succeeded and something unrelated blew up afterwards,
    # which is the failure mode this test exists to rule out.
    with pytest.raises(TypeError):
        atomic_write_text(target, object())  # type: ignore[arg-type]

    assert target.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]
