"""``python -m genesis.trash put`` and the live-database refusal (#2926 PR 6a).

The suite-wide fixtures keep the trash in tmp and point ``genesis_db_path`` at
``tmp_path / "isolated-genesis.db"``. Always read it through the ``genesis.env``
module: a name imported before the fixture runs is the REAL path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import genesis.env as env
from genesis.trash import ITEM, TrashRefused, list_entries, trash
from genesis.trash.__main__ import EXIT_REFUSED, EXIT_USAGE, main


def _touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_put_trashes_every_path_and_prints_each_id(tmp_path, capsys):
    a = _touch(tmp_path / "a.txt", "aa")
    d = tmp_path / "dir"
    _touch(d / "b.txt")
    assert main(["put", str(a), str(d), "--reason", "tidy up"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert not a.exists() and not d.exists()
    entries = list_entries()
    assert {e.tombstone.caller for e in entries} == {"cli"}
    assert {e.tombstone.reason for e in entries} == {"tidy up"}
    assert len(out) == 2 and all(e.path.name in "\n".join(out) for e in entries)


def test_one_refusal_does_not_stop_the_rest(tmp_path, capsys):
    a = _touch(tmp_path / "a.txt")
    missing = tmp_path / "missing.txt"
    assert main(["put", str(missing), str(a), "--reason", "r"]) == EXIT_REFUSED
    assert not a.exists()  # trashed despite the earlier refusal
    assert "does not exist" in capsys.readouterr().err  # unlike rm -f


def test_put_needs_a_reason(tmp_path):
    a = _touch(tmp_path / "a.txt")
    with pytest.raises(SystemExit) as exc:
        main(["put", str(a)])
    assert exc.value.code == EXIT_USAGE
    assert a.exists()


@pytest.mark.parametrize("which", ["file", "wal", "shm", "dir"])
def test_the_live_database_is_never_trashed(which):
    db = env.genesis_db_path()
    _touch(db, "db")
    target = {
        "file": db,
        "wal": _touch(db.with_name(db.name + "-wal")),
        "shm": _touch(db.with_name(db.name + "-shm")),
        "dir": None,
    }[which]
    if which == "dir":
        # The isolated database sits directly in tmp_path, which the trash
        # already refuses as a parent of itself; a fresh holder isolates the rule.
        holder = db.parent / "holder"
        holder.mkdir()
        inner = _touch(holder / db.name)
        mp = pytest.MonkeyPatch()
        mp.setattr(env, "genesis_db_path", lambda: inner)
        try:
            with pytest.raises(TrashRefused, match="default Genesis database"):
                trash(holder, reason="r", caller="c")
        finally:
            mp.undo()
        assert inner.exists()
        return
    with pytest.raises(TrashRefused, match="default Genesis database"):
        trash(target, reason="r", caller="c")
    assert target.exists()


def test_an_old_database_copy_beside_it_can_be_trashed():
    db = env.genesis_db_path()
    _touch(db, "db")
    old = _touch(db.with_name(db.name + ".pre-restore.1"))
    stone = trash(old, reason="r", caller="c")
    assert not old.exists() and db.exists()
    assert stone.name == old.name
    assert (Path(stone.root) / stone.entry_id / ITEM).exists()


def test_the_database_path_is_isolated_in_tests(tmp_path):
    # Guard-the-guard: these tests must never touch a real database file.
    assert env.genesis_db_path().is_relative_to(tmp_path)


def test_a_symlink_at_the_configured_path_is_refused(tmp_path, monkeypatch):
    real = _touch(tmp_path / "real" / "genesis.db", "db")
    link = tmp_path / "linkdir" / "genesis.db"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(env, "genesis_db_path", lambda: link)
    with pytest.raises(TrashRefused, match="default Genesis database"):
        trash(link, reason="r", caller="c")
    assert link.is_symlink()


def test_an_empty_reason_is_a_usage_error(tmp_path):
    a = _touch(tmp_path / "a.txt")
    with pytest.raises(SystemExit) as exc:
        main(["put", str(a), "--reason", "  "])
    assert exc.value.code == EXIT_USAGE
    assert a.exists()


# The server's database path comes from secrets.env, read by systemd and again by
# dotenv, so it is not re-derived here. The hazard itself is checked instead: a
# path some running process is using. Holders run in a SEPARATE process, as the
# server does, so a scan of only this process would fail these tests.

_HOLD = {
    "open": "f = open(sys.argv[1], 'rb')",
    # libc mmap, then close the descriptor: Python's mmap module keeps a dup of
    # it, which would make this an "open" holder. The vector store maps its
    # segments with no descriptor behind them, and that is the shape tested.
    "map": (
        "import ctypes, os\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.mmap.restype = ctypes.c_void_p\n"
        "libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,"
        " ctypes.c_int, ctypes.c_int, ctypes.c_long]\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
        "addr = libc.mmap(None, os.fstat(fd).st_size, 1, 1, fd, 0)\n"  # PROT_READ, MAP_SHARED
        "assert addr not in (None, ctypes.c_void_p(-1).value)\n"
        "os.close(fd)"
    ),
    "cwd": "import os; os.chdir(sys.argv[1])",
}


@pytest.fixture
def held():
    """Start a process that uses a path (open, mapped, or as its cwd) until the test ends."""
    procs = []

    def hold(how, path):
        code = f"import sys, time\n{_HOLD[how]}\nprint('ready', flush=True)\ntime.sleep(120)"
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True
        )
        procs.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        return proc.pid

    yield hold
    for proc in procs:
        proc.kill()
        proc.wait()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(env, "repo_root", lambda: root)
    return root


@pytest.mark.parametrize(("how", "says"), [("open", "open"), ("map", "memory-mapped")])
def test_a_file_another_process_uses_is_refused(tmp_path, held, how, says):
    live = _touch(tmp_path / "elsewhere" / "live.db", "db")
    pid = held(how, live)
    with pytest.raises(TrashRefused, match=rf"process {pid} has it \(or a file under it\) {says}"):
        trash(live, reason="r", caller="c")
    assert live.read_text() == "db"


@pytest.mark.parametrize("how", ["open", "map", "cwd"])
def test_a_directory_another_process_uses_is_refused(tmp_path, held, how):
    store = tmp_path / "store"
    live = _touch(store / "sub" / "live.db", "db")
    pid = held(how, store / "sub" if how == "cwd" else live)
    with pytest.raises(TrashRefused, match=rf"process {pid} "):
        trash(store, reason="r", caller="c")
    assert live.exists()
    assert list_entries() == []  # the refused attempt left no entry behind


def test_a_sibling_with_the_same_prefix_is_not_a_holder(tmp_path, held):
    _touch(tmp_path / "store2" / "live.db", "db")
    held("open", tmp_path / "store2" / "live.db")
    item = _touch(tmp_path / "store" / "notes.txt")
    trash(item.parent, reason="r", caller="c")
    assert not item.parent.exists()


def test_a_deleted_open_file_does_not_hold_its_directory(tmp_path, held):
    store = tmp_path / "store"
    gone = _touch(store / "gone.log")
    held("open", gone)
    gone.unlink()
    trash(store, reason="r", caller="c")
    assert not store.exists()


def test_the_same_file_is_trashed_once_nothing_holds_it(tmp_path):
    item = _touch(tmp_path / "elsewhere" / "done.db", "db")
    code = f"import sys, time\n{_HOLD['open']}\nprint('ready', flush=True)\ntime.sleep(120)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(item)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "ready"
    with pytest.raises(TrashRefused, match="has it .* open"):
        trash(item, reason="r", caller="c")
    proc.kill()
    proc.wait()
    trash(item, reason="r", caller="c")
    assert not item.exists()


def test_a_symlink_to_an_open_file_is_trashable(tmp_path, held):
    """Moving a link leaves what it points at in place, and the holder still has it.
    (A later opener that goes through the link path would find nothing there; the
    configured database's path is refused statically, see the symlink test above.)"""
    live = _touch(tmp_path / "real.db", "db")
    link = tmp_path / "links" / "db.lnk"
    link.parent.mkdir()
    link.symlink_to(live)
    held("open", live)
    trash(link, reason="r", caller="c")
    assert live.exists() and not link.is_symlink()


def test_the_default_location_stays_protected_when_another_is_configured(repo):
    default = _touch(repo / "data" / "genesis.db", "db")
    with pytest.raises(TrashRefused, match="default Genesis database"):
        trash(default, reason="r", caller="c")
    assert default.exists()


def test_an_unresolvable_database_path_refuses_instead_of_crashing(tmp_path, monkeypatch):
    def unresolvable():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(env, "genesis_db_path", unresolvable)
    item = _touch(tmp_path / "plain.txt")
    with pytest.raises(TrashRefused, match="could not be determined"):
        trash(item, reason="r", caller="c")
    assert item.exists()
