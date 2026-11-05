"""resolve_refusing_loops behaves the same on every supported Python."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.util.resolve import resolve_refusing_loops


@pytest.mark.parametrize("child", ["", "below"])
def test_a_loop_raises_whether_it_is_the_path_or_a_parent(tmp_path, child):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises((OSError, RuntimeError)):
        resolve_refusing_loops(loop / child if child else loop)


def test_a_two_link_loop_raises(tmp_path):
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    with pytest.raises((OSError, RuntimeError)):
        resolve_refusing_loops(tmp_path / "a")


def test_a_real_path_and_a_link_resolve_as_usual(tmp_path):
    real = tmp_path / "real"
    real.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert resolve_refusing_loops(real) == real.resolve()
    assert resolve_refusing_loops(link) == real.resolve()


def test_a_missing_path_still_resolves(tmp_path):
    assert resolve_refusing_loops(tmp_path / "absent" / "x") == (tmp_path / "absent" / "x").resolve()


def test_a_dangling_link_still_resolves_to_its_target(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone")
    assert resolve_refusing_loops(link) == (tmp_path / "gone").resolve()


def test_a_path_under_a_file_still_resolves(tmp_path):
    """stat answers ENOTDIR, which is not a loop and has never been a refusal."""
    (tmp_path / "file").write_text("x")
    target = tmp_path / "file" / "below"
    assert resolve_refusing_loops(target) == target.resolve()


def test_an_unreadable_parent_still_resolves(tmp_path):
    """stat answers EACCES, which a plain resolve() has never raised on."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert resolve_refusing_loops(locked / "x") == Path(locked / "x").resolve()
    finally:
        locked.chmod(0o700)
