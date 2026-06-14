"""Tests for atomic file writes (genesis.util.atomic)."""

from __future__ import annotations

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
