"""`live_move_repair.py --restore TS` (#3492 PR B2b): the restore that WRITES.

Every case is a REAL repair in the scratch world (_live_repair_world.py),
then a real restore: it puts back each part B1's judge calls `as-left`, under
the repair's locks, and refuses every other part by name. Its write per node:
stage the saved copy in a run folder under deploy-backups (hash checked
against SAVED.json), record the part in flight, rename what is at the path
aside into the run folder, re-judge the aside, and put the staged node in
with renameat2(RENAME_NOREPLACE), which never replaces anything. Then one
`update-index --index-info` for the index parts, the flags, a refresh, and a
second judge that must read `already-restored` for every part written.

A killed restore is a real SIGKILL at a named step (the restore module's
KILL_ENV hook); a write by "a session" between two steps is the module's
step hook replaced in-process.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    LIB,
    MODULE,
    World,
    _ancestor_file,
    _dir_becomes_file,
    _foreign_both_flags,
    _foreign_dir_symlink_inside_dir,
    _foreign_edit,
    _foreign_fifo_inside_dir,
    _foreign_gitlink,
    _foreign_inside_dir,
    _foreign_skip_worktree,
    _foreign_staged,
    _foreign_symlink,
    _foreign_unmerged,
    _foreign_untracked,
    _git,
    _git_env,
    _hash,
    _lock_aside,
    age_locks,
    killed_tag_side,
    update_bytes,
)
from tests.test_scripts.test_live_move_restore import (
    _ancestor_file_child_edit,
    _check,
    _edit_report,
    _newer_dir,
    _parts,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")

SUFFIX = "-live-move-repair"
KILL_ENV = "GENESIS_LIVE_RESTORE_TEST_KILL"


def _restore(w, ts: str, *extra: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--restore", ts, *extra, str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(**env),
        timeout=600,
    )


def _repaired(w, make) -> str:
    """A real repair of a killed switch with ``make``'s foreign path; its ts."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: make(w))
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(row["class"] == "foreign" for row in w.report()["paths"]), "control: none foreign"
    return w.backups()[-1].name[: -len(SUFFIX)]


def _state_of(w, path: str) -> dict:
    """What the checkout holds at and below ``path``: nodes (bytes, links, FIFOs),
    their permission bits, its index entries and its flags."""
    snap = {k: v for k, v in w.snapshot().items() if k == path or k.startswith(path + "/")}
    modes = {}
    for k in snap:
        st = os.lstat(w.root / k)
        if not stat.S_ISLNK(st.st_mode):
            modes[k] = stat.S_IMODE(st.st_mode)
    return {
        "nodes": snap,
        "modes": modes,
        "index": _git(w.root, "ls-files", "-s", "--", path),
        "flags": _git(w.root, "ls-files", "-v", "--", path),
    }


def _runs(w) -> list[Path]:
    return sorted((w.gen / "deploy-backups").glob("*-live-move-restore"))


def _kit(name: str = "live_move_restore.py"):
    spec = importlib.util.spec_from_file_location(f"_kit_under_test_{name[:-3]}", LIB / name)
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    return kit


def _restore_in(w, mod, monkeypatch, capsys, ts: str, at: dict | None = None, patch=None):
    """The restore run in-process, the write kit's step hook replaced:
    ``at[name]()`` runs when the write path reaches the step ``name`` (a
    session's write there); ``patch(writes, monkeypatch)`` edits the write kit.
    Returns (rc, stdout)."""
    kits = {n: _kit(n) for n in ("live_move_restore.py", "live_move_restore_write.py")}
    writes = kits["live_move_restore_write.py"]
    if at:
        real = writes._step
        monkeypatch.setattr(
            writes, "_step", lambda name: (at.pop(name, lambda: None)(), real(name))
        )
    if patch:
        patch(writes, monkeypatch)
    load = mod._load_kit
    monkeypatch.setattr(mod, "_load_kit", lambda n: kits[n] if n in kits else load(n))
    capsys.readouterr()
    rc = mod.main(["--restore", ts, str(w.state), str(w.root)])
    return rc, capsys.readouterr().out


def _foreign_fifo(w):
    os.mkfifo(w.root / "Z5", 0o640)
    return "Z5"


def _foreign_staged_deletion(w):
    _git(w.root, "rm", "-qf", "A")
    return "A"


def _foreign_index_only(w):
    (w.root / "sub" / "s1").write_text("staged, not on disk\n")
    _git(w.root, "add", "sub/s1")
    (w.root / "sub" / "s1").write_text("s1\n")
    return "sub/s1"


def _foreign_assume_unchanged(w):
    _git(w.root, "update-index", "--assume-unchanged", "sub/s1")
    (w.root / "sub" / "s1").write_text("hidden edit\n")
    return "sub/s1"


def _foreign_mode_only(w):
    (w.root / "sub" / "s1").chmod(0o755)
    return "sub/s1"


def _foreign_private_file(w):
    (w.root / "Z5").write_text("private\n")
    (w.root / "Z5").chmod(0o600)
    return "Z5"


def _foreign_dir_at_s1(w):
    (w.root / "sub" / "s1").unlink()
    (w.root / "sub" / "s1").mkdir()
    (w.root / "sub" / "s1" / "mine.txt").write_text("notes\n")
    return "sub/s1"


def _dir_file(w):
    _dir_becomes_file(w)
    return "sub"


ROUND_TRIP = [
    _foreign_edit,
    _foreign_untracked,
    _foreign_inside_dir,
    _foreign_staged,
    _foreign_unmerged,
    _foreign_skip_worktree,
    _foreign_gitlink,
    _foreign_both_flags,
    _foreign_symlink,
    _foreign_dir_symlink_inside_dir,
    _foreign_fifo_inside_dir,
    _ancestor_file,
    _dir_file,
    _foreign_fifo,
    _foreign_staged_deletion,
    _foreign_index_only,
    _foreign_assume_unchanged,
    _foreign_mode_only,
    _foreign_private_file,
    _foreign_dir_at_s1,
]


def _minus_update(w, state: dict) -> dict:
    """``state`` without the files below a directory whose bytes are the move
    target's: the update's own, which a restore leaves out of a directory."""
    path = min(state["nodes"], key=len, default="")
    drop = {k for k, v in state["nodes"].items() if k != path and update_bytes(w, k, v)}
    return {**state, "nodes": {k: v for k, v in state["nodes"].items() if k not in drop},
            "modes": {k: v for k, v in state["modes"].items() if k not in drop}}  # fmt: skip


def _fresh(w, make) -> tuple[str, str, dict]:
    """A real repair over ``make``'s path; (ts, path, the path's state before,
    less the update's own files in a directory)."""
    killed_tag_side(w, "Z3")
    path = _lock_aside(w, lambda: make(w))
    before = _minus_update(w, _state_of(w, path))
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) != before, "control: the repair changed nothing there"
    return w.backups()[-1].name[: -len(SUFFIX)], path, before


def _converged(w, ts: str) -> None:
    c = _check(w, ts)
    assert c.returncode == 0 and set(_parts(c).values()) == {"already-restored"}, c.stdout
    block = w.report()["restore"]
    assert set(block) == {"inflight", "refused", "restored_at"}, block
    assert block["inflight"] is None and block["refused"] == 0, block
    assert isinstance(block["restored_at"], str), block


# ── the round trip: every kind the writer saves comes back exactly ──────────


@pytest.mark.parametrize("make", ROUND_TRIP)
def test_a_restore_puts_back_exactly_what_the_repair_saved(w, make):
    """Node, bytes, mode, index entries (unmerged stages too) and flags come
    back as they were before the repair (what the update itself wrote or
    removed, its engine rows, stays the tag's); B1's judge then reads every
    part already-restored, and the report records restored_at, 0 refused."""
    ts, path, before = _fresh(w, make)
    engine = {r["path"] for r in w.report()["paths"] if r["class"] == "engine"}

    def foreign(state: dict) -> dict:
        keep = {k: v for k, v in state.items() if k not in ("nodes", "modes")}
        for key in ("nodes", "modes"):
            keep[key] = {k: v for k, v in state[key].items() if k not in engine}
        return keep

    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    parts = _parts(r)
    assert parts and set(parts.values()) <= {"restored", "already-restored"}, r.stdout
    assert "restored" in parts.values(), r.stdout
    assert foreign(_state_of(w, path)) == foreign(before)
    _converged(w, ts)


def test_the_restored_mode_is_the_records_without_setuid(w):
    """Owner ruling: restored mode = record mode & 0o777 (setuid, setgid and
    sticky never come back); never the copy's, never the umask's."""
    ts, path, before = _fresh(w, lambda w: (_foreign_edit(w), (w.root / "sub/s1").chmod(0o4750))[0])
    assert w.report() and stat.S_IMODE(os.lstat(w.root / path).st_mode) != 0o4750
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert stat.S_IMODE(os.lstat(w.root / path).st_mode) == 0o750


def test_a_restored_file_gets_its_extended_attributes_back(w):
    """#3974 item 7: the saved copy keeps a file's user.* extended attributes
    (the repair's _copy_xattrs), and the restore copies them back with the
    bytes, so a restored file holds what the original did."""

    def make(w):
        path = _foreign_edit(w)
        try:
            os.setxattr(w.root / path, "user.genesis-test", b"kept")
        except OSError as exc:  # tmpfs before 6.6, some overlay setups
            pytest.skip(f"no user xattrs on this file system: {exc}")
        return path

    ts, path, before = _fresh(w, make)
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.getxattr(w.root / path, "user.genesis-test") == b"kept"


def test_the_replaced_node_is_kept_in_a_private_run_folder_before_the_write(w):
    """What the restore replaces is renamed into <deploy-backups>/<run>/aside/
    first (no copy, no temp in the checkout); the run folder is this user's
    alone (0700) and its name is the one a restore makes. REPAIR.json keeps
    only the scalars (review SF-B: no per-part list)."""
    ts, path, _ = _fresh(w, _foreign_edit)
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout
    [run] = _runs(w)
    assert stat.S_IMODE(os.lstat(run).st_mode) == 0o700
    assert (run / "aside" / path).read_text() == "s1\n"  # the tag's bytes the repair left
    assert str(run) in r.stdout
    assert set(w.report()["restore"]) == {"inflight", "refused", "restored_at"}
    assert not list(w.root.rglob("*genesis-restore*")), "a temp in the checkout"


def test_nothing_to_write_writes_nothing(w):
    """A re-run after a full restore: every part already-restored, nothing
    written, REPAIR.json untouched; no run folder made (made lazily)."""
    ts, _, _ = _fresh(w, _foreign_staged)
    assert _restore(w, ts).returncode == 0
    report = (w.backups()[-1] / "REPAIR.json").read_bytes()
    snap, runs = w.snapshot(), _runs(w)
    r = _restore(w, ts)
    assert r.returncode == 0 and set(_parts(r).values()) == {"already-restored"}, r.stdout
    assert (w.backups()[-1] / "REPAIR.json").read_bytes() == report
    assert w.snapshot() == snap and _runs(w) == runs


def test_a_restore_by_hand_still_records_restored_at(w):
    """Review N-4: a run that writes nothing but finds every part
    already-restored records restored_at, or the report never clears."""
    ts, _, _ = _fresh(w, _foreign_edit)
    assert _restore(w, ts).returncode == 0
    _edit_report(w, lambda m: m.pop("restore"))
    snap = w.snapshot()
    r = _restore(w, ts)
    assert r.returncode == 0 and w.snapshot() == snap and _runs(w) and len(_runs(w)) == 1
    _converged(w, ts)


# ── refusals: nothing that is not the as-left state is ever written over ────


def test_a_file_staged_below_the_path_is_refused(w):
    """#3721 Codex P1: a file staged below a gitlink's directory after the
    repair: the node part is refused and nothing there changes."""
    ts = _repaired(w, _foreign_gitlink)
    oid = _hash(w, "NEW\n")
    _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},Z5/new")
    snap, index = w.snapshot(), _git(w.root, "ls-files", "-s")
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("Z5", "node")] == "refused:other:index-changed", (
        r.stdout
    )
    assert w.snapshot() == snap and _git(w.root, "ls-files", "-s") == index


def test_a_disk_edit_after_the_repair_is_refused_and_kept(w):
    ts = _repaired(w, _foreign_edit)
    (w.root / "sub" / "s1").write_text("edited since\n")
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "refused:other:changed", r.stdout
    assert (w.root / "sub" / "s1").read_text() == "edited since\n"
    block = w.report()["restore"]
    assert block["refused"] == 1 and block["restored_at"] is None, block


def test_head_moved_on_the_path_is_refused(w):
    ts = _repaired(w, _foreign_edit)
    (w.root / "sub" / "s1").write_text("upstream s1\n")
    _git(w.root, "commit", "-qam", "upstream, the path")
    r = _restore(w, ts)
    assert _parts(r)[("sub/s1", "node")] == "refused:other:changed-upstream", r.stdout
    assert (w.root / "sub" / "s1").read_text() == "upstream s1\n"


def test_a_newer_report_overlapping_the_path_is_refused(w):
    ts = _repaired(w, _foreign_edit)
    d = _newer_dir(w, ts)
    (d / "REPAIR.json").write_text(
        json.dumps({"repo": str(w.root), "paths": [{"path": "sub", "class": "engine"}]})
    )
    r = _restore(w, ts)
    assert _parts(r)[("sub/s1", "node")] == "refused:other:newer-report", r.stdout
    assert (w.root / "sub" / "s1").read_text() == "s1\n"


def test_a_held_update_lock_is_refused_naming_the_holder(w):
    """Review NOTE-6: still held after the wait, the refusal names the pid the
    update state records."""
    ts = _repaired(w, _foreign_edit)
    w.write_state({**w.row(), "pid": os.getpid()})
    lock = w.gen / "locks" / "update.lock"
    lock.parent.mkdir(exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = _restore(w, ts, "--wait", "1")
    finally:
        os.close(fd)
    assert r.returncode == 1 and "update.lock" in r.stdout and f"pid {os.getpid()}" in r.stdout
    assert _parts(r) == {} and (w.root / "sub" / "s1").read_text() == "s1\n"


@pytest.mark.parametrize("phase", ["mixed", "bootstrap"])
def test_a_half_written_or_unfinished_update_refuses_the_whole_run(w, phase):
    """MIXED_TREE and (review LOW-1) GUARDS refuse every write; nothing is judged."""
    ts = _repaired(w, _foreign_edit)
    if phase == "mixed":
        w.write_state(w.row())
        (w.root / "A").write_text("a2 with more bytes\n")
    else:
        w.write_state(
            {k: v for k, v in {**w.row(), "phase": "bootstrap"}.items() if k != "live_move"}
        )
        assert w.guard_rc(json.loads(w.state.read_text())) == 0, "control: GUARDS"
    snap = w.snapshot()
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r) == {} and "nothing was" in r.stdout, r.stdout
    assert w.snapshot() == snap


def _group_write(w, where: str) -> Path:
    backup = w.backups()[-1]
    return {
        "deploy-backups": backup.parent,
        "report-dir": backup,
        "REPAIR.json": backup / "REPAIR.json",
        "SAVED.json": backup / "SAVED.json",
        "tree-file": backup / "tree" / "sub" / "s1",
        "tree-dir": backup / "tree" / "sub",
        "index-blob": next((backup / "index-blobs").iterdir()),
    }[where]


@pytest.mark.parametrize(
    "where",
    ["deploy-backups", "report-dir", "REPAIR.json", "SAVED.json", "tree-file", "tree-dir",
     "index-blob"],
)  # fmt: skip
def test_a_group_writable_node_anywhere_in_the_report_refuses_it_whole(w, where):
    """Origin rule (owner rulings ced82b49, 73b6285d): deploy-backups and every
    node of the report dir down are this user's with no group or world write;
    anything else refuses the whole report `other:not-owned <path>`, before
    REPAIR.json is parsed, and writes nothing."""
    ts = _repaired(w, lambda w: (_foreign_staged(w), _foreign_edit(w))[1])
    node = _group_write(w, where)
    node.chmod(stat.S_IMODE(os.lstat(node).st_mode) | 0o020)
    snap = w.snapshot()
    r = _restore(w, ts)
    assert r.returncode == 1 and "other:not-owned" in r.stdout and _parts(r) == {}, r.stdout
    assert w.snapshot() == snap and _runs(w) == []
    assert "other:not-owned" in _check(w, ts).stdout, "--check judges by the same origin rule"


def test_a_symlinked_deploy_backups_is_refused(w, tmp_path):
    ts = _repaired(w, _foreign_edit)
    real = tmp_path / "elsewhere"
    shutil.move(w.gen / "deploy-backups", real)
    os.symlink(real, w.gen / "deploy-backups")
    r = _restore(w, ts)
    assert r.returncode == 1 and "other:not-owned" in r.stdout and _parts(r) == {}, r.stdout
    assert (w.root / "sub" / "s1").read_text() == "s1\n"


def test_a_report_another_user_owns_is_refused(w, mod, monkeypatch, capsys):
    """Every node not the effective uid's refuses (the uid itself faked: a
    test cannot chown)."""
    ts = _repaired(w, _foreign_edit)

    def other_uid(kit, mp):
        mp.setattr(kit.os, "geteuid", lambda: os.getuid() + 1)

    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, patch=other_uid)
    assert rc == 1 and "other:not-owned" in out, out
    assert (w.root / "sub" / "s1").read_text() == "s1\n"


def test_a_node_part_below_another_refuses_the_report(w):
    """Review LOW-2: the writer never writes a node part below another (a
    directory row covers its children; a non-directory parent makes them
    unreachable), so a report holding one is refused whole, nothing written."""
    ts = _repaired(w, _foreign_edit)

    def nest(m):
        row = next(r for r in m["paths"] if r["path"] == "sub/s1")
        m["paths"].append({**row, "path": "sub/s1/deeper", "saved_as": "tree/sub/s1/deeper"})

    _edit_report(w, nest)
    snap = w.snapshot()
    r = _restore(w, ts)
    assert r.returncode == 1 and "a node part below another" in r.stdout, r.stdout
    assert _parts(r) == {} and w.snapshot() == snap


def _saved_json(w, change) -> None:
    path = w.backups()[-1] / "SAVED.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("damage", "verdict"),
    [
        ("copy-edited", "refused:other:hash-mismatch"),
        ("record-removed", "refused:other:no-hash"),
        ("hash-null", "refused:other:too-large"),
    ],
)
def test_a_copy_that_does_not_match_its_record_is_refused_and_nothing_moves(w, damage, verdict):
    """SF-3: the staged copy is checked against the writer's record (SAVED.json),
    never against what tree/ holds now: an edited copy, a copy with no record
    and one recorded with no hash are refused, before anything in the
    checkout moves."""
    ts = _repaired(w, _foreign_edit)
    if damage == "copy-edited":
        (w.backups()[-1] / "tree" / "sub" / "s1").write_text("not what was saved\n")
    elif damage == "record-removed":
        _saved_json(w, lambda d: (d["files"].pop("tree/sub/s1"), d.update(complete=False)))
    else:
        _saved_json(w, lambda d: d["files"]["tree/sub/s1"].update(sha256=None))
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == verdict, r.stdout
    assert (w.root / "sub" / "s1").read_text() == "s1\n"
    assert not any((run / "aside").exists() for run in _runs(w)), "something was moved aside"


@pytest.mark.parametrize("damage", ["copy-edited", "record-removed", "hash-null", "dir-entry-edited"])
def test_check_names_the_refusal_the_restore_makes_for_a_damaged_copy(w, damage):
    """#4025 item 3: --check reads a saved copy the restore's staging refuses
    exactly as the restore does (rc 1, the same reason), never as-left."""
    make = _foreign_inside_dir if damage == "dir-entry-edited" else _foreign_edit
    ts = _repaired(w, make)
    path = "P" if damage == "dir-entry-edited" else "sub/s1"
    if damage == "copy-edited":
        (w.backups()[-1] / "tree" / "sub" / "s1").write_text("not what was saved\n")
    elif damage == "dir-entry-edited":
        (w.backups()[-1] / "tree" / "P" / "mine.txt").write_text("not the notes\n")
    elif damage == "record-removed":
        _saved_json(w, lambda d: (d["files"].pop("tree/sub/s1"), d.update(complete=False)))
    else:
        _saved_json(w, lambda d: d["files"]["tree/sub/s1"].update(sha256=None))
    c = _restore(w, ts, "--check")
    r = _restore(w, ts)
    assert c.returncode == 1 and r.returncode == 1, c.stdout + r.stdout
    said, did = _parts(c)[(path, "node")], _parts(r)[(path, "node")]
    assert said.startswith("other:") and did == f"refused:{said}", (c.stdout, r.stdout)


def test_a_damaged_copy_behind_a_changed_checkout_reads_changed_in_both(w):
    """#4025 review S2: the writer never stages a part that is not as-left, so
    a damaged copy under a path edited since the repair is refused `changed`
    by --restore, and --check says the same, not the damage."""
    ts = _repaired(w, _foreign_edit)
    (w.backups()[-1] / "tree" / "sub" / "s1").write_text("not what was saved\n")
    (w.root / "sub" / "s1").write_text("edited since the repair\n")
    c, r = _restore(w, ts, "--check"), _restore(w, ts)
    assert _parts(c)[("sub/s1", "node")] == "other:changed", c.stdout
    assert _parts(r)[("sub/s1", "node")] == "refused:other:changed", r.stdout


def test_a_report_without_the_integrity_record_is_refused_whole(w):
    """Review N-3: a report from before SAVED.json is refused whole
    (`other:no-integrity`); nothing is written, REPAIR.json included."""
    ts = _repaired(w, _foreign_staged)
    _edit_report(w, lambda m: m.pop("integrity"))
    (w.backups()[-1] / "SAVED.json").unlink()
    report, snap = (w.backups()[-1] / "REPAIR.json").read_bytes(), w.snapshot()
    r = _restore(w, ts)
    assert r.returncode == 1 and "other:no-integrity" in r.stdout and _parts(r) == {}, r.stdout
    assert w.snapshot() == snap and (w.backups()[-1] / "REPAIR.json").read_bytes() == report


def _fail_rename(err: int, on: str):
    """The kit's renameat2 failing with ``err`` at its ``on`` call (aside or put-in)."""

    def patch(kit, mp):
        real = kit._renameat2

        def rename(sfd, sname, dfd, dname, where):
            if where == on:
                raise OSError(err, os.strerror(err))
            return real(sfd, sname, dfd, dname, where)

        mp.setattr(kit, "_renameat2", rename)

    return patch


@pytest.mark.parametrize(
    ("make", "on", "err", "verdict"),
    [
        (_foreign_edit, "aside", errno.EXDEV, "refused:other:cross-device"),
        (_foreign_untracked, "put-in", errno.EXDEV, "refused:other:cross-device"),  # L-3
        (_foreign_edit, "aside", errno.EINVAL, "refused:other:no-noreplace"),
        (_foreign_untracked, "put-in", errno.ENOSYS, "refused:other:no-noreplace"),
    ],
)
def test_a_filesystem_that_cannot_move_or_cannot_noreplace_is_refused_by_name(
    w, mod, monkeypatch, capsys, make, on, err, verdict
):
    """EXDEV (the run folder on another filesystem) and EINVAL/ENOSYS (no
    RENAME_NOREPLACE) refuse the part by name, at the aside and (review L-3,
    an absent as-left) at the put-in; nothing moved, nothing left in flight."""
    ts = _repaired(w, make)
    path = "sub/s1" if make is _foreign_edit else "Z5"
    before = w.snapshot().get(path)
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, patch=_fail_rename(err, on))
    assert rc == 1 and _parts_of(out)[(path, "node")] == verdict, out
    assert w.snapshot().get(path) == before
    assert w.report()["restore"]["inflight"] is None


def _parts_of(out: str) -> dict:
    return _parts(subprocess.CompletedProcess([], 0, stdout=out, stderr=""))


# ── a concurrent writer (review SF-1): self-verifying writes ────────────────


def test_a_write_between_the_judge_and_the_aside_is_put_back(w, mod, monkeypatch, capsys):
    """A session writes the path after the judge: the aside re-judge sees it
    is not the as-left state, puts it back (no clobber) and refuses."""
    ts = _repaired(w, _foreign_edit)
    session = lambda: (w.root / "sub" / "s1").write_text("session\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"intent": session})
    assert rc == 1 and _parts_of(out)[("sub/s1", "node")] == "refused:changed-during-restore", out
    assert (w.root / "sub" / "s1").read_text() == "session\n"


def test_a_write_between_the_aside_and_the_put_in_is_kept(w, mod, monkeypatch, capsys):
    """A session writes the path after the aside: the no-clobber put-in fails
    (EEXIST), the session's file stays, the as-left copy stays aside, named."""
    ts = _repaired(w, _foreign_edit)
    session = lambda: (w.root / "sub" / "s1").write_text("session\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"aside": session})
    assert rc == 1 and _parts_of(out)[("sub/s1", "node")] == "refused:changed-during-restore", out
    assert (w.root / "sub" / "s1").read_text() == "session\n"
    [run] = _runs(w)
    assert (run / "aside" / "sub" / "s1").read_text() == "s1\n"
    assert str(run / "aside" / "sub" / "s1") in out


def test_a_write_into_the_aside_after_the_put_in_is_named(w, mod, monkeypatch, capsys):
    """Review L-2: a process holding the as-left file open writes into it
    after the aside: before the part is recorded the aside is judged again,
    and the part is refused naming where that write went."""
    ts = _repaired(w, _foreign_edit)

    def write_through_fd():
        [run] = _runs(w)
        (run / "aside" / "sub" / "s1").write_text("written through an open fd\n")

    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"put-in": write_through_fd})
    assert rc == 1 and _parts_of(out)[("sub/s1", "node")] == "refused:changed-during-restore", out
    assert "aside" in out
    # #3986 review: the in-flight record names that aside for every later run.
    assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]


# ── SIGKILL at each step: a re-run converges ────────────────────────────────


@pytest.mark.parametrize("make", [_foreign_staged, _foreign_inside_dir, _foreign_untracked])
@pytest.mark.parametrize("step", ["stage", "intent", "aside", "put-in", "index"])
def test_a_restore_killed_at_any_step_converges_on_a_re_run(w, make, step):
    """A real SIGKILL at each step of the write path; the next run finishes it
    (the resume rule covers the gap after the aside: the path is empty, the
    in-flight record and the aside say what was there)."""
    ts, path, before = _fresh(w, make)
    r = _restore(w, ts, **{KILL_ENV: step})
    reached = r.returncode == -9
    if not reached:  # an absent as-left has no aside step
        assert step == "aside" and make is _foreign_untracked, r.stdout + r.stderr
    if step == "aside" and reached:
        c = _check(w, ts)
        assert _parts(c)[(path, "node")] == "as-left" and "interrupted restore" in c.stdout, (
            c.stdout
        )
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before
    _converged(w, ts)


def test_a_killed_restore_converges_on_a_group_writable_checkout(tmp_path):
    """Review B-1: under umask 0002 (the server's) the aside is a 0664 file;
    the resume rule checks the run folder and the folders it made (0700),
    and only the owner of the aside itself."""
    old = os.umask(0o002)
    try:
        w = World(tmp_path)
        ts, path, before = _fresh(w, _foreign_edit)
        assert stat.S_IMODE(os.lstat(w.root / path).st_mode) == 0o664, "control: umask 0002"
        assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
        r = _restore(w, ts)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _state_of(w, path) == before
        _converged(w, ts)
    finally:
        os.umask(old)


def test_a_second_kill_never_loses_the_first_in_flight_part(w):
    """Review SF-C: run 1 is killed after sub/t's aside; run 2 finishes sub/t
    FIRST (from run 1's aside), then is killed at sub/s1's intent; run 3
    converges. Had run 2 started with sub/s1, its intent would have erased
    the only record of where sub/t's as-left copy is."""
    killed_tag_side(w, "Z3")

    def both(w):
        (w.root / "sub" / "t").write_text("my t\n")
        return _foreign_edit(w)

    _lock_aside(w, lambda: both(w))
    before = {p: _state_of(w, p) for p in ("sub/s1", "sub/t")}
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    (w.root / "sub" / "s1").write_text("edited since\n")  # refused in run 1
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    assert w.report()["restore"]["inflight"][1:] == ["sub/t", "node"]
    (w.root / "sub" / "s1").write_text("s1\n")  # as the repair left it again
    assert _restore(w, ts, **{KILL_ENV: "intent"}).returncode == -9
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert {p: _state_of(w, p) for p in before} == before
    _converged(w, ts)


def test_a_killed_restore_whose_aside_changed_names_where_it_is(w):
    """Review SF-E: killed after the aside, and the aside then written: the
    re-run and --check refuse it and name the aside path, where that newer
    content is."""
    ts = _repaired(w, _foreign_edit)
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    [run] = _runs(w)
    aside = run / "aside" / "sub" / "s1"
    aside.write_text("written after the aside\n")
    # #3986 review: the in-flight record is kept, so a second run names it too.
    for r in (_check(w, ts), _restore(w, ts), _restore(w, ts), _check(w, ts)):
        assert r.returncode == 1 and str(aside) in r.stdout, r.stdout
        assert _parts(r)[("sub/s1", "node")] in ("other:changed", "refused:other:changed")
        assert w.report()["restore"]["restored_at"] is None
    assert aside.read_text() == "written after the aside\n"


def _dir_and_later_edit(w):
    """A directory row P (sorts first) and a later foreign edit sub/t."""
    (w.root / "sub" / "t").write_text("my t\n")
    return _foreign_inside_dir(w)


def test_an_aside_holding_only_the_as_left_copy_never_holds_the_rest(w):
    """#3986 review: killed after P's aside (it holds the tag's file P, the
    repair's own copy), then a session stages at P before the re-run (git
    refuses an entry below P while the index holds the file P): P is refused
    `index-changed` by its gate, but an aside holding only what the restore
    moved there is no unresolved content, so the other parts still restore
    rather than every run refusing them `inflight-unresolved`."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _dir_and_later_edit(w))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    assert w.report()["restore"]["inflight"][1:] == ["P", "node"], "control: P first"
    oid = _hash(w, "staged at P since\n")
    _git(w.root, "update-index", "--cacheinfo", f"100644,{oid},P")
    c = _check(w, ts)
    assert _parts(c)[("P", "node")] == "other:index-changed", c.stdout
    assert _parts(c)[("sub/t", "node")] == "as-left", c.stdout
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("P", "node")] == "refused:other:index-changed", r.stdout
    assert _parts(r)[("sub/t", "node")] == "restored", r.stdout
    assert (w.root / "sub" / "t").read_text() == "my t\n"
    assert "inflight-unresolved" not in r.stdout, r.stdout


def test_a_failed_put_back_of_the_as_left_copy_never_holds_the_rest(w, mod, monkeypatch, capsys):
    """#3986 review: P's put-in fails (EEXIST) and so does the put-back, so the
    aside keeps what was at P: the repair's own as-left copy, named, which is
    no unresolved content. The record is cleared and sub/t, after it, restores."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _dir_and_later_edit(w))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]

    def fail_p(kit, mp):
        real = kit._renameat2

        def rename(sfd, sname, dfd, dname, where):
            if sname == "P" and where in ("put-in", "put-back"):
                raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
            return real(sfd, sname, dfd, dname, where)

        mp.setattr(kit, "_renameat2", rename)

    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, patch=fail_p)
    parts = _parts_of(out)
    assert rc == 1 and parts[("P", "node")] == "refused:changed-during-restore", out
    assert "what was at the path is kept at" in out, out
    assert parts[("sub/t", "node")] == "restored", out
    assert w.report()["restore"]["inflight"] is None


def test_a_hold_set_mid_run_keeps_what_that_run_already_restored(w, mod, monkeypatch, capsys):
    """#3986 review: the run restores the directory P (its entry P/mine.txt
    with it), then sub/s1's aside is written through an open fd after its
    put-in. The hold refuses only what is still to write (sub/t): P and its
    entry stay restored, with no hold note on them."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: (_dir_and_later_edit(w), _foreign_edit(w)))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    put_ins = []

    def write_into_s1_aside(kit, mp):
        real = kit._step

        def step(name):
            if name == "put-in":
                put_ins.append(name)
                aside = w.gen / "deploy-backups"
                found = list(aside.glob("*-live-move-restore/aside/sub/s1"))
                if found:
                    found[0].write_text("written through an open fd\n")
            real(name)

        mp.setattr(kit, "_step", step)

    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, patch=write_into_s1_aside)
    parts = _parts_of(out)
    assert rc == 1 and len(put_ins) == 2, out  # P, then sub/s1; never sub/t
    assert parts[("P", "node")] == "restored" and parts[("P/mine.txt", "entry")] == "restored", out
    assert parts[("sub/s1", "node")] == "refused:changed-during-restore", out
    assert parts[("sub/t", "node")] == "refused:other:inflight-unresolved", out
    for line in out.splitlines():
        if line.startswith(('"P"', '"P/')):
            assert "inflight" not in line and "newer content" not in line, line
    assert (w.root / "sub" / "t").read_text() == "t\n"
    assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]


def _newer_in_s1_aside(w, put_ins: list):
    """The write kit's step hook: at each put-in, once sub/s1's aside exists,
    a process holding the replaced file open writes newer content into it."""

    def patch(kit, mp):
        real = kit._step

        def step(name):
            if name == "put-in":
                put_ins.append(name)
                for aside in (w.gen / "deploy-backups").glob("*-live-move-restore/aside/sub/s1"):
                    aside.write_text("written through an open fd\n")
            real(name)

        mp.setattr(kit, "_step", step)

    return patch


def test_a_hold_mid_run_beside_a_cannot_make_entry_still_reports(w, mod, monkeypatch, capsys):
    """#3986 review: a directory row also lists entries it cannot restore (a
    socket: `other:cannot-make`), and those are no restorable entry. A hold
    set mid-run passes over them, and the run still prints its report with
    the hold applied, rather than dying on the lookup of their directory."""
    import socket as socketlib

    killed_tag_side(w, "Z3")
    monkeypatch.chdir(w.root)  # a unix socket path is capped near 108 bytes
    keep = socketlib.socket(socketlib.AF_UNIX)
    _lock_aside(w, lambda: (_dir_and_later_edit(w), _foreign_edit(w), keep.bind("P/sock")))
    try:
        age_locks(w)
        assert w.repair().returncode == 0
    finally:
        keep.close()
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    assert _parts(_check(w, ts))[("P/sock", "entry")] == "other:cannot-make", "control"
    put_ins: list = []
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, patch=_newer_in_s1_aside(w, put_ins))
    parts = _parts_of(out)
    assert rc == 1 and "part(s):" in out, out
    assert parts[("P/sock", "entry")] == "refused:other:cannot-make", out
    assert parts[("sub/s1", "node")] == "refused:changed-during-restore", out
    assert parts[("sub/t", "node")] == "refused:other:inflight-unresolved", out
    assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]


@pytest.mark.parametrize("step", ["aside", "put-in"])
def test_an_in_flight_path_put_back_as_left_still_names_its_aside(w, step):
    """#3986 review: killed after the aside (or the put-in), a process holding
    the file open writes into the aside, and then the tag's file is put back at
    the path (`git checkout --`). The path reads as-left on its own, but only
    the resume rule's as-left looked at the aside: this one did not, so the
    aside is judged, still holds, and the rerun neither writes the path again
    (which would overwrite the record naming the aside) nor clears the report."""
    killed_tag_side(w, "Z3")
    # sub/s1 sorts first, so the kill lands on it; sub/t is the part after.
    _lock_aside(w, lambda: ((w.root / "sub" / "t").write_text("my t\n"), _foreign_edit(w)))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)  # opened before the restore
    try:
        r = _restore(w, ts, **{KILL_ENV: step})
        assert r.returncode == -9, r.stdout + r.stderr
        os.ftruncate(fd, 0)
        os.pwrite(fd, b"newer, through an open fd\n", 0)
    finally:
        os.close(fd)
    assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"], "control: sub/s1"
    [run] = _runs(w)
    aside = run / "aside" / "sub" / "s1"
    assert aside.read_text() == "newer, through an open fd\n", "control: the fd is the aside's"
    _git(w.root, "checkout", "--", "sub/s1")  # the tag's file back at the path
    assert (w.root / "sub" / "s1").read_text() == "s1\n"
    for r in (_check(w, ts), _restore(w, ts), _restore(w, ts)):
        parts = _parts(r)
        assert r.returncode == 1 and str(aside) in r.stdout, r.stdout
        assert parts[("sub/s1", "node")].endswith("other:inflight-unresolved"), r.stdout
        assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]
        assert w.report()["restore"]["restored_at"] is None
        assert parts[("sub/t", "node")].endswith("other:inflight-unresolved"), r.stdout
    assert (w.root / "sub" / "s1").read_text() == "s1\n"
    assert (w.root / "sub" / "t").read_text() == "t\n"
    assert aside.read_text() == "newer, through an open fd\n"


def test_the_aside_read_never_spends_the_walks_budget(w, mod, monkeypatch, capsys):
    """#3986 review: rows judged before the in-flight aside is read may spend
    the check's read budget; that read has its own, so an aside holding only
    the as-left copy is not taken for newer content (every part held) when
    the shared budget runs out."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _dir_and_later_edit(w))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    oid = _hash(w, "staged at P since\n")
    _git(w.root, "update-index", "--cacheinfo", f"100644,{oid},P")  # P: index-changed
    kit = _kit("live_move_restore.py")
    real = kit.Judge.unresolved

    def spent(self, parts):
        self.budget.entries = self.budget.bytes = 0  # the rows before used it all
        return real(self, parts)

    monkeypatch.setattr(kit.Judge, "unresolved", spent)
    load = mod._load_kit
    monkeypatch.setattr(mod, "_load_kit", lambda n: kit if n == "live_move_restore.py" else load(n))
    capsys.readouterr()
    mod.main(["--restore", ts, "--check", str(w.state), str(w.root)])
    out = capsys.readouterr().out
    assert '"P" node other:index-changed' in out, out
    assert '"sub/t" node as-left' in out and "inflight-unresolved" not in out, out


def test_nothing_else_is_written_while_an_in_flight_aside_is_unresolved(w, tmp_path):
    """#3986 review: REPAIR.json has one in-flight slot. While the in-flight
    part's aside holds newer content, writing another part would overwrite
    the record naming it, so no other part is written (`--check` says so too);
    once that content is moved out, the rest is restored."""
    killed_tag_side(w, "Z3")

    def both(w):
        (w.root / "sub" / "t").write_text("my t\n")  # sorts after sub/s1
        return _foreign_edit(w)

    _lock_aside(w, lambda: both(w))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"], "control: sub/s1 first"
    [run] = _runs(w)
    aside = run / "aside" / "sub" / "s1"
    aside.write_text("written after the aside\n")
    other = w.root / "sub" / "t"
    os.utime(other, (1_000_000_000, 1_000_000_000))  # a marker a rewrite would lose
    for r in (_check(w, ts), _restore(w, ts), _restore(w, ts)):
        parts = _parts(r)
        assert r.returncode == 1 and str(aside) in r.stdout, r.stdout
        assert parts[("sub/t", "node")].endswith("other:inflight-unresolved"), r.stdout
        assert other.read_text() == "t\n" and os.lstat(other).st_mtime == 1_000_000_000
        assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]
    assert aside.read_text() == "written after the aside\n"
    shutil.move(aside, tmp_path / "dealt-with")  # what was there, moved out
    r = _restore(w, ts)
    assert _parts(r)[("sub/t", "node")] == "restored", r.stdout
    assert other.read_text() == "my t\n" and w.report()["restore"]["inflight"] is None


def test_a_resume_from_a_folder_others_can_write_is_refused(w):
    """B-1: the resume rule trusts a run folder only when it is this user's
    alone; a group-writable one is never resumed from."""
    ts = _repaired(w, _foreign_edit)
    assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    [run] = _runs(w)
    run.chmod(0o770)
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "refused:other:changed", r.stdout
    assert not (w.root / "sub" / "s1").exists()


# ── a tag directory moved aside (review SF-D) ───────────────────────────────


def test_a_tag_directory_moves_aside_only_with_ignored_extras(w):
    """`sub` was a foreign file where the tag has sub/: the restore moves
    sub/ aside. A git-ignored extra there (a cache) moves with it and is
    named; a file that is not ignored refuses the part (--check stays
    lenient: it says as-left)."""
    ts, path, before = _fresh(w, _dir_file)
    (w.root / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    (w.root / "sub" / "notes.txt").write_text("a session's\n")
    assert _parts(_check(w, ts))[("sub", "node")] == "as-left"
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub", "node")] == "refused:other:changed", r.stdout
    assert (w.root / "sub" / "notes.txt").read_text() == "a session's\n"
    (w.root / "sub" / "notes.txt").unlink()
    (w.root / "sub" / "__pycache__").mkdir()
    (w.root / "sub" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before and "sub/__pycache__/x.pyc" in r.stdout, r.stdout


def test_a_cache_written_into_a_restored_directory_keeps_it_restored(w):
    """Review SF-6: once the server imports from a restored package dir it
    writes __pycache__ there; git ignores it, so the dir still reads
    already-restored."""
    ts, _, _ = _fresh(w, _foreign_inside_dir)
    assert _restore(w, ts).returncode == 0
    (w.root / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    (w.root / "P" / "__pycache__").mkdir()
    (w.root / "P" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    c = _check(w, ts)
    assert c.returncode == 0 and set(_parts(c).values()) == {"already-restored"}, c.stdout


# ── the index ───────────────────────────────────────────────────────────────


def test_a_parent_index_part_is_refused_when_a_child_is(w):
    """Review SF-5: the stage-0 file `sub` would drop every entry below it; a
    child row refused (sub/s1 edited since) refuses the parent `child-refused`,
    and the index keeps the child's entries."""
    ts = _repaired(w, _ancestor_file_child_edit)
    (w.root / "sub" / "s1").write_text("edited since\n")
    index = _git(w.root, "ls-files", "-s")
    r = _restore(w, ts)
    assert _parts(r)[("sub", "index")] == "refused:child-refused", r.stdout
    assert r.returncode == 1 and _git(w.root, "ls-files", "-s") == index


def test_an_entry_staged_under_a_restored_path_mid_restore_is_kept(w, mod, monkeypatch, capsys):
    """Review SF-A: git drops every entry under a stage-0 record. A session
    stages sub/new after the judge: the part whose footprint covers it is
    refused before the call, and sub/new stays staged."""
    ts = _repaired(w, _ancestor_file)
    oid = _hash(w, "NEW\n")
    stage = lambda: _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},sub/new")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"index": stage})
    assert rc == 1 and _parts_of(out)[("sub", "index")] == "refused:changed-during-restore", out
    assert "sub/new" in _git(w.root, "ls-files", "-s", "--", "sub/new")


def test_a_held_index_lock_refuses_the_index_and_flag_parts(w):
    """Review NOTE-7 and N-1: an index.lock a session's git holds (here a
    stale one, so the restore's own wait lets it run) fails the one
    update-index call: the index parts are `index-locked`, a flag part
    `flag-failed`; once it is gone, a re-run finishes."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: (_foreign_staged(w), _foreign_skip_worktree(w)))
    age_locks(w)
    assert w.repair().returncode == 0
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    lock = w.root / ".git" / "index.lock"
    lock.write_text("")
    age_locks(w)
    r = _restore(w, ts)
    parts = _parts(r)
    assert r.returncode == 1 and parts[("A", "index")] == "refused:index-locked", r.stdout
    assert parts[("sub/s1", "flag")] == "refused:flag-failed", r.stdout
    assert w.report()["restore"]["restored_at"] is None
    lock.unlink()
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    _converged(w, ts)


def test_an_index_that_is_not_what_was_written_is_index_skipped(w, mod, monkeypatch, capsys):
    """The whole index is verified after the one call: a session's entry
    staged right after it makes every index part `index-skipped`."""
    ts = _repaired(w, _foreign_staged)
    oid = _hash(w, "LATE\n")
    late = lambda: _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},late")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"index-call": late})
    assert rc == 1 and _parts_of(out)[("A", "index")] == "refused:index-skipped", out


def test_an_index_skipped_run_names_where_the_index_before_the_call_is(w, mod, monkeypatch, capsys):
    """Security W2: git applies the one call whole, so once the check after it
    fails nothing in REPAIR.json holds what the index was. The footprint's
    entries before the call are written first, to <run>/index-before.txt
    (`ls-files -s -z` records, 0600), and the index-skipped note names it."""
    ts = _repaired(w, _foreign_staged)
    before = _git(w.root, "ls-files", "-s", "-z", "--", "A")
    oid = _hash(w, "LATE\n")
    late = lambda: _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},late")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"index-call": late})
    assert rc == 1 and _parts_of(out)[("A", "index")] == "refused:index-skipped", out
    [run] = _runs(w)
    kept = run / "index-before.txt"
    assert str(kept) in out, out
    assert kept.read_bytes() == before.encode() and before.endswith("\tA\0"), before
    assert stat.S_IMODE(os.lstat(kept).st_mode) == 0o600


def _staged_in_untracked_dir(w):
    """An untracked directory Z5/ (the tag has nothing there) holding a staged
    Z5/new: the row Z5 is a directory row, Z5/new a row it covers, whose
    index part has no entry at or above it after the repair."""
    (w.root / "Z5").mkdir()
    (w.root / "Z5" / "mine.txt").write_text("notes\n")
    (w.root / "Z5" / "new").write_text("STAGED\n")
    _git(w.root, "add", "--", "Z5/new")
    (w.root / "Z5" / "new").write_text("WORKTREE\n")
    return "Z5"


def test_a_covered_child_index_part_waits_for_its_directory_row(w, mod, monkeypatch, capsys):
    """Security N4: a row under a directory row has no node part of its own;
    its disk is the directory's. When that directory's node part is refused
    at write time, the child's index part is refused `row-refused` too, and
    the index keeps nothing at the child."""
    ts = _repaired(w, _staged_in_untracked_dir)
    assert _parts(_check(w, ts))[("Z5/new", "index")] == "as-left", "control"
    session = lambda: (w.root / "Z5").write_text("session\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"intent": session})
    parts = _parts_of(out)
    assert rc == 1 and parts[("Z5", "node")].startswith("refused:"), out
    assert parts[("Z5/new", "index")] == "refused:row-refused", out
    assert _git(w.root, "ls-files", "-s", "--", "Z5") == ""
    assert (w.root / "Z5").read_text() == "session\n"


def _many_staged(w):
    """30 staged files in an untracked Z5/ (a target path): 30 index parts."""
    (w.root / "Z5").mkdir()
    for i in range(30):
        (w.root / "Z5" / f"n{i:02}").write_text(f"staged {i}\n")
    _git(w.root, "add", "--", "Z5")
    return "Z5"


def test_the_index_phase_never_rescans_every_outcome(w, mod, monkeypatch, capsys):
    """Security W1: the rows with a refused part are a set kept as each part
    is refused. Rebuilt from every outcome per index part (and per pass of
    the child-refused fixpoint) it was quadratic in the report, with the
    update and checkout locks held. Pinned: between the `index` and the
    `post-judge` steps the outcomes are never iterated."""
    ts = _repaired(w, _many_staged)
    reads, phase, made = [0], [False], []

    class Counted(dict):
        def items(self):
            reads[0] += phase[0]
            return super().items()

    def patch(kit, mp):
        init = kit._Writer.__init__

        def counted(self, *a):
            init(self, *a)
            self.out = Counted(self.out)
            made.append(self)

        mp.setattr(kit._Writer, "__init__", counted)

    at = {"index": lambda: phase.__setitem__(0, True),
          "post-judge": lambda: phase.__setitem__(0, False)}  # fmt: skip
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at=at, patch=patch)
    assert rc == 0 and made, out
    written = [k for k, v in _parts_of(out).items() if k[1] == "index" and v == "restored"]
    assert len(written) >= 30, out
    assert reads[0] == 0, f"the outcomes were iterated {reads[0]} times in the index phase"


def test_the_restore_never_runs_the_repositorys_fsmonitor(w, tmp_path):
    """Security W3: every git call on the restore path runs with
    core.fsmonitor=false and core.hooksPath=/dev/null, so a repository config
    naming an fsmonitor program (here, one that leaves a marker) never runs
    it. Control: plain git in the same checkout does run it."""
    ts = _repaired(w, _foreign_edit)
    marker, hook = tmp_path / "fsmonitor-ran", tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    _git(w.root, "config", "core.fsmonitor", str(hook))
    _git(w.root, "update-index", "-q", "--refresh", check=False)
    assert marker.exists(), "control: git runs the configured fsmonitor"
    marker.unlink()
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists(), "the restore ran the repository's fsmonitor"


def test_flags_survive_the_index_call(w):
    """Flags are set after the one --index-info call (it re-creates entries):
    a staged and skip-worktree row comes back with both."""
    ts, path, before = _fresh(
        w, lambda w: (_foreign_staged(w), _git(w.root, "update-index", "--skip-worktree", "A"))[0]
    )
    assert before["flags"].startswith("S"), "control: flagged"
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before


def test_a_flag_already_back_is_set_again_on_a_re_created_entry(w):
    """#3986 review (Codex 4492621582): the flag part already reads restored
    (someone set skip-worktree again by hand) while the index part still
    needs writing; the index call re-creates the entry without the flag, so
    the restore sets it again rather than counting it done."""
    ts, path, before = _fresh(
        w, lambda w: (_foreign_staged(w), _git(w.root, "update-index", "--skip-worktree", "A"))[0]
    )
    _git(w.root, "update-index", "--skip-worktree", "A")  # the flag back by hand, the index not
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(w.root, "ls-files", "-v", "--", "A").startswith("S "), "the flag was lost"
    assert _state_of(w, path) == before


def test_a_saved_directory_missing_a_recorded_entry_is_refused(w):
    """#3986 review (Codex 4492621571): a file SAVED.json records that is gone
    from the saved directory refuses the directory's node, never a restore
    that writes the rest and clears the report."""
    ts = _repaired(w, _foreign_inside_dir)
    saved = w.backups()[-1] / "tree" / "P" / "mine.txt"
    assert saved.is_file(), "control: the file was saved"
    saved.unlink()
    r = _restore(w, ts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _parts_of(r.stdout)[("P", "node")].startswith("refused:"), r.stdout
    assert w.report()["restore"]["restored_at"] is None


def test_a_saved_copy_changed_to_match_the_checkout_is_refused(w):
    """#3986 review (Codex, finding 1): a saved copy changed to hold what the
    checkout holds is not the saved state SAVED.json records, so it never
    reads already-restored and clears the report; the restore refuses it."""
    ts = _repaired(w, _foreign_edit)
    saved = w.backups()[-1] / "tree" / "sub" / "s1"
    assert saved.read_text() == "mine\n", "control: the edit was saved"
    saved.write_bytes((w.root / "sub" / "s1").read_bytes())
    r = _restore(w, ts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _parts_of(r.stdout)[("sub/s1", "node")] == "refused:other:hash-mismatch", r.stdout
    assert w.report()["restore"]["restored_at"] is None


def test_a_saved_directory_entry_changed_to_match_the_checkout_is_refused(w):
    """The same for an entry of a saved directory: the checkout and the saved
    copy agree, SAVED.json does not, so the directory never reads
    already-restored."""
    ts = _repaired(w, _foreign_inside_dir)
    saved = w.backups()[-1] / "tree" / "P" / "mine.txt"
    assert saved.read_text() == "notes\n", "control: the file was saved"
    saved.write_text("not the notes\n")
    (w.root / "P").unlink()  # the repair left the tag's file P
    shutil.copytree(saved.parent, w.root / "P", symlinks=True)
    r = _restore(w, ts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _parts_of(r.stdout)[("P", "node")].startswith("refused:"), r.stdout
    assert w.report()["restore"]["restored_at"] is None


def _foreign_edit_of_an_update_file(w):
    """P/inner, a file the update wrote into its new directory P/, edited."""
    (w.root / "P" / "inner").write_text("my inner\n")
    return "P"


def test_a_saved_directory_entry_changed_to_the_update_bytes_is_refused(w):
    """#3986 review (Codex, round 3, finding 1): a saved entry whose bytes are
    the move target's is the update's own and is left out of the directory.
    One changed back to the target's bytes is not what SAVED.json records, so
    it is never taken for the update's: the restore refuses the directory
    rather than putting back an empty one and clearing the report."""
    ts = _repaired(w, _foreign_edit_of_an_update_file)
    saved = w.backups()[-1] / "tree" / "P" / "inner"
    assert saved.read_text() == "my inner\n", "control: the edit was saved"
    saved.write_text("inner\n")  # the move target's P/inner
    assert update_bytes(w, "P/inner", saved.read_bytes()), "control: the update's bytes"
    r = _restore(w, ts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _parts_of(r.stdout)[("P", "node")] == "refused:other:hash-mismatch", r.stdout
    assert (w.root / "P").read_text() == "p\n", "the tag's P was replaced"
    assert w.report()["restore"]["restored_at"] is None


def test_a_write_into_the_aside_after_a_kill_at_the_put_in_is_named(w):
    """#3986 review (Codex, round 3, finding 2): killed after the put-in and
    before the aside is judged again, a process holding the replaced file open
    then writes into the aside. The checkout holds the saved state, but the
    in-flight part is never read already-restored until its aside is still
    what was moved there: the re-run and --check refuse it, naming the aside."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)  # opened before the restore
    try:
        assert _restore(w, ts, **{KILL_ENV: "put-in"}).returncode == -9
        os.ftruncate(fd, 0)
        os.pwrite(fd, b"newer, through an open fd\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    aside = run / "aside" / "sub" / "s1"
    assert aside.read_text() == "newer, through an open fd\n", "control: the fd is the aside's"
    assert (w.root / "sub" / "s1").read_text() == "mine\n", "control: put in"
    # The in-flight record is kept while its aside is unresolved, so every
    # rerun refuses and names it, never one that reads it back and clears.
    for r in (_check(w, ts), _restore(w, ts), _restore(w, ts), _check(w, ts)):
        assert r.returncode == 1 and str(aside) in r.stdout, r.stdout
        assert _parts(r)[("sub/s1", "node")] in ("other:changed", "refused:other:changed")
        assert w.report()["restore"]["restored_at"] is None
        assert w.report()["restore"]["inflight"][1:] == ["sub/s1", "node"]
    assert aside.read_text() == "newer, through an open fd\n"


def test_a_flag_set_after_the_repair_is_never_dropped_by_the_index_call(w):
    """#3986 review (Codex, round 3, finding 3): the row saved no flag, so a
    skip-worktree set after the repair is no flag part; the one index call
    re-creates the entry unflagged. Its footprint's flags are read with it: a
    flag the restore does not set again refuses the index part, and the entry
    keeps its flag."""
    ts, path, _ = _fresh(w, _foreign_staged)
    _git(w.root, "update-index", "--skip-worktree", "A")
    index = _git(w.root, "ls-files", "-s", "--", "A")
    r = _restore(w, ts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _parts(r)[("A", "index")] == "refused:other:flag-changed", r.stdout
    assert '"A" holds a flag (S) the index call would drop' in r.stdout, r.stdout
    assert _git(w.root, "ls-files", "-v", "--", "A").startswith("S "), "the newer flag was lost"
    assert _git(w.root, "ls-files", "-s", "--", "A") == index
    assert w.report()["restore"]["restored_at"] is None


def _unmerged_below_a_file(w):
    """The index holds a file `sub` and an unmerged `sub/s1` (stages 1-3),
    which git keeps side by side (MEASURED, git 2.43)."""
    sides = [_hash(w, f"side {n}\n") for n in (1, 2, 3)]
    _git(w.root, "update-index", "--force-remove", "sub/s1", "sub/t")
    info = "".join(f"100644 {o} {n}\tsub/s1\n" for n, o in zip((1, 2, 3), sides, strict=True))
    subprocess.run(
        ["git", "-C", str(w.root), "update-index", "--index-info"],
        input=info, text=True, check=True, env=_git_env(),
    )  # fmt: skip
    staged = _hash(w, "STAGED SUB\n")
    _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{staged},sub")
    return "sub"


def _back_below_a_refused_file(w) -> str:
    """Run 1 puts sub/s1's unmerged stages back and refuses the stage-0 file
    `sub` (a session staged sub/new below it); sub/new is then gone again, so
    run 2 writes `sub`, whose footprint holds sub/s1, already back."""
    ts = _repaired(w, _unmerged_below_a_file)
    new = _hash(w, "NEW\n")
    _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{new},sub/new")
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub", "index")].startswith("refused:"), r.stdout
    assert _parts(r)[("sub/s1", "index")] == "restored", "control: sub/s1 back in run 1"
    _git(w.root, "update-index", "--force-remove", "sub/new")
    return ts


def _stages(w, path: str) -> list[str]:
    return [ln.split()[2] for ln in _git(w.root, "ls-files", "-s", "--", path).splitlines()]


def test_an_index_part_already_back_keeps_its_entries_under_a_file_written_later(w):
    """#3986 review: a stage-0 record drops every entry below it, and an index
    part already back can sit in another row's footprint. Its saved entries
    are written again in the same update-index call (and the whole index then
    checked with them), so a run that clears the report never leaves them
    deleted; the post-check judges that part again too."""
    ts = _back_below_a_refused_file(w)
    r = _restore(w, ts)
    assert _stages(w, "sub/s1") == ["1", "2", "3"], r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert _parts(r)[("sub", "index")] == "restored", r.stdout
    _converged(w, ts)


def _check_said_what_restore_did(c, r) -> None:
    """Every part: as-left is written, already-restored stays, and any other
    verdict is the reason the restore prints after ``refused:`` (#4025 item 3)."""
    said, did = _parts(c), _parts(r)
    assert said.keys() == did.keys(), (c.stdout, r.stdout)
    for key, v in said.items():
        want = {"as-left": "restored", "already-restored": "already-restored"}.get(v, f"refused:{v}")
        assert did[key] == want, (key, c.stdout, r.stdout)


@pytest.mark.parametrize("case", ["flag-after-repair", "child-refused"])
def test_check_names_the_index_calls_refusals(w, case):
    """#4025 item 3: --check runs the index call's own rule (footprint) and the
    flag cascade on what it read, so it names every refusal the restore makes
    there: a flag the call would drop, a stage-0 parent above a refused child."""
    if case == "flag-after-repair":
        ts, _path, _ = _fresh(w, _foreign_staged)
        _git(w.root, "update-index", "--skip-worktree", "A")
    else:
        ts = _repaired(w, _ancestor_file_child_edit)
        (w.root / "sub" / "s1").write_text("edited since\n")
    c = _check(w, ts)
    r = _restore(w, ts)
    assert c.returncode == 1 and r.returncode == 1, c.stdout + r.stdout
    _check_said_what_restore_did(c, r)
    key = ("A", "index") if case == "flag-after-repair" else ("sub", "index")
    assert _parts(c)[key] == ("other:flag-changed" if case == "flag-after-repair" else "child-refused")


def test_a_flagged_entry_already_back_in_a_footprint_still_refuses(w, mod, monkeypatch, capsys):
    """The entries rewritten for an index part already back are still checked
    for flags first: a flag the restore does not set again refuses the call
    (`other:flag-changed`), and nothing at or below the path changes. Git
    refuses to flag an unmerged entry (`Unable to mark file`, MEASURED on git
    2.43), so the flag is faked in the index read the restore makes."""
    ts = _back_below_a_refused_file(w)
    index = _git(w.root, "ls-files", "-s")
    real = mod._index_state

    def flagged(repo):
        entries, flags = real(repo)
        return entries, {**flags, "sub/s1": "S"}

    monkeypatch.setattr(mod, "_index_state", flagged)
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts)
    assert rc == 1 and _parts_of(out)[("sub", "index")] == "refused:other:flag-changed", out
    assert '"sub/s1" holds a flag (S) the index call would drop' in out, out
    assert _git(w.root, "ls-files", "-s") == index and _stages(w, "sub/s1") == ["1", "2", "3"]


@pytest.mark.parametrize(
    ("make", "rel", "part"),
    [
        (_foreign_edit, "sub/s1", ("sub/s1", "node")),
        (_foreign_inside_dir, "P/mine.txt", ("P", "node")),  # an entry of a saved directory
    ],
)
def test_a_saved_copys_exec_bit_never_outranks_its_record(w, make, rel, part):
    """#3986 review: the node a saved copy stands for takes SAVED.json's mode,
    the one the restore writes (its record's & 0o777), never the copy's. A copy
    chmodded after the repair neither makes the post-check refuse the
    restore's own write on every rerun nor lets a checkout with that mode read
    already-restored."""
    ts = _repaired(w, make)
    copy = w.backups()[-1] / "tree" / rel
    assert not os.lstat(copy).st_mode & 0o100, "control: saved without the exec bit"
    os.chmod(copy, 0o700)
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.lstat(w.root / rel).st_mode & 0o100, "the record's mode"
    _converged(w, ts)
    os.chmod(w.root / rel, 0o755)
    assert _parts(_check(w, ts))[part] == "other:changed"


def test_a_saved_executable_that_lost_its_bit_never_reads_restored(w):
    """#3986 review: a saved executable whose backup copy and checkout copy
    both lose the exec bit is not the saved state: SAVED.json records it
    executable, and the copy stands for that record."""
    ts, path, _ = _fresh(w, lambda w: (_foreign_edit(w), (w.root / "sub/s1").chmod(0o755))[0])
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.lstat(w.root / path).st_mode & 0o100, "control: restored executable"
    os.chmod(w.backups()[-1] / "tree" / path, 0o644)
    os.chmod(w.root / path, 0o644)
    assert _parts(_check(w, ts))[(path, "node")] == "other:changed"


def test_a_saved_directory_with_an_incomplete_inventory_is_refused(w):
    """#3986 review: past its cap SAVED.json records nothing more and says
    `complete: false`. A saved file it never recorded, gone from the backup
    since, is invisible, so the directory would be built without it and the
    report cleared. Both judges refuse a saved directory then, as a part that
    goes back by hand."""
    ts = _repaired(w, _foreign_inside_dir)
    saved = w.backups()[-1] / "tree" / "P" / "mine.txt"
    saved.unlink()  # unrecorded, then lost from the backup
    _saved_json(w, lambda d: (d["files"].pop("tree/P/mine.txt"), d.update(complete=False)))
    c = _check(w, ts)
    assert c.returncode == 1 and _parts(c)[("P", "node")] == "other:no-hash", c.stdout
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("P", "node")] == "refused:other:no-hash", r.stdout
    assert "incomplete" in r.stdout and "by hand" in r.stdout, r.stdout
    assert (w.root / "P").read_text() == "p\n" and w.report()["restore"]["restored_at"] is None


def test_an_aside_check_never_spends_the_judges_budget(w, mod, monkeypatch, capsys):
    """#3986 review: the judge's read budget is spent judging every row; each
    check of an aside reads on its own, so a tag node checked again after the
    move is never read as changed because the shared budget ran out."""
    ts = _repaired(w, _foreign_edit)
    made = []

    def capture(kit, mp):
        init = kit._Writer.__init__

        def keep(self, *a):
            init(self, *a)
            made.append(self)

        mp.setattr(kit._Writer, "__init__", keep)

    def drain():
        made[0].j.budget.entries = made[0].j.budget.bytes = 0  # the rows judged first used it

    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"intent": drain}, patch=capture)
    assert rc == 0 and _parts_of(out)[("sub/s1", "node")] == "restored", out
    assert (w.root / "sub" / "s1").read_text() == "mine\n"


def test_a_flag_is_never_set_on_an_entry_staged_after_the_judge(w, mod, monkeypatch, capsys):
    """#3986 review: a flag-only part (the index as the tag's) is set on the
    entry the judge read. A session staging a newer blob there in between is
    refused `changed-during-restore`: its entry is never flagged."""
    ts = _repaired(w, _foreign_skip_worktree)
    oid = _hash(w, "staged by a session\n")
    stage = lambda: _git(w.root, "update-index", "--cacheinfo", f"100644,{oid},sub/s1")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"index": stage})
    assert rc == 1 and _parts_of(out)[("sub/s1", "flag")] == "refused:changed-during-restore", out
    assert _git(w.root, "ls-files", "-v", "--", "sub/s1").startswith("H "), "flagged anyway"
    assert oid in _git(w.root, "ls-files", "-s", "--", "sub/s1")


@pytest.mark.parametrize("killed", [False, True])
def test_a_directory_the_checkout_ignores_keeps_its_files_ignored_in_the_aside(
    tmp_path, monkeypatch, killed
):
    """#3986 review: the checkout ignores sub/ and the tag's sub/.gitignore
    re-includes `x.cache`. Git never re-includes below an excluded directory
    (MEASURED, git 2.43: `check-ignore -v` names the checkout's `sub/`), so
    the aside is judged the same way: the cache stays an ignored extra, the
    restore converges, and one killed after the aside resumes."""
    from tests.test_scripts import _live_repair_world as world

    monkeypatch.setitem(world.TAG_FILES, "sub/.gitignore", "!x.cache\n")
    w = World(tmp_path)
    ts, path, before = _fresh(w, _dir_file)
    # The checkout's own .gitignore (not .git/info/exclude, which the aside's
    # check reads too, sharing the checkout's git dir): only it stays behind.
    (w.root / ".gitignore").write_text("sub/\n")
    (w.root / "sub" / "x.cache").write_bytes(b"cache")
    assert _parts(_check(w, ts))[("sub", "node")] == "as-left", "control"
    if killed:
        assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before
    _converged(w, ts)


def test_a_restore_killed_between_its_two_flag_writes_converges(w):
    """#3986 review (Codex, finding 2): a row saved with both flags (`s`) is
    written skip-worktree first, then assume-unchanged. Killed between the two,
    the entry holds `S`, the restore's own half-written state: the re-run
    finishes it rather than refusing it `other:changed`."""
    ts, path, before = _fresh(w, _foreign_both_flags)
    assert before["flags"].startswith("s "), "control: both flags"
    assert _restore(w, ts, **{KILL_ENV: "flag"}).returncode == -9
    assert _git(w.root, "ls-files", "-v", "--", path).startswith("S "), "control: half written"
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before
    _converged(w, ts)


@pytest.mark.parametrize(
    ("make", "flag", "verdict"),
    [
        (_foreign_both_flags, "--skip-worktree", "as-left"),  # the restore's own half
        (_foreign_both_flags, "--assume-unchanged", "other:changed"),  # never its half
        (_foreign_skip_worktree, "--assume-unchanged", "other:changed"),
    ],
)
def test_only_the_restores_own_half_written_flag_reads_as_left(w, make, flag, verdict):
    """A flag state the restore cannot have left (assume-unchanged alone, or a
    flag a one-flag row never had) is still `other:changed`."""
    ts = _repaired(w, make)
    _git(w.root, "update-index", flag, "--", "sub/s1")
    assert _parts(_check(w, ts))[("sub/s1", "flag")] == verdict


def test_a_flag_lost_to_an_index_call_that_did_not_verify_is_refused(w, mod, monkeypatch, capsys):
    """#3986 review (CodeRabbit, finding 4): the flag part already reads restored
    and the index part is written; the call re-creates the entry unflagged, and
    then the whole index is not the one written (a session staged meanwhile).
    The flag is judged again and refused, never reported already-restored; a
    re-run puts it back."""
    ts, path, before = _fresh(
        w, lambda w: (_foreign_staged(w), _git(w.root, "update-index", "--skip-worktree", "A"))[0]
    )
    _git(w.root, "update-index", "--skip-worktree", "A")  # the flag back by hand, the index not
    oid = _hash(w, "LATE\n")
    late = lambda: _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},late")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"index-call": late})
    parts = _parts_of(out)
    assert rc == 1 and parts[("A", "index")] == "refused:index-skipped", out
    assert not _git(w.root, "ls-files", "-v", "--", "A").startswith("S "), "control: flag lost"
    assert parts[("A", "flag")].startswith("refused:"), out
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(w.root, "ls-files", "-v", "--", "A").startswith("S ")


@pytest.mark.parametrize("killed", [False, True])
def test_ignore_rules_inside_a_moved_directory_still_apply_to_its_aside(
    tmp_path, monkeypatch, killed
):
    """#3986 review (Codex, finding 3): the tag's sub/.gitignore ignores
    `*.cache`; a cache file in the tag directory sub/ moves aside with it. Its
    rule moved too, so the aside is judged by the rules it holds, not by a
    checkout that no longer has them: the restore converges (and a restore
    killed after the aside resumes)."""
    from tests.test_scripts import _live_repair_world as world

    monkeypatch.setitem(world.TAG_FILES, "sub/.gitignore", "*.cache\n")
    w = World(tmp_path)
    ts, path, before = _fresh(w, _dir_file)
    (w.root / "sub" / "x.cache").write_bytes(b"cache")
    assert _parts(_check(w, ts))[("sub", "node")] == "as-left", "control"
    if killed:
        assert _restore(w, ts, **{KILL_ENV: "aside"}).returncode == -9
        c = _check(w, ts)
        assert _parts(c)[("sub", "node")] == "as-left", c.stdout
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _state_of(w, path) == before
    if not killed:
        assert "sub/x.cache" in r.stdout, r.stdout
    _converged(w, ts)


def test_a_disagreeing_post_write_judge_counts_as_refused(w, mod, monkeypatch, capsys):
    """Review N-4: the judge run after the writes must read already-restored
    for every part written; one that does not is refused, and the report is
    not cleared."""
    ts = _repaired(w, _foreign_edit)
    edit = lambda: (w.root / "sub" / "s1").write_text("after the write\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"post-judge": edit})
    assert rc == 1 and _parts_of(out)[("sub/s1", "node")] == "refused:post-check", out
    block = w.report()["restore"]
    assert block["refused"] == 1 and block["restored_at"] is None, block


# ── output and the readers ──────────────────────────────────────────────────


def test_the_output_says_what_now_blocks_a_deploy(w):
    """NOTE-3: tracked edits block deploy_code_only.sh and update.sh; untracked
    restores only under src/, config/ or pyproject.toml; the run folder and
    its 90-day prune are named."""
    ts, _, _ = _fresh(w, _foreign_edit)
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout
    for text in ("deploy_code_only.sh", "update.sh", "src/", "pyproject.toml", "90 days"):
        assert text in r.stdout, (text, r.stdout)


def test_a_permanent_refusal_says_to_restore_by_hand_then_dismiss(w, monkeypatch):
    """Review L-4: a socket cannot be made again; the output says to put it
    back by hand from the folder, then --dismiss."""
    import socket as socketlib

    killed_tag_side(w, "Z3")
    monkeypatch.chdir(w.root)  # a unix socket path is capped near 108 bytes
    keep = socketlib.socket(socketlib.AF_UNIX)
    keep.bind("Z5")
    try:
        age_locks(w)
        assert w.repair().returncode == 0
    finally:
        keep.close()
    ts = w.backups()[-1].name[: -len(SUFFIX)]
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[("Z5", "node")] == "refused:other:cannot-make"
    assert "by hand" in r.stdout and f"--dismiss {ts}" in r.stdout, r.stdout


def test_a_report_with_a_restore_in_flight_is_never_superseded(w):
    """§7: the writer marks only older unfinished reports superseded; one a
    restore holds in flight is not (pinned)."""
    _repaired(w, _foreign_edit)
    run = "20260101T000000Z-1-live-move-restore"
    _edit_report(
        w,
        lambda m: m.update(
            result=None, restore={"inflight": [run, "sub/s1", "node"], "refused": 0,
                                  "restored_at": None}
        ),
    )  # fmt: skip
    first = w.backups()[-1]
    killed_tag_side(w, "Z3")
    age_locks(w)
    assert w.repair().returncode == 0 and len(w.backups()) == 2
    assert json.loads((first / "REPAIR.json").read_text())["result"] is None
