"""`live_move_repair.py --restore TS --check` (#3492 PR B1): the judge of what a
restore may write, read-only, and the report validators it applies first
(scripts/lib/live_move_restore.py).

Every case is a REAL repair in the scratch world (tests/test_scripts/
_live_repair_world.py), judged afterwards. A part is `as-left` (the path holds
what the repair left: the tag's), `already-restored` (it holds what the repair
saved) or `other:<reason>`; only a run where every part is one of the first two
exits 0. Restoring below runs the real restore (`--restore TS`, #3492 B2b;
its own suite is test_live_move_restore_write.py).
"""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    LIB,
    MODULE,
    _ancestor_file,
    _dir_becomes_file,
    _foreign_both_flags,
    _foreign_edit,
    _foreign_fifo_inside_dir,
    _foreign_gitlink,
    _foreign_inside_dir,
    _foreign_skip_worktree,
    _foreign_staged,
    _foreign_symlink,
    _foreign_unmerged,
    _foreign_untracked,
    _gc,
    _git,
    _hash,
    _lock_aside,
    age_locks,
    killed_tag_side,
    restore_all,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")

SUFFIX = "-live-move-repair"
KINDS = [
    _foreign_edit,
    _foreign_untracked,
    _foreign_inside_dir,
    _foreign_staged,
    _foreign_unmerged,
    _foreign_skip_worktree,
    _foreign_gitlink,
    _foreign_both_flags,
    _foreign_symlink,
    _foreign_fifo_inside_dir,
    _ancestor_file,  # a tag-directory row: an index file `sub` over the tag's sub/
    _dir_becomes_file,  # a node part over a tag directory: a foreign file `sub`
]


def _repaired(w, make) -> str:
    """A real repair of a killed switch with ``make``'s foreign path; its ts."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: make(w))
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(row["class"] == "foreign" for row in w.report()["paths"]), "control: none foreign"
    return w.backups()[-1].name[: -len(SUFFIX)]


def _check(w, ts: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "python3",
            "-I",
            "-S",
            str(MODULE),
            "--restore",
            ts,
            "--check",
            *extra,
            str(w.state),
            str(w.root),
        ],  # fmt: skip
        capture_output=True,
        text=True,
        env=w.env(),
        timeout=600,
    )


def _parts(r: subprocess.CompletedProcess) -> dict[tuple[str, str], str]:
    """(path, part) -> verdict, from every per-part line `<json path> <part> <verdict>`
    (a note in parentheses may follow the verdict)."""
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            path, end = json.JSONDecoder().raw_decode(line)
            part, verdict = line[end:].split()[:2]
            out[(path, part)] = verdict
    return out


def _edit_report(w, change) -> None:
    path = w.backups()[-1] / "REPAIR.json"
    report = json.loads(path.read_text())
    change(report)
    path.write_text(json.dumps(report, indent=2))


def _newer_dir(w, ts: str) -> Path:
    """An empty report dir named a minute after ``ts``."""
    stamp = calendar.timegm(time.strptime(ts.split("-", 1)[0], "%Y%m%dT%H%M%SZ")) + 60
    d = (
        w.gen
        / "deploy-backups"
        / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp))}-7{SUFFIX}"
    )
    d.mkdir()
    return d


# ── the judge on real repairs ───────────────────────────────────────────────


@pytest.mark.parametrize("make", KINDS)
def test_a_fresh_repair_is_as_left_then_already_restored(w, make):
    """Right after the repair every foreign part holds what the repair left;
    once the restore puts the saved state back, every part holds what was
    saved."""
    ts = _repaired(w, make)
    r = _check(w, ts)
    parts = _parts(r)
    assert r.returncode == 0, r.stdout + r.stderr
    assert parts and set(parts.values()) == {"as-left"}, parts
    restore_all(w)
    r = _check(w, ts)
    parts = _parts(r)
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(parts.values()) == {"already-restored"}, parts


def test_a_directory_row_lists_only_the_entries_the_update_did_not_write(w):
    """P was saved whole (P/inner the update's bytes, P/mine.txt not): only
    P/mine.txt is an entry to restore."""
    ts = _repaired(w, _foreign_inside_dir)
    parts = _parts(_check(w, ts))
    assert ("P/mine.txt", "entry") in parts and ("P/inner", "entry") not in parts, parts


def _foreign_dir_with_nested_repo(w):
    (w.root / "P" / "mine.txt").write_text("notes\n")
    nested = w.root / "P" / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True, env=w.env())
    return "P"


def test_a_saved_directory_holding_a_nested_repository_is_judged(w):
    """#3974 item 1: the writer saves a foreign directory whole, a nested
    checkout's .git files with it; their SAVED.json keys hold a `.git` part
    below the row's path, which the check accepts (only a key whose first part
    is `.git`, the checkout's own, is refused)."""
    ts = _repaired(w, _foreign_dir_with_nested_repo)
    saved = json.loads((w.backups()[-1] / "SAVED.json").read_text())["files"]
    assert any("/.git/" in k for k in saved), "control: no nested .git key was saved"
    r = _check(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bad-report" not in r.stdout and _parts(r).get(("P", "node")) == "as-left", r.stdout


def _foreign_dir_with_socket_in_nested_git(w):
    import socket

    _foreign_dir_with_nested_repo(w)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(w.root / "P" / "nested" / ".git" / "s"))
    sock.close()
    return "P"


def test_an_unsaved_node_inside_a_nested_repository_is_judged(w):
    """#3974 item 1, the same rule for the writer's `unsaved` entries: a socket
    inside a nested checkout's .git (it cannot be copied) is named on its row,
    and the report is still judged rather than refused whole."""
    ts = _repaired(w, _foreign_dir_with_socket_in_nested_git)
    rows = [r for r in w.report()["paths"] if r["path"] == "P"]
    assert rows and any(u["path"] == "P/nested/.git/s" for u in rows[0]["unsaved"]), rows
    r = _check(w, ts)
    assert "bad-report" not in r.stdout, r.stdout
    assert _parts(r).get(("P/nested/.git/s", "entry")) == "other:cannot-make", r.stdout


def test_a_saved_key_into_the_checkouts_own_git_dir_is_refused(w):
    """The other side of #3974 item 1: `tree/.git/...` would name the
    checkout's own .git, which the writer never saves."""
    ts = _repaired(w, _foreign_inside_dir)
    path = w.backups()[-1] / "SAVED.json"
    saved = json.loads(path.read_text())
    saved["files"]["tree/.git/config"] = {"type": "file", "mode": "0644", "sha256": "0" * 64}
    path.write_text(json.dumps(saved))
    r = _check(w, ts)
    assert r.returncode == 1 and "bad-report" in r.stdout, r.stdout


def test_the_check_writes_nothing(w):
    ts = _repaired(w, _foreign_staged)

    def digest() -> str:
        h = hashlib.sha256()
        for p in sorted((w.gen / "deploy-backups").rglob("*")):
            h.update(str(p).encode())
            if p.is_file() and not p.is_symlink():
                h.update(p.read_bytes())
        return h.hexdigest()

    before, snap, index = digest(), w.snapshot(), (w.root / ".git" / "index").read_bytes()
    assert _check(w, ts).returncode == 0
    assert digest() == before and w.snapshot() == snap
    assert (w.root / ".git" / "index").read_bytes() == index


def test_a_staged_file_below_a_row_is_other(w):
    """#3721 Codex P1: a file staged below a restorable path after the repair
    (its disk node gone again) leaves the path as-left on disk, but the index
    below it is not the tag's: nothing there may be written."""
    ts = _repaired(w, _foreign_gitlink)  # Z5, a gitlink and its directory
    oid = _hash(w, "NEW\n")
    _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},Z5/new")
    r = _check(w, ts)
    assert r.returncode == 1
    assert _parts(r)[("Z5", "node")] == "other:index-changed", r.stdout


def test_a_disk_edit_after_the_repair_is_other(w):
    ts = _repaired(w, _foreign_edit)
    (w.root / "sub" / "s1").write_text("edited since\n")
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "other:changed", r.stdout


def test_head_moved_judges_by_the_path_upstream(w):
    """HEAD left the tag: a path HEAD changed is refused by name; a path HEAD
    did not change is still as-left."""
    ts = _repaired(w, _foreign_edit)
    (w.root / "K").write_text("upstream\n")
    _git(w.root, "commit", "-qam", "upstream, elsewhere")
    r = _check(w, ts)
    assert r.returncode == 0 and _parts(r)[("sub/s1", "node")] == "as-left", r.stdout
    (w.root / "sub" / "s1").write_text("upstream s1\n")
    _git(w.root, "commit", "-qam", "upstream, the path")
    r = _check(w, ts)
    assert r.returncode == 1
    assert _parts(r)[("sub/s1", "node")] == "other:changed-upstream", r.stdout


def test_a_symlinked_ancestor_made_after_the_repair_is_other(w, tmp_path):
    ts = _repaired(w, _foreign_edit)
    shutil.move(w.root / "sub", tmp_path / "elsewhere")
    os.symlink(tmp_path / "elsewhere", w.root / "sub")
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "other:link-parent", r.stdout


def test_a_gitlink_directory_populated_after_the_repair_is_other(w):
    ts = _repaired(w, _foreign_gitlink)
    (w.root / "Z5").mkdir()
    (w.root / "Z5" / "f").write_text("populated\n")
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[("Z5", "node")] == "other:changed", r.stdout


@pytest.mark.parametrize("newer_path", ["sub", "sub/s1", "sub/s1/deeper"])
def test_a_newer_report_overlapping_the_path_is_other(w, newer_path):
    ts = _repaired(w, _foreign_edit)
    d = _newer_dir(w, ts)
    (d / "REPAIR.json").write_text(
        json.dumps({"repo": str(w.root), "paths": [{"path": newer_path, "class": "engine"}]})
    )
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "other:newer-report", r.stdout
    assert d.name[: -len(SUFFIX)] in r.stdout, "security L4: the newer report is not named"


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([{"path": ["sub"], "class": "engine"}], id="path-list"),
        pytest.param([{"class": "engine"}], id="no-path"),
        pytest.param([{"path": "sub/../sub", "class": "engine"}], id="not-canonical"),
        pytest.param(["sub"], id="not-an-object"),
    ],
)
def test_a_newer_report_whose_row_path_cannot_be_read_refuses(w, rows):
    """#3966 item 1: the overlap check reads every newer row's path; a row
    with no plain path could hide an overlap, so the restore is refused."""
    ts = _repaired(w, _foreign_edit)
    d = _newer_dir(w, ts)
    (d / "REPAIR.json").write_text(json.dumps({"repo": str(w.root), "paths": rows}))
    r = _check(w, ts)
    assert r.returncode == 1 and "other:newer-report-unreadable" in r.stdout, r.stdout + r.stderr
    assert d.name[: -len(SUFFIX)] in r.stdout


def test_a_newer_report_elsewhere_and_an_older_one_do_not_block(w):
    ts = _repaired(w, _foreign_edit)
    d = _newer_dir(w, ts)
    (d / "REPAIR.json").write_text(json.dumps({"repo": str(w.root), "paths": [{"path": "sub2"}]}))
    old = w.gen / "deploy-backups" / f"20000101T000000Z-1{SUFFIX}"
    old.mkdir()
    (old / "REPAIR.json").write_text("not json")
    r = _check(w, ts)
    assert r.returncode == 0, r.stdout


def test_an_unreadable_newer_report_refuses_the_whole_report(w):
    ts = _repaired(w, _foreign_edit)
    d = _newer_dir(w, ts)
    (d / "REPAIR.json").write_text("not json")
    r = _check(w, ts)
    assert r.returncode == 1 and "other:newer-report-unreadable" in r.stdout, r.stdout
    assert _parts(r) == {}
    assert d.name[: -len(SUFFIX)] in r.stdout, "security L4: the blocking report is not named"


# ── the validators ──────────────────────────────────────────────────────────


def _row(report: dict, path: str) -> dict:
    return next(r for r in report["paths"] if r["path"] == path)


def _moved(report: dict, path: str) -> None:
    """Row A renamed with its own saved_as following: only the path rule refuses it."""
    _row(report, "A").update(path=path, saved_as=f"tree/{path}")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: _moved(m, "../x"), id="dotdot-path"),
        pytest.param(lambda m: _moved(m, "sub/.git/x"), id="dot-git"),
        pytest.param(lambda m: _moved(m, "A\0"), id="nul-path"),
        pytest.param(
            lambda m: _row(m, "A")["index"][0].__setitem__(1, "0" * 39 + "\0"), id="nul-oid"
        ),
        pytest.param(lambda m: _row(m, "A")["index"][0].__setitem__(2, "4"), id="stage"),
        pytest.param(
            lambda m: _row(m, "A").update(blobs=["index-blobs/" + "e" * 40]), id="blob-not-its"
        ),
        pytest.param(
            lambda m: _row(m, "A").update(blobs=_row(m, "A")["blobs"] * 2), id="blob-twice"
        ),
        pytest.param(lambda m: _row(m, "A").update(saved_as="tree/K"), id="saved-as"),
        pytest.param(lambda m: _row(m, "A").update(unreachable_through="B"), id="through"),
        pytest.param(lambda m: _row(m, "A").update(flag="H"), id="flag"),
        # #3965 item 3: an unhashable value refuses by name, not with a TypeError.
        pytest.param(lambda m: _row(m, "A").update(flag=["S"]), id="flag-list"),
        pytest.param(lambda m: _row(m, "A").update(disk=["file"]), id="disk-list"),
        pytest.param(lambda m: _row(m, "A").update(**{"class": {"x": 1}}), id="class-object"),
        pytest.param(lambda m: _row(m, "A").update(**{"class": "mine"}), id="class"),
        pytest.param(lambda m: _row(m, "A").update(tag=["100644", "nothex"]), id="tag-entry"),
        pytest.param(lambda m: m.update(tag="-HEAD"), id="report-tag"),
        pytest.param(
            lambda m: m["paths"].extend(
                {"path": "/".join(["d"] * 1500) + f"/{n}"} for n in range(2)
            ),
            id="depth-budget",
        ),
    ],
)
def test_a_row_the_writer_does_not_write_refuses_the_report(w, change):
    ts = _repaired(w, _foreign_staged)
    control = _check(w, ts)
    assert control.returncode == 0 and "bad-report" not in control.stdout, control.stdout
    _edit_report(w, change)
    r = _check(w, ts)
    assert r.returncode == 1 and "other:bad-report" in r.stdout, r.stdout + r.stderr
    assert _parts(r) == {}


# ── the run around the judge ────────────────────────────────────────────────


def test_a_held_update_lock_is_waited_for_then_refused(w):
    ts = _repaired(w, _foreign_edit)
    lock = w.gen / "locks" / "update.lock"
    lock.parent.mkdir(exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        r = _check(w, ts, "--wait", "1")
        took = time.monotonic() - start
    finally:
        os.close(fd)
    assert r.returncode == 1 and "update.lock" in r.stdout and _parts(r) == {}, r.stdout
    assert took < 30


def test_a_half_written_checkout_is_refused(w):
    ts = _repaired(w, _foreign_edit)
    w.write_state(w.row())  # the guard: mixed_tree again (a second kill)
    (w.root / "A").write_text("a2 with more bytes\n")
    r = _check(w, ts)
    assert r.returncode == 1 and "half-written" in r.stdout and _parts(r) == {}, r.stdout


@pytest.mark.parametrize("ts", ["../x", "20261103T000000Z-1/../../x", "x"])
def test_a_ts_that_names_no_report_is_refused(w, ts):
    """#3733: --dismiss (and --restore) validate the ts before building a path."""
    target = w.gen / "x-live-move-repair"
    target.mkdir()
    (target / "REPAIR.json").write_text(json.dumps({"repo": str(w.root), "paths": []}))
    for args in (["--dismiss", ts], ["--restore", ts, "--check"]):
        r = subprocess.run(
            ["python3", "-I", "-S", str(MODULE), *args, str(w.state), str(w.root)],
            capture_output=True,
            text=True,
            env=w.env(),
        )
        assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert "dismissed_at" not in json.loads((target / "REPAIR.json").read_text())


def test_dismiss_reads_through_the_loader(w):
    """A report the loader refuses (another checkout's) is not dismissed."""
    ts = _repaired(w, _foreign_edit)
    path = w.backups()[-1] / "REPAIR.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "repo": "/elsewhere"}))
    r = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, str(w.state), str(w.root)],
        capture_output=True, text=True, env=w.env(),
    )  # fmt: skip
    assert r.returncode == 1 and json.loads(path.read_text())["dismissed_at"] is None


def test_an_unreadable_tag_refuses_the_whole_report(w):
    ts = _repaired(w, _foreign_edit)
    _edit_report(w, lambda m: m.update(tag="f" * 40))
    r = _check(w, ts)
    assert r.returncode == 1 and "other:tree-unreadable" in r.stdout and _parts(r) == {}, r.stdout


def test_an_unreadable_target_restores_only_foreign_rows_of_a_directory(w):
    """Closure row 3: with the move target unreadable, a saved entry that is
    not itself a foreign row cannot be told from the update's bytes."""
    ts = _repaired(w, _foreign_inside_dir)
    _edit_report(w, lambda m: m.update(other="e" * 40))
    r = _check(w, ts)
    parts = _parts(r)
    assert r.returncode == 1 and parts[("P/mine.txt", "entry")] == "other:target-unreadable", parts
    assert parts[("P/inner", "entry")] == "other:target-unreadable", parts


# ── a tag directory, a saved blob, a symlinked ancestor (the B1 review) ─────


@pytest.mark.parametrize(
    ("make", "edit", "part"),
    [
        (_ancestor_file, "sub/s1", ("sub/s1", "index")),
        (_dir_becomes_file, "sub/t", ("sub", "node")),
    ],
)
def test_an_untracked_file_under_a_tag_directory_changes_no_part(w, make, edit, part):
    """Review SF1: the repair never removes an untracked file under a tag
    directory that is not a row (a cache the server writes), so the directory
    is compared on the tag's own paths only. Control: one of those paths
    edited is still `other:changed`."""
    ts = _repaired(w, make)
    (w.root / "sub" / "__pycache__").mkdir()
    (w.root / "sub" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    r = _check(w, ts)
    parts = _parts(r)
    assert r.returncode == 0 and parts and set(parts.values()) == {"as-left"}, r.stdout
    (w.root / edit).write_text("edited since\n")
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[part] == "other:changed", r.stdout


def _staged_row(w) -> tuple[str, Path]:
    oid = next(r for r in w.report()["paths"] if r["path"] == "A")["index"][0][1]
    return oid, w.backups()[-1] / "index-blobs" / oid


def _gone(w, oid: str) -> None:
    _gc(w)
    gone = subprocess.run(["git", "-C", str(w.root), "cat-file", "-e", oid], env=w.env())
    assert gone.returncode != 0, "control: gc did not prune the staged blob"


@pytest.mark.parametrize(
    ("damage", "verdict"),
    [
        ("none, gc", "as-left"),
        ("saved removed, the store still holds it", "as-left"),
        ("saved removed, gc", "other:not-saved"),
        ("saved truncated, gc", "other:not-saved"),
        ("saved a symlink to a good copy, gc", "other:not-saved"),
    ],
)
def test_an_index_part_whose_blob_is_nowhere_is_not_saved(w, tmp_path, damage, verdict):
    """Review SF2: the writer skips a blob the object store lacks, and a saved
    one can be lost later. An index part whose blob is neither saved (a regular
    file at index-blobs/<oid> hashing to it) nor in the object store cannot be
    written: `other:not-saved`."""
    ts = _repaired(w, _foreign_staged)
    oid, saved = _staged_row(w)
    assert saved.is_file(), "control: the writer saved the staged blob"
    if damage.startswith("saved removed"):
        saved.unlink()
    elif damage.startswith("saved truncated"):
        saved.write_bytes(b"staged")
    elif damage.startswith("saved a symlink"):
        good = tmp_path / "good-copy"
        shutil.copy2(saved, good)
        saved.unlink()
        os.symlink(good, saved)
    if damage.endswith("gc"):
        _gone(w, oid)
    r = _check(w, ts)
    assert _parts(r)[("A", "index")] == verdict, r.stdout
    assert r.returncode == (0 if verdict == "as-left" else 1), r.stdout


def _foreign_dir_at_s1(w):
    """sub/s1 (a tag file) made a directory holding a file of its own."""
    (w.root / "sub" / "s1").unlink()
    (w.root / "sub" / "s1").mkdir()
    (w.root / "sub" / "s1" / "mine.txt").write_text("notes\n")


def test_a_directory_row_under_a_symlinked_ancestor_is_never_walked(w, tmp_path):
    """Review SF3: with `sub` made a symlink to a directory outside the
    checkout that holds the saved copy, nothing under it is read (a directory
    there that cannot be listed would stop a walk): the parts are
    `other:link-parent`."""
    ts = _repaired(w, _foreign_dir_at_s1)
    outside = tmp_path / "outside"
    (outside / "s1" / "locked").mkdir(parents=True)
    (outside / "s1" / "mine.txt").write_text("notes\n")
    (outside / "s1" / "locked").chmod(0)
    shutil.rmtree(w.root / "sub")
    os.symlink(outside, w.root / "sub")
    try:
        r = _check(w, ts)
    finally:
        (outside / "s1" / "locked").chmod(0o700)
    parts = _parts(r)
    assert parts.get(("sub/s1", "node")) == "other:link-parent", r.stdout + r.stderr
    assert parts[("sub/s1/mine.txt", "entry")] == "other:link-parent", parts


@pytest.mark.parametrize("at", ["tree/sub/s1", "tree/sub"])
def test_a_saved_directory_behind_a_symlink_in_the_backup_is_not_saved(w, tmp_path, at):
    """Review SF3: the saved copy is read without following a link at
    tree/<p> or at any ancestor inside the backup."""
    ts = _repaired(w, _foreign_dir_at_s1)
    link = w.backups()[-1] / at
    shutil.copytree(link, tmp_path / "copy", symlinks=True)
    shutil.rmtree(link)
    os.symlink(tmp_path / "copy", link)
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "other:not-saved", r.stdout


def _staged_child_type_change(w):
    """A staged sub/s1, then `sub` made a file: the child sits beyond it."""
    (w.root / "sub" / "s1").write_text("STAGED S1\n")
    _git(w.root, "add", "--", "sub/s1")
    shutil.rmtree(w.root / "sub")
    (w.root / "sub").write_text("now a file\n")


def _ancestor_file_child_edit(w):
    """An index file `sub` over the tag's sub/, and sub/s1 edited on disk."""
    _ancestor_file(w)
    (w.root / "sub" / "s1").write_text("mine\n")


@pytest.mark.parametrize(
    ("make", "child", "blocked"),
    [
        (_staged_child_type_change, ("sub/s1", "index"), "other:link-parent"),
        (_ancestor_file_child_edit, ("sub/s1", "node"), "other:index-above"),
    ],
)
def test_a_parent_this_report_restored_does_not_refuse_its_own_child(w, make, child, blocked):
    """Review SF4 (MEASURED with the real writer): a report holds a parent and
    a child that both need a write, and once the parent is back (put back by
    hand here, parent first) the parent itself is a non-directory or an index
    entry above the child. A restore stopped between the two must still
    converge: that parent does not refuse this report's own child, and the
    real restore then finishes."""
    ts = _repaired(w, make)
    report = w.report()
    assert sorted(r["path"] for r in report["paths"] if r["class"] == "foreign")[:2] == [
        "sub",
        "sub/s1",
    ], "control: the report holds the parent and the child"
    parent = next(r for r in report["paths"] if r["path"] == "sub")
    if make is _staged_child_type_change:  # the parent's node: the file `sub`
        shutil.rmtree(w.root / "sub")
        shutil.copy2(w.backups()[-1] / parent["saved_as"], w.root / "sub")
        (w.root / "sub").chmod(0o644)
    else:  # the parent's index entry: the file `sub` over sub/
        _git(w.root, "update-index", "--force-remove", "sub/s1", "sub/t")
        mode, oid, _ = parent["index"][0]
        _git(w.root, "update-index", "--add", "--cacheinfo", f"{mode},{oid},sub")
    r = _check(w, ts)
    parts = _parts(r)
    assert parts[("sub", "node" if make is _staged_child_type_change else "index")] == (
        "already-restored"
    ), parts
    assert parts[child] == "as-left", f"{child} would read {blocked} forever: {parts}"
    restore_all(w)
    r = _check(w, ts)
    assert r.returncode == 0 and set(_parts(r).values()) == {"already-restored"}, r.stdout


def test_a_newer_report_of_another_checkout_does_not_block(w):
    """Review LOW-1: a newer report naming another checkout cannot overlap this one."""
    ts = _repaired(w, _foreign_edit)
    (_newer_dir(w, ts) / "REPAIR.json").write_text(
        json.dumps({"repo": "/elsewhere", "paths": [{"path": "sub/s1"}]})
    )
    r = _check(w, ts)
    assert r.returncode == 0 and _parts(r)[("sub/s1", "node")] == "as-left", r.stdout


def test_a_report_name_that_is_no_date_is_refused_not_crashed(w):
    """Review LOW-2: a dir named like a report whose ts is no date. Judged
    itself: refused as unreadable, not a crash. Beside another report: it may
    be newer, so that one is refused (newer-report-unreadable)."""
    ts = _repaired(w, _foreign_edit)
    bad, real = "20261399T000000Z-1", w.backups()[-1] / "REPAIR.json"
    d = w.gen / "deploy-backups" / f"{bad}{SUFFIX}"
    d.mkdir()
    shutil.copy2(real, d / "REPAIR.json")  # a valid report
    r = _check(w, bad)
    assert r.returncode == 1 and "other:unreadable" in r.stdout, r.stdout + r.stderr
    assert "failed" not in r.stderr and "Traceback" not in r.stderr, r.stderr
    r = _check(w, ts)
    assert r.returncode == 1 and "other:newer-report-unreadable" in r.stdout, r.stdout


def test_a_missing_report_dir_is_named_so(w):
    """Review LOW-5."""
    r = _check(w, "20261104T000000Z-1")
    assert r.returncode == 1 and "no such report" in r.stdout, r.stdout + r.stderr


def test_an_unreadable_path_refuses_the_whole_report_by_name(w):
    """Review LOW-4: a directory the judge cannot list is named as such, never
    the repair's own "failed ...; the update state is kept"."""
    ts = _repaired(w, _foreign_dir_at_s1)
    locked = w.backups()[-1] / "tree" / "sub" / "s1" / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        r = _check(w, ts)
    finally:
        locked.chmod(0o700)
    assert r.returncode == 1 and "other:unreadable (a path cannot be read" in r.stdout, r.stdout
    assert "failed" not in r.stderr and _parts(r) == {}, r.stderr


def test_a_torn_index_at_the_repair_is_said(w):
    """Review LOW-7: after a torn index the staged entries live only in the
    backup's git/index, which no part judges; the summary says so."""
    ts = _repaired(w, _foreign_edit)
    assert "torn" not in _check(w, ts).stdout
    _edit_report(w, lambda m: m.update(index_rebuilt=True))
    assert "the index was torn" in _check(w, ts).stdout


# ── the B1 security review ──────────────────────────────────────────────────

GIB = 1024**3


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


#: The address space the check may use: far under the 3 GiB file, far over
#: what it needs (it hashes in 1 MiB chunks; FILE_BYTES is 256 MiB).
_AS_CAP = 1 << 30

#: Sets RLIMIT_AS in a fresh small process, then execs the command under it.
_CAPPED = (
    "import os, resource, sys; c = int(sys.argv[1]); "
    "resource.setrlimit(resource.RLIMIT_AS, (c, c)); os.execvp(sys.argv[2], sys.argv[2:])"
)


def _run_capped(argv: list[str], env: dict, timeout: float):
    """Run argv with its address space capped at _AS_CAP, so reading the
    file whole fails (MemoryError) instead of passing.

    A child's ru_maxrss cannot be measured from the suite: it starts from the
    parent's resident set at fork (a `pass` child of an 800 MiB parent reads
    830,752 KiB), and CI runs the whole suite in one process. A limit set
    after the fork, in the small wrapper, does not depend on the parent."""
    return subprocess.run(
        ["python3", "-I", "-S", "-c", _CAPPED, str(_AS_CAP), *argv],
        env=env, capture_output=True, text=True, timeout=timeout,
    )  # fmt: skip


@pytest.mark.parametrize("where", ["checkout", "backup"])
def test_a_huge_file_is_too_large_promptly_never_read_whole(w, where):
    """Security S1 (the review's 3 GiB case, a sparse file): a node the judge
    would hash is over the per-file budget, in the checkout (a gitlink's
    directory, walked) or in the backup (a saved copy). It answers promptly
    as `other:too-large` under a 1 GiB address-space cap, so it never read the
    file whole."""

    ts = _repaired(w, _foreign_gitlink if where == "checkout" else _foreign_edit)
    if where == "checkout":
        _sparse(w.root / "Z5" / "big", 3 * GIB)
        part = ("Z5", "node")
    else:
        saved = w.backups()[-1] / "tree" / "sub" / "s1"
        saved.unlink()
        _sparse(saved, 3 * GIB)
        part = ("sub/s1", "node")
    start = time.monotonic()
    r = _run_capped(
        ["python3", "-I", "-S", str(MODULE), "--restore", ts, "--check", str(w.state), str(w.root)],
        w.env(), 120,
    )  # fmt: skip
    took = time.monotonic() - start
    assert _parts(r).get(part) == "other:too-large", r.stdout + r.stderr
    assert r.returncode == 1 and took < 60, took


def test_node_never_follows_or_blocks_on_a_node_swapped_after_its_lstat(mod, tmp_path, monkeypatch):
    """Security S2: lstat says regular file, then the path is a FIFO (an open
    would block forever) or a symlink (an open would read its target). The
    read opens without following or blocking, checks the opened file is the
    one lstat saw, and fails loudly instead."""
    import threading

    (tmp_path / "regular").write_text("r\n")
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "target").write_text("not this\n")
    os.symlink(tmp_path / "target", tmp_path / "link")
    real, regular = os.lstat, os.lstat(tmp_path / "regular")
    swapped = {str(tmp_path / "fifo"), str(tmp_path / "link")}
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **k: regular if str(p) in swapped else real(p, *a, **k)
    )
    for name in ("fifo", "link"):
        got: list = []

        def read(name=name, got=got):
            try:
                got.append(mod._node(str(tmp_path), name, "sha1"))
            except OSError as exc:
                got.append(exc)

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(10)
        assert got, f"{name}: the read blocked"
        assert isinstance(got[0], OSError), f"{name}: read {got[0]} instead of failing"


_ROW_KEYS = sorted(
    ["path", "class", "disk", "disk_changed", "tag", "other", "index", "flag", "special",
     "saved_as", "blobs", "unsaved", "unreachable_through"]
)  # fmt: skip


def test_the_writer_writes_every_row_key_the_validator_requires(w):
    """Security L1, the whole set at once: the writer's real rows hold exactly
    the keys the validator requires (one list, both sides)."""
    _repaired(w, _foreign_staged)
    assert {tuple(sorted(r)) for r in w.report()["paths"]} == {tuple(_ROW_KEYS)}


@pytest.mark.parametrize("key", _ROW_KEYS)
def test_a_row_missing_a_key_is_a_bad_report_never_a_crash(w, key):
    """Security L1: a row missing any key the writer writes is refused as a
    bad report naming the key, never a KeyError through the repair's handler."""
    ts = _repaired(w, _foreign_staged)
    _edit_report(w, lambda m: _row(m, "A").pop(key))
    r = _check(w, ts)
    assert r.returncode == 1 and "other:bad-report" in r.stdout and key in r.stdout, (
        r.stdout + r.stderr
    )
    assert "failed" not in r.stderr, r.stderr


@pytest.mark.parametrize("args", [["--dismiss", ""], ["--restore", "", "--check"]])
def test_an_empty_ts_is_refused_by_the_ts_check(w, args):
    """Security L2: an empty ts is refused by the ts check itself (exit 2,
    named), never read as "no ts given" and run as the writing repair."""
    killed_tag_side(w, "Z3")
    before = w.state.read_bytes()
    r = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), *args, str(w.state), str(w.root)],
        capture_output=True, text=True, env=w.env(),
    )  # fmt: skip
    assert r.returncode == 2 and "is not a repair report's ts" in r.stderr, r.stdout + r.stderr
    assert w.state.read_bytes() == before and w.backups() == []


def test_a_symlinked_lock_file_is_refused_by_name_never_followed(w, tmp_path):
    """Security L3: locks/update.lock planted as a dangling symlink is neither
    followed (no file appears at its target) nor taken; the refusal names it."""
    ts = _repaired(w, _foreign_edit)
    lock = w.gen / "locks" / "update.lock"
    lock.unlink(missing_ok=True)
    os.symlink(tmp_path / "planted", lock)
    r = _check(w, ts)
    assert not (tmp_path / "planted").exists(), "the lock's symlink was followed"
    assert r.returncode == 1 and "update.lock" in r.stdout + r.stderr and _parts(r) == {}, (
        r.stdout + r.stderr
    )


def test_newer_reports_together_are_held_to_one_reports_caps(w):
    """Security L4: the newer reports read for the overlap check are bounded
    together by one report's row cap (REPORT_ROWS, 20,000); over it the whole
    report is refused, naming the newer report that crossed it."""
    ts = _repaired(w, _foreign_edit)
    first = _newer_dir(w, ts)
    stamp = calendar.timegm(time.strptime(ts.split("-", 1)[0], "%Y%m%dT%H%M%SZ")) + 120
    second = first.with_name(f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp))}-8{SUFFIX}")
    second.mkdir()
    for n, d in enumerate((first, second)):
        paths = [{"path": f"elsewhere{n}/{i}"} for i in range(10_001)]
        (d / "REPAIR.json").write_text(json.dumps({"repo": str(w.root), "paths": paths}))
    r = _check(w, ts)
    assert r.returncode == 1 and "other:newer-report-unreadable" in r.stdout, r.stdout
    assert first.name[: -len(SUFFIX)] in r.stdout or second.name[: -len(SUFFIX)] in r.stdout


# ── #3492 B2a: the integrity record (SAVED.json) and --dismiss under the lock ──


def test_an_old_report_without_the_integrity_record_is_judged_as_before(w):
    """A report written before SAVED.json existed names none: --check judges it
    exactly as one that does, and says a restore refuses it whole (review N-3;
    the check only reads)."""
    ts = _repaired(w, _foreign_inside_dir)
    with_record = _check(w, ts)
    assert with_record.returncode == 0, with_record.stdout
    _edit_report(w, lambda report: report.pop("integrity"))
    (w.backups()[-1] / "SAVED.json").unlink()
    old = _check(w, ts)
    assert old.returncode == 0 and _parts(old) == _parts(with_record), old.stdout
    assert "a restore refuses it" in old.stdout, old.stdout


def _saved_path(w) -> Path:
    return w.backups()[-1] / "SAVED.json"


def _rewrite_saved(change):
    def edit(w):
        data = json.loads(_saved_path(w).read_text())
        change(data)
        _saved_path(w).write_text(json.dumps(data))

    return edit


def _saved_link(w):
    real = w.backups()[-1] / "elsewhere.json"
    _saved_path(w).rename(real)
    os.symlink(real.name, _saved_path(w))


def _saved_over_cap(w):
    data = _saved_path(w).read_bytes()
    _saved_path(w).write_bytes(data + b" " * (32 * 1024 * 1024))  # still valid JSON


def _first_record(change):
    def edit(data):
        key = next(iter(data["files"]))
        data["files"][key] = change(dict(data["files"][key]))

    return edit


_BAD_SAVED = {
    "named-elsewhere": lambda w: _edit_report(w, lambda r: r.update(integrity="../x.json")),
    "named-not-a-str": lambda w: _edit_report(w, lambda r: r.update(integrity=["SAVED.json"])),
    "missing": lambda w: _saved_path(w).unlink(),
    "symlink": _saved_link,
    "over-the-cap": _saved_over_cap,
    "not-json": lambda w: _saved_path(w).write_text("{"),
    "not-an-object": lambda w: _saved_path(w).write_text("[]"),
    "extra-key": _rewrite_saved(lambda d: d.update(more=1)),
    "complete-not-bool": _rewrite_saved(lambda d: d.update(complete="yes")),
    "key-outside-tree": _rewrite_saved(
        lambda d: d["files"].update({"../x": d["files"].popitem()[1]})
    ),
    "key-with-dotdot": _rewrite_saved(
        lambda d: d["files"].update({"tree/../x": d["files"].popitem()[1]})
    ),
    "bad-type": _rewrite_saved(_first_record(lambda r: {**r, "type": "socket"})),
    "bad-mode": _rewrite_saved(_first_record(lambda r: {**r, "mode": "644"})),
    "bad-sha256": _rewrite_saved(_first_record(lambda r: {**r, "sha256": "ab" * 31})),
    "file-extra-field": _rewrite_saved(_first_record(lambda r: {**r, "target": "x"})),
    "symlink-target-nul": _rewrite_saved(
        _first_record(lambda r: {"type": "symlink", "target": "a\u0000b"})
    ),
    # Security review N1: more records than a check may look at (ENTRIES).
    "more-records-than-entries": _rewrite_saved(
        lambda d: d["files"].update(
            {f"tree/n{i}": next(iter(d["files"].values())) for i in range(200_001)}
        )
    ),
}


@pytest.mark.parametrize("case", sorted(_BAD_SAVED))
def test_a_bad_integrity_record_is_a_bad_report(w, case):
    """B1's validators shape-check the new fields: REPAIR.json's `integrity`
    names exactly SAVED.json, opened as REPAIR.json is (no symlink, no FIFO,
    at most SAVED_BYTES), holding {files, complete} and records of the three
    kinds the writer writes. Anything else refuses the whole report."""
    ts = _repaired(w, _foreign_inside_dir)
    assert _check(w, ts).returncode == 0, "control: the real record is accepted"
    _BAD_SAVED[case](w)
    r = _check(w, ts)
    assert r.returncode == 1 and "other:bad-report" in r.stdout, r.stdout
    assert "SAVED.json" in r.stdout and _parts(r) == {}, r.stdout


def test_dismiss_takes_update_lock_and_keeps_the_report_private(w):
    """Review SF-7: --dismiss rewrites REPAIR.json, as the restore will; it
    waits for update.lock (the short wait) and, still held, changes nothing.
    Its rewrite is 0600 like the writer's."""
    ts = _repaired(w, _foreign_edit)
    path = w.backups()[-1] / "REPAIR.json"
    before = path.read_bytes()
    lock = w.gen / "locks" / "update.lock"
    lock.parent.mkdir(exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    dismiss = ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, "--wait", "0",
               str(w.state), str(w.root)]  # fmt: skip
    try:
        r = subprocess.run(dismiss, capture_output=True, text=True, env=w.env())
        assert r.returncode == 1 and "update.lock" in r.stderr, r.stderr
        assert path.read_bytes() == before, "dismissed while update.lock was held"
    finally:
        os.close(fd)
    r = subprocess.run(dismiss, capture_output=True, text=True, env=w.env())
    assert r.returncode == 0, r.stderr
    assert json.loads(path.read_text())["dismissed_at"]
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


# ── #3492 B2b: the gates --check applies before it judges ───────────────────


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
    REPAIR.json is parsed."""
    ts = _repaired(w, lambda w: (_foreign_staged(w), _foreign_edit(w))[1])
    assert _check(w, ts).returncode == 0, "control: the report judges clean"
    node = _group_write(w, where)
    node.chmod(stat.S_IMODE(os.lstat(node).st_mode) | 0o020)
    r = _check(w, ts)
    assert r.returncode == 1 and "other:not-owned" in r.stdout and _parts(r) == {}, r.stdout


def test_a_symlinked_deploy_backups_is_refused(w, tmp_path):
    ts = _repaired(w, _foreign_edit)
    real = tmp_path / "elsewhere"
    shutil.move(w.gen / "deploy-backups", real)
    os.symlink(real, w.gen / "deploy-backups")
    r = _check(w, ts)
    assert r.returncode == 1 and "other:not-owned" in r.stdout and _parts(r) == {}, r.stdout


def test_a_report_another_user_owns_is_refused(w, mod, monkeypatch, capsys):
    """Every node not the effective uid's refuses (the uid itself faked: a
    test cannot chown)."""
    ts = _repaired(w, _foreign_edit)
    spec = importlib.util.spec_from_file_location("_restore_kit", LIB / "live_move_restore.py")
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    monkeypatch.setattr(kit.os, "geteuid", lambda: os.getuid() + 1)
    load = mod._load_kit
    monkeypatch.setattr(mod, "_load_kit", lambda n: kit if n == "live_move_restore.py" else load(n))
    capsys.readouterr()
    rc = mod.main(["--restore", ts, "--check", str(w.state), str(w.root)])
    out = capsys.readouterr().out
    assert rc == 1 and "other:not-owned" in out, out


def test_a_node_part_below_another_refuses_the_report(w):
    """Review LOW-2: the writer never writes a node part below another (a
    directory row covers its children; a non-directory parent makes them
    unreachable), so a report holding one is refused whole."""
    ts = _repaired(w, _foreign_edit)

    def nest(m):
        row = next(r for r in m["paths"] if r["path"] == "sub/s1")
        m["paths"].append({**row, "path": "sub/s1/deeper", "saved_as": "tree/sub/s1/deeper"})

    _edit_report(w, nest)
    r = _check(w, ts)
    assert r.returncode == 1 and "a node part below another" in r.stdout, r.stdout
    assert _parts(r) == {}, r.stdout


def test_the_check_never_runs_the_repositorys_fsmonitor(w, tmp_path):
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
    r = _check(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists(), "the check ran the repository's fsmonitor"
