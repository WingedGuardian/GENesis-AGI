"""genesis.trash: per-volume rename into a Genesis-owned trash, with tombstones.

The suite-wide ``_isolate_trash_root`` fixture points the home trash at
``tmp_path / "genesis-trash"``, so nothing here touches the live trash.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

import genesis.trash as gt
from genesis.trash import ITEM, TOMBSTONE, TrashRefused, list_entries, restore, trash
from genesis.trash.__main__ import EXIT_REFUSED, EXIT_USAGE, main


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "genesis-trash"


def _entry(stone) -> Path:
    return Path(stone.root) / stone.entry_id


# ── trash ────────────────────────────────────────────────────────────────────


def test_a_file_is_renamed_into_the_trash_with_a_tombstone(tmp_path, root):
    f = tmp_path / "notes.md"
    f.write_text("keep me")
    stone = trash(f, reason="test", caller="unit")
    assert not f.exists()
    entry = _entry(stone)
    assert entry.parent == root
    assert (entry / ITEM).read_text() == "keep me"
    data = json.loads((entry / TOMBSTONE).read_text())
    assert data["original_path"] == str(f)
    assert data["name"] == "notes.md"
    assert data["kind"] == "file" and data["size"] == 7
    assert data["reason"] == "test" and data["caller"] == "unit"
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700


def test_a_directory_moves_whole(tmp_path):
    d = tmp_path / "work"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("abc")
    stone = trash(d, reason="r", caller="c")
    assert stone.kind == "dir" and stone.size == 3
    assert (_entry(stone) / ITEM / "sub" / "a.txt").read_text() == "abc"


def test_a_symlink_is_trashed_as_the_link_not_its_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("t")
    link = tmp_path / "link"
    link.symlink_to(target)
    stone = trash(link, reason="r", caller="c")
    assert stone.kind == "symlink"
    assert target.read_text() == "t"  # the target is untouched
    assert os.path.islink(_entry(stone) / ITEM)


def test_a_file_named_like_the_tombstone_cannot_overwrite_it(tmp_path):
    f = tmp_path / TOMBSTONE
    f.write_text("user data, not a tombstone")
    stone = trash(f, reason="r", caller="c")
    entry = _entry(stone)
    assert json.loads((entry / TOMBSTONE).read_text())["name"] == TOMBSTONE
    assert (entry / ITEM).read_text() == "user data, not a tombstone"


def test_the_same_name_twice_gets_distinct_entries(tmp_path):
    a = trash(_touch(tmp_path / "x.txt"), reason="r", caller="c")
    b = trash(_touch(tmp_path / "x.txt"), reason="r", caller="c")
    assert a.entry_id != b.entry_id
    assert _entry(a).exists() and _entry(b).exists()


def _touch(p: Path) -> Path:
    p.write_text("x")
    return p


def test_an_unreadable_subtree_makes_the_size_unknown(tmp_path):
    d = tmp_path / "work"
    (d / "locked").mkdir(parents=True)
    (d / "locked" / "a.txt").write_text("abc")
    (d / "b.txt").write_text("xy")
    (d / "locked").chmod(0)
    try:
        if os.access(d / "locked", os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        stone = trash(d, reason="r", caller="c")
    finally:
        with contextlib.suppress(OSError):
            (d / "locked").chmod(0o700)
        for p in Path(tmp_path).rglob("locked"):
            with contextlib.suppress(OSError):
                p.chmod(0o700)
    assert stone.size is None  # never a partial total that reads as exact


def test_entry_ids_are_unique_and_an_ambiguous_restore_is_refused(monkeypatch, tmp_path, root):
    a = trash(_touch(tmp_path / "x.txt"), reason="r", caller="c")
    b = trash(_touch(tmp_path / "x.txt"), reason="r", caller="c")
    assert a.entry_id != b.entry_id
    other = tmp_path / "other-root"
    other.mkdir(mode=0o700)
    shutil.copytree(_entry(a), other / a.entry_id)  # the same id in a second root
    monkeypatch.setattr(gt, "_roots", lambda: [root, other])
    with pytest.raises(TrashRefused, match="more than one root"):
        restore(a.entry_id)
    assert (_entry(a) / ITEM).exists()


def test_two_roots_never_issue_the_same_id_in_the_same_second(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r1.mkdir()
    r2.mkdir()
    assert gt._claim_entry(r1, "x.txt").name != gt._claim_entry(r2, "x.txt").name


def test_one_directory_behind_two_mount_points_is_one_root(tmp_path):
    real = tmp_path / "m1"
    (real / f".genesis-trash-{os.getuid()}").mkdir(parents=True, mode=0o700)
    alias = tmp_path / "m2"
    alias.symlink_to(real)
    roots = gt._roots_from([str(real), str(alias), str(tmp_path / "none")])
    assert roots[1:] == [real / f".genesis-trash-{os.getuid()}"]


def test_a_file_gone_mid_scan_keeps_the_size_known(monkeypatch, tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "a.txt").write_text("abc")
    (d / "gone.txt").write_text("zz")
    real_lstat = os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("gone.txt"):
            raise FileNotFoundError(p)
        return real_lstat(p, *a, **k)

    monkeypatch.setattr(gt.os, "lstat", lstat)
    st = real_lstat(d)
    assert gt._size(d, st) == 3


def test_mount_escapes_are_all_decoded():
    assert gt._unescape_mount("/mnt/a\\040b\\011c\\012d\\134e") == "/mnt/a b\tc\nd\\e"
    assert gt._unescape_mount("/plain") == "/plain"


def test_an_undecodable_name_is_trashed_and_restored(tmp_path):
    name = os.fsdecode(b"bad\xff.md")
    f = _touch(tmp_path / name)
    stone = trash(f, reason="r", caller="c")
    assert not f.exists()
    assert restore(stone.entry_id) == f and f.read_text() == "x"


# ── refusals: the item is left untouched ────────────────────────────────────


def test_a_missing_path_is_refused(tmp_path):
    with pytest.raises(TrashRefused, match="does not exist"):
        trash(tmp_path / "nope", reason="r", caller="c")


def test_a_parent_of_the_trash_root_is_refused(tmp_path, root):
    _touch(tmp_path / "keep.txt")
    with pytest.raises(TrashRefused, match="contains"):
        trash(tmp_path, reason="r", caller="c")
    assert (tmp_path / "keep.txt").exists()


def test_home_is_refused(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(TrashRefused, match="contains"):
        trash(home, reason="r", caller="c")
    assert home.is_dir()


def test_an_item_already_in_the_trash_is_refused(tmp_path):
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    with pytest.raises(TrashRefused, match="already in a trash"):
        trash(_entry(stone) / ITEM, reason="r", caller="c")


def test_the_claude_code_temp_volume_is_refused(monkeypatch, tmp_path):
    gh = tmp_path / "gh"
    (gh / "cc-tmp").mkdir(parents=True)
    f = _touch(gh / "cc-tmp" / "scratch.txt")
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    with pytest.raises(TrashRefused, match="temp volume"):
        trash(f, reason="r", caller="c")
    assert f.exists()


def test_another_device_is_refused_and_never_copied(monkeypatch, tmp_path):
    # Fake only the trash root's parent device: faking the ITEM's device would
    # also flip os.path.ismount, which compares an item's device with its parent's.
    f = _touch(tmp_path / "a.txt")
    other = tmp_path / "other-volume"
    other.mkdir()
    monkeypatch.setattr(gt, "_root_for", lambda dev, item: other / "trash")
    real_stat = os.stat

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if Path(p) == other:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 999) + tuple(st)[3:])
        return st

    monkeypatch.setattr(gt.os, "stat", fake_stat)
    with pytest.raises(TrashRefused, match="another device"):
        trash(f, reason="r", caller="c")
    assert f.read_text() == "x"


def test_a_cross_device_rename_failure_leaves_no_entry(monkeypatch, tmp_path, root):
    import errno

    f = _touch(tmp_path / "a.txt")

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(gt.os, "rename", exdev)
    with pytest.raises(TrashRefused, match="another device"):
        trash(f, reason="r", caller="c")
    assert f.exists()
    assert list(root.iterdir()) == []  # the claimed entry was removed


@pytest.mark.parametrize("shape", ["symlink", "foreign_mode"])
def test_a_hijacked_trash_root_is_refused(tmp_path, root, shape):
    f = _touch(tmp_path / "a.txt")
    if shape == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        root.symlink_to(elsewhere)
    else:
        root.mkdir(mode=0o700)
        root.chmod(0o755)
    expected = "is not a plain directory" if shape == "symlink" else "is not mode 0700"
    with pytest.raises(TrashRefused, match=expected):
        trash(f, reason="r", caller="c")
    assert f.exists()


# ── list / restore ───────────────────────────────────────────────────────────


def test_restore_puts_the_item_back_and_clears_the_entry(tmp_path):
    f = _touch(tmp_path / "a.txt")
    stone = trash(f, reason="r", caller="c")
    assert restore(stone.entry_id) == f
    assert f.read_text() == "x"
    assert not _entry(stone).exists()


def test_restore_refuses_an_existing_destination(tmp_path):
    f = _touch(tmp_path / "a.txt")
    stone = trash(f, reason="r", caller="c")
    f.write_text("new")
    with pytest.raises(TrashRefused, match="already exists"):
        restore(stone.entry_id)
    assert f.read_text() == "new"
    assert (_entry(stone) / ITEM).exists()


def test_an_incomplete_entry_is_listed_and_not_restorable(tmp_path):
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    (_entry(stone) / ITEM).unlink()  # a crash between tombstone and rename
    [entry] = list_entries()
    assert entry.complete is False and entry.tombstone is not None
    with pytest.raises(TrashRefused, match="is incomplete"):
        restore(stone.entry_id)


def test_cli_list_and_restore(tmp_path, capsys):
    f = _touch(tmp_path / "a.txt")
    stone = trash(f, reason="why", caller="who")
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert stone.entry_id in out and "who: why" in out
    assert main(["restore", stone.entry_id]) == 0
    assert f.exists()
    assert main(["restore", stone.entry_id]) == EXIT_REFUSED


def test_cli_usage_error_exits_64():
    with pytest.raises(SystemExit) as exc:
        main(["restor", "x"])  # no abbreviations
    assert exc.value.code == EXIT_USAGE
