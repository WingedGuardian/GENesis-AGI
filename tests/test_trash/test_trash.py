"""genesis.trash: per-volume rename into a Genesis-owned trash, with tombstones.

The suite-wide ``_isolate_trash_root`` fixture points the home trash at
``tmp_path / "genesis-trash"``, so nothing here touches the live trash.
"""

from __future__ import annotations

import contextlib
import json
import os
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
    with pytest.raises(TrashRefused, match="already in the trash"):
        trash(_entry(stone) / ITEM, reason="r", caller="c")


def test_the_claude_code_temp_volume_is_refused(monkeypatch, tmp_path):
    gh = tmp_path / "gh"
    (gh / "cc-tmp").mkdir(parents=True)
    f = _touch(gh / "cc-tmp" / "scratch.txt")
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    with pytest.raises(TrashRefused, match="temp volume"):
        trash(f, reason="r", caller="c")
    assert f.exists()


def test_another_volume_is_refused_and_never_copied(monkeypatch, tmp_path, root):
    # Fake the trash parent's device only: faking the ITEM's device would also
    # flip os.path.ismount, which compares an item's device with its parent's.
    f = _touch(tmp_path / "a.txt")
    real_stat = os.stat

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if Path(p) == root.parent:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 999) + tuple(st)[3:])
        return st

    monkeypatch.setattr(gt.os, "stat", fake_stat)
    with pytest.raises(TrashRefused, match="another volume") as exc:
        trash(f, reason="r", caller="c")
    assert "Ask the user" in str(exc.value)  # never an invitation to rm it instead
    assert f.read_text() == "x"


@pytest.mark.parametrize("raw", ["", ".", "..", "sub/.", "sub/..", "sub/./", "/"])
def test_dot_paths_are_refused_before_normalising(monkeypatch, tmp_path, raw):
    (tmp_path / "sub").mkdir()
    _touch(tmp_path / "sub" / "keep.txt")
    monkeypatch.chdir(tmp_path / "sub")
    with pytest.raises(TrashRefused, match="not a trashable path"):
        trash(raw, reason="r", caller="c")
    assert (tmp_path / "sub" / "keep.txt").exists()


def test_an_entry_that_cannot_be_created_is_a_refusal(monkeypatch, tmp_path, root):
    import errno

    f = _touch(tmp_path / "a.txt")
    real_mkdir = os.mkdir

    def full(p, *a, **k):
        if Path(p).parent == root:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_mkdir(p, *a, **k)

    monkeypatch.setattr(gt.os, "mkdir", full)
    with pytest.raises(TrashRefused, match="cannot create a trash entry"):
        trash(f, reason="r", caller="c")
    assert f.exists()


@pytest.mark.parametrize("bad", ["loop", "nul"])
def test_an_unreadable_path_is_a_refusal_not_a_crash(tmp_path, bad):
    if bad == "loop":
        (tmp_path / "l1").symlink_to(tmp_path / "l2")
        (tmp_path / "l2").symlink_to(tmp_path / "l1")
        target = tmp_path / "l1" / "x"
    else:
        target = str(tmp_path / "a\0b")
    with pytest.raises(TrashRefused):
        trash(target, reason="r", caller="c")


def test_a_long_multibyte_name_is_cut_on_a_character(tmp_path):
    # 2 + 240 bytes: with the 18-byte stamp prefix a 200-byte cut lands
    # mid-character, which a naive byte slice would split.
    name = "ab" + "\u00e9" * 120
    stone = trash(_touch(tmp_path / name), reason="r", caller="c")
    assert len(os.fsencode(stone.entry_id)) <= gt._MAX_ENTRY_NAME
    stone.entry_id.encode("utf-8")  # no lone surrogate from a split code point
    assert restore(stone.entry_id) == tmp_path / name


def test_a_symlinked_genesis_home_still_sees_its_own_trash(monkeypatch, tmp_path):
    real = tmp_path / "realg"
    real.mkdir()
    link = tmp_path / "linkg"
    link.symlink_to(real)
    monkeypatch.setattr(gt, "home_trash_root", lambda: link / "trash")
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    inside = real / "trash" / stone.entry_id / ITEM
    with pytest.raises(TrashRefused, match="already in the trash"):
        trash(inside, reason="r", caller="c")
    assert [e.complete for e in list_entries()] == [True]


def test_restore_into_an_unreadable_place_is_a_refusal(tmp_path):
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        with pytest.raises(TrashRefused):
            restore(stone.entry_id, to=locked / "sub" / "a.txt")
    finally:
        locked.chmod(0o700)
    assert (_entry(stone) / ITEM).exists()


def test_a_cross_device_rename_failure_leaves_no_entry(monkeypatch, tmp_path, root):
    import errno

    f = _touch(tmp_path / "a.txt")

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(gt.os, "rename", exdev)
    with pytest.raises(TrashRefused, match="another volume"):
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


def test_cli_output_survives_odd_names(tmp_path, capsys):
    trash(_touch(tmp_path / os.fsdecode(b"bad\xff")), reason="r", caller="c")
    trash(_touch(tmp_path / "two\nlines"), reason="r", caller="c")
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    out.encode("utf-8")  # printable on a strict UTF-8 terminal
    assert len(out.splitlines()) == 2  # one line per entry
    assert "two\\nlines" in out and "bad\\xff" in out


def test_a_symlink_loop_in_the_trash_location_is_a_refusal(monkeypatch, tmp_path):
    (tmp_path / "la").symlink_to(tmp_path / "lb")
    (tmp_path / "lb").symlink_to(tmp_path / "la")
    monkeypatch.setattr(gt, "home_trash_root", lambda: tmp_path / "la" / "x" / "trash")
    f = _touch(tmp_path / "a.txt")
    with pytest.raises(TrashRefused):
        trash(f, reason="r", caller="c")
    assert f.exists()
    with pytest.raises(TrashRefused, match="cannot read the trash"):
        list_entries()


def test_cli_list_survives_a_hand_edited_tombstone(tmp_path, capsys):
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    tomb = _entry(stone) / TOMBSTONE
    data = json.loads(tomb.read_text())
    data["reason"] = "\ud800"
    tomb.write_text(json.dumps(data))
    assert main(["list"]) == 0
    capsys.readouterr().out.encode("utf-8")


def test_an_unreadable_trash_is_reported_not_shown_empty(tmp_path, root, capsys):
    trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    root.chmod(0)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        assert main(["list"]) == EXIT_REFUSED
        # _check_root fires first now; listdir's "cannot read" is reachable
        # only through an ACL or LSM denial on a 0700 root.
        assert "is not mode 0700" in capsys.readouterr().err
    finally:
        root.chmod(0o700)
    assert list_entries()  # readable again, entry intact


@pytest.mark.parametrize("shape", ["mode", "symlink"])
def test_restore_refuses_a_root_that_is_no_longer_ours(tmp_path, root, shape):
    stone = trash(_touch(tmp_path / "a.txt"), reason="r", caller="c")
    if shape == "mode":
        root.chmod(0o755)
    else:
        moved = tmp_path / "moved-trash"
        root.rename(moved)
        root.symlink_to(moved)
    with pytest.raises(TrashRefused):
        restore(stone.entry_id)
    with pytest.raises(TrashRefused):
        list_entries()
    assert not (tmp_path / "a.txt").exists()


def test_a_trash_not_created_yet_is_empty():
    assert list_entries() == []


def test_cli_usage_error_exits_64():
    with pytest.raises(SystemExit) as exc:
        main(["restor", "x"])  # no abbreviations
    assert exc.value.code == EXIT_USAGE
