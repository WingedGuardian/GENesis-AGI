"""Every restore run folder of a report is found and judged (#4011).

A restore moves the node at a path aside into its run folder
(deploy-backups/<UTC>-<pid>-<report ts>-live-move-restore/aside/<path>).
REPAIR.json's in-flight slot names only one run, so an aside a later write
filled after its run finished, or one whose record a later node overwrote,
used to be invisible, and a pruned held folder read as released. Each restore
and --check now lists the report's own run folders by name and judges every
aside in them; a held in-flight folder that is gone is reported lost.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from tests.test_scripts._live_repair_world import _foreign_edit
from tests.test_scripts.test_live_move_restore import _check, _parts
from tests.test_scripts.test_live_move_restore_write import (
    KILL_ENV,
    _repaired,
    _restore,
    _runs,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")


def test_a_run_folder_is_named_for_its_report(w):
    ts = _repaired(w, _foreign_edit)
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout + r.stderr
    [run] = _runs(w)
    assert run.name.endswith(f"-{ts}-live-move-restore"), run.name


def test_an_aside_written_after_its_run_finished_is_named_on_every_run(w):
    """#4011: the restore put the saved file in and judged its aside as-left,
    so it cleared its record; a write through a descriptor opened on the
    replaced file then fills the aside. No in-flight record names it, yet
    every --check and --restore names it, refuses it and leaves restored_at
    unwritten, so the report's alert stays up."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)  # opened before the restore
    try:
        r = _restore(w, ts)
        assert r.returncode == 0 and w.report()["restore"]["inflight"] is None, r.stdout
        os.ftruncate(fd, 0)
        os.pwrite(fd, b"newer, through an open fd\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    aside = run / "aside" / "sub" / "s1"
    assert aside.read_text() == "newer, through an open fd\n", "control: the fd is the aside's"
    key = (f"{run.name}/aside/sub/s1", "aside")
    for r in (_check(w, ts), _restore(w, ts), _check(w, ts)):
        assert r.returncode == 1 and str(aside) in r.stdout, r.stdout
        assert _parts(r).get(("sub/s1", "index")) != "refused:changed-during-restore", r.stdout
        assert _parts(r)[key] in ("other:aside-held", "refused:other:aside-held")
    assert w.report()["restore"]["restored_at"] is None
    assert w.report()["restore"]["refused"] >= 1


def test_moving_an_aside_out_by_hand_releases_it(w):
    """The documented release: what the aside held is moved out while its run
    folder stays; the next restore reads nothing held."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts).returncode == 0
        os.pwrite(fd, b"newer\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    (run / "aside" / "sub" / "s1").unlink()
    r = _restore(w, ts)
    assert r.returncode == 0, r.stdout
    assert isinstance(w.report()["restore"]["restored_at"], str)


def test_a_held_run_folder_that_is_gone_is_reported_lost_once(w):
    """#4025 item 2: a held in-flight aside whose whole run folder is gone
    (pruned, or removed) is said once, counted refused, and restored_at stays
    unwritten; the record is then cleared, and a later run reports normally."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts, **{KILL_ENV: "put-in"}).returncode == -9
        os.ftruncate(fd, 0)
        os.pwrite(fd, b"newer, through an open fd\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    assert w.report()["restore"]["inflight"][0] == run.name, "control: the record names it"
    shutil.rmtree(run)
    key = (f"{run.name}/aside/sub/s1", "aside")
    c = _check(w, ts)
    assert c.returncode == 1 and _parts(c)[key] == "other:aside-lost", c.stdout
    assert "is gone with its run folder: what it held was not restored" in c.stdout
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[key] == "refused:other:aside-lost", r.stdout
    block = w.report()["restore"]
    assert block["inflight"] is None and block["restored_at"] is None and block["refused"] >= 1
    again = _restore(w, ts)
    assert again.returncode == 0 and key not in _parts(again), again.stdout
    assert isinstance(w.report()["restore"]["restored_at"], str)


def test_a_run_folder_that_is_not_this_users_alone_is_named_not_skipped(w):
    ts = _repaired(w, _foreign_edit)
    assert _restore(w, ts).returncode == 0
    [run] = _runs(w)
    run.chmod(0o755)
    r = _check(w, ts)
    assert r.returncode == 1 and _parts(r)[(run.name, "aside")] == "other:not-owned", r.stdout


def test_more_run_folders_than_the_cap_refuse_the_report_whole(w):
    from tests.test_scripts.test_live_move_restore_write import _kit

    ts = _repaired(w, _foreign_edit)
    cap = _kit().RUNS_CAP
    for n in range(cap + 1):
        (w.gen / "deploy-backups" / f"20260101T000000Z-{n}-{ts}-live-move-restore").mkdir(
            mode=0o700
        )
    r = _check(w, ts)
    assert r.returncode == 1 and f"other:too-large (over {cap} restore runs)" in r.stdout, r.stdout


def test_a_refused_rerun_leaves_no_new_run_folder(w):
    """#4025 review SF-6: a run whose only write was staging (refused before
    anything moved) removes its folder, so refusals never pile up folders
    against the cap."""
    ts = _repaired(w, _foreign_edit)
    (w.backups()[-1] / "tree" / "sub" / "s1").write_text("not what was saved\n")
    for _ in range(3):
        r = _restore(w, ts)
        assert r.returncode == 1 and _parts(r)[("sub/s1", "node")] == "refused:other:hash-mismatch"
    assert _runs(w) == [], _runs(w)


def test_one_path_held_in_two_runs_is_named_twice(w):
    """#4011 review SF-1: each held aside is its own part, keyed by its run, so
    neither copy of newer content goes unnamed."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts).returncode == 0
        os.pwrite(fd, b"newer one\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    twin = run.with_name("29" + run.name[2:])
    shutil.copytree(run, twin, symlinks=True)
    os.chmod(twin, 0o700)
    (twin / "aside" / "sub" / "s1").write_text("newer two\n")
    c = _check(w, ts)
    held = {k for k, v in _parts(c).items() if v == "other:aside-held"}
    want = {(f"{run.name}/aside/sub/s1", "aside"), (f"{twin.name}/aside/sub/s1", "aside")}
    assert held == want, c.stdout


def test_a_later_part_replacing_the_record_does_not_hide_an_earlier_aside(w):
    """#4011: the in-flight slot holds one record, and each later node part's
    intent replaces it; an earlier part's aside, written after its put-in, is
    still found by its run folder's name."""
    from tests.test_scripts._live_repair_world import _foreign_untracked

    def both(w):
        _foreign_edit(w)
        return _foreign_untracked(w)

    ts = _repaired(w, both)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        r = _restore(w, ts)
        assert r.returncode == 0, r.stdout
        os.pwrite(fd, b"newer\n", 0)
    finally:
        os.close(fd)
    assert w.report()["restore"]["inflight"] is None, "control: no record names the aside"
    [run] = _runs(w)
    c = _check(w, ts)
    assert _parts(c)[(f"{run.name}/aside/sub/s1", "aside")] == "other:aside-held", c.stdout


def test_an_in_flight_record_naming_another_report_is_not_read():
    from tests.test_scripts.test_live_move_restore_write import _kit

    kit = _kit()
    mine, other = "20260101T000000Z-1", "20250101T000000Z-2"
    block = {"inflight": [f"20260102T000000Z-9-{other}-live-move-restore", "a", "node"],
             "refused": 0, "restored_at": None}  # fmt: skip
    assert kit.restore_block({"restore": block}, mine) is None
    assert kit.restore_block({"restore": block}, other) == block
    old = {**block, "inflight": ["20260102T000000Z-9-live-move-restore", "a", "node"]}
    assert kit.restore_block({"restore": old}, mine) == old, "a folder named before #4011"


def test_check_and_restore_agree_beside_a_held_aside_and_a_damaged_copy(w):
    """#4011 rebase review LOW-1: an aside part and a --check prediction in
    one report. sub/s1's aside is held (a write through an open fd after its
    run), and Z5's saved copy is not what SAVED.json records: --check names
    exactly what --restore refuses, part for part."""
    from tests.test_scripts._live_repair_world import _foreign_untracked
    from tests.test_scripts.test_live_move_restore_write import _repaired

    def both(w):
        _foreign_edit(w)
        return _foreign_untracked(w)

    ts = _repaired(w, both)
    (w.backups()[-1] / "tree" / "Z5").write_text("not what was saved\n")
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts).returncode == 1  # Z5 refused; sub/s1 restored
        os.pwrite(fd, b"newer\n", 0)
    finally:
        os.close(fd)
    c, r = _check(w, ts), _restore(w, ts)
    said, did = _parts(c), _parts(r)
    assert "other:hash-mismatch" in said.values() and "other:aside-held" in said.values(), c.stdout
    assert said.keys() == did.keys(), (c.stdout, r.stdout)
    for key, v in said.items():
        want = {"as-left": "restored", "already-restored": "already-restored"}.get(
            v, f"refused:{v}"
        )
        assert did[key] == want, (key, c.stdout, r.stdout)


@pytest.mark.parametrize("locked", ["aside-dir", "run-folder"])
def test_an_aside_the_restore_cannot_search_is_refused_never_released(w, locked):
    """#4364 review (Codex): an aside behind a directory with no search bit is
    not absent. os.path.lexists says False there, which released a held aside
    in silence (rc 0, restored_at written); it is now `other:aside-unreadable`,
    refused, in both modes, and the report stays unresolved."""
    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts).returncode == 0
        os.pwrite(fd, b"newer\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    target = run / "aside" / "sub" if locked == "aside-dir" else run
    target.chmod(0)
    try:
        for r in (_check(w, ts), _restore(w, ts)):
            assert r.returncode == 1, r.stdout
            assert "aside-unreadable" in r.stdout or "not-owned" in r.stdout, r.stdout
        assert w.report()["restore"]["restored_at"] is None
        assert w.report()["restore"]["refused"] >= 1
    finally:
        target.chmod(0o700)


def test_a_run_whose_move_aside_is_refused_leaves_no_folder(w):
    """#4364 review round 2 (B1): the move aside fails (sub/ not writable),
    leaving only the empty aside/sub/ the move made; every rerun removes its
    own, so repeated refusals never pile folders up against RUNS_CAP."""
    ts = _repaired(w, _foreign_edit)
    sub = w.root / "sub"
    sub.chmod(0o555)
    try:
        for _ in range(3):
            r = _restore(w, ts)
            assert r.returncode == 1 and _parts(r)[("sub/s1", "node")].startswith("refused:"), (
                r.stdout
            )
    finally:
        sub.chmod(0o755)
    assert _runs(w) == [], _runs(w)


def test_a_run_put_back_after_a_session_write_leaves_no_folder(w, mod, monkeypatch, capsys):
    """#4364 review round 2 (B2): a session writes the path after the intent;
    the moved node goes back and its aside/ is left empty, then removed."""
    from tests.test_scripts.test_live_move_restore_write import _restore_in

    ts = _repaired(w, _foreign_edit)
    session = lambda: (w.root / "sub" / "s1").write_text("a session's\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"intent": session})
    assert rc == 1, out
    assert (w.root / "sub" / "s1").read_text() == "a session's\n"
    assert _runs(w) == [], _runs(w)


def test_a_stopped_run_leaves_no_folder(w):
    """#4364 review round 2 (B3): REPAIR.json cannot be written (the report
    folder read-only), so the run stops; it removes its staging-only folder
    instead of leaving one per rerun, and REPAIR.json is unchanged."""
    ts = _repaired(w, _foreign_edit)
    report = w.backups()[-1]
    before = (report / "REPAIR.json").read_bytes()
    report.chmod(0o500)
    try:
        for _ in range(2):
            assert _restore(w, ts).returncode == 1
    finally:
        report.chmod(0o700)
    assert _runs(w) == [], _runs(w)
    assert (report / "REPAIR.json").read_bytes() == before


def test_an_in_flight_name_the_kernel_rejects_is_a_named_refusal(w):
    """#4364 review round 2 (A): a run name restore_block accepts but lstat
    rejects (ENAMETOOLONG) is named other:aside-unreadable, never a crash."""
    import json

    ts = _repaired(w, _foreign_edit)
    path = w.backups()[-1] / "REPAIR.json"
    report = json.loads(path.read_text())
    long = f"20260101T000000Z-{'1' * 300}-{ts}-live-move-restore"
    report["restore"] = {"inflight": [long, "sub/s1", "node"], "refused": 0, "restored_at": None}
    path.write_text(json.dumps(report))
    for r in (_check(w, ts), _restore(w, ts)):
        assert "Traceback" not in r.stderr, r.stderr
        assert r.returncode == 1 and "aside-unreadable" in r.stdout, r.stdout


def _empty_ignored_dir(w):
    """A repaired `sub` directory holding an ignored, empty sub/e/nested."""
    from tests.test_scripts._live_repair_world import _dir_becomes_file

    ts = _repaired(w, _dir_becomes_file)
    (w.root / ".git" / "info" / "exclude").write_text("e/\n")
    nested = w.root / "sub" / "e" / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o751)
    return ts


def _kept_nested(w):
    """The run folders whose aside still holds sub/e/nested, mode 0751."""
    return [
        run
        for run in _runs(w)
        if (run / "aside" / "sub" / "e" / "nested").is_dir()
        and (run / "aside" / "sub" / "e" / "nested").stat().st_mode & 0o777 == 0o751
    ]


def test_an_empty_directory_inside_a_moved_aside_node_is_kept(w):
    """#4364 review round 3: the cleanup removes only what the restore made;
    an empty directory inside a node it moved aside is the person's, kept in
    the aside with its mode."""
    ts = _empty_ignored_dir(w)
    r = _restore(w, ts)
    assert r.returncode == 0 and _parts(r)[("sub", "node")] == "restored", r.stdout
    assert (w.root / "sub").is_file()
    assert _kept_nested(w), [sorted(p for p in run.rglob("*")) for run in _runs(w)]


def test_a_node_kept_aside_by_a_failed_put_back_keeps_its_empty_directories(
    w, mod, monkeypatch, capsys
):
    """#4364 review round 3: a path re-made after the move fails the put-in and
    the put-back, so the node stays in the aside; its empty directories stay
    with it."""
    from tests.test_scripts.test_live_move_restore_write import _restore_in

    ts = _empty_ignored_dir(w)
    session = lambda: (w.root / "sub").write_text("a session's\n")  # noqa: E731
    rc, out = _restore_in(w, mod, monkeypatch, capsys, ts, at={"aside": session})
    assert rc == 1, out
    assert (w.root / "sub").read_text() == "a session's\n"
    assert _kept_nested(w), [sorted(p for p in run.rglob("*")) for run in _runs(w)]


# ── ignored files kept aside with a tag directory (#4025 item 4) ────────────


def _dir_file_with_cache(w) -> str:
    """`sub` was a foreign file where the tag has sub/; the server then writes
    a cache git ignores into the tag's sub/, so the restore moves the cache
    aside with the directory and puts the file `sub` back: the cache has
    nowhere to go back to."""
    from tests.test_scripts.test_live_move_restore_write import _dir_file, _fresh

    ts, _, _ = _fresh(w, _dir_file)
    (w.root / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    (w.root / "sub" / "__pycache__").mkdir()
    (w.root / "sub" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    return ts


def test_an_ignored_file_kept_aside_is_named_on_every_run(w):
    ts = _dir_file_with_cache(w)
    for _ in range(3):
        r = _restore(w, ts)
        assert r.returncode == 0, r.stdout + r.stderr
        [run] = _runs(w)
        assert r.stdout.count(str(run / "aside" / "sub" / "__pycache__" / "x.pyc")) == 1, r.stdout
        assert "Kept aside with their directory" in r.stdout
        assert "x.pyc" not in _check(w, ts).stdout, "--check names verdicts only"
    assert isinstance(w.report()["restore"]["restored_at"], str), "it never withholds"
    assert (run / "aside" / "sub" / "__pycache__" / "x.pyc").read_bytes() == b"cache"


@pytest.mark.parametrize("step", ["aside", "put-in"])
def test_a_run_killed_before_naming_them_loses_nothing(w, step):
    """#4025 item 4 (probe map): killed at `aside` or at `put-in`, the next
    runs used to name neither the file nor its folder."""
    ts = _dir_file_with_cache(w)
    assert _restore(w, ts, **{KILL_ENV: step}).returncode == -9
    for _ in range(2):
        r = _restore(w, ts)
        assert r.returncode == 0, r.stdout + r.stderr
        [run] = _runs(w)
        assert r.stdout.count(str(run / "aside" / "sub" / "__pycache__" / "x.pyc")) == 1, r.stdout


def test_a_held_aside_is_named_held_not_kept(w):
    ts = _dir_file_with_cache(w)
    assert _restore(w, ts).returncode == 0
    [run] = _runs(w)
    (run / "aside" / "sub" / "notes.txt").write_text("written after the run\n")  # not ignored
    r = _restore(w, ts)
    assert r.returncode == 1 and _parts(r)[(f"{run.name}/aside/sub", "aside")] == (
        "refused:other:aside-held"
    ), r.stdout
    assert "Kept aside with their directory" not in r.stdout, r.stdout


def test_an_ignored_file_is_named_and_an_ignored_empty_directory_kept(w):
    """#4364 round 3 with #4025 item 4: a tag directory moved aside holding an
    ignored file and an ignored empty directory. On every run the file is
    named once and kept, the empty directory survives with its mode, one run
    folder holds both, and --check names nothing."""
    from tests.test_scripts.test_live_move_restore_write import _dir_file, _fresh

    ts, _, _ = _fresh(w, _dir_file)
    (w.root / ".git" / "info" / "exclude").write_text("__pycache__/\ne/\n")
    (w.root / "sub" / "__pycache__").mkdir()
    (w.root / "sub" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    nested = w.root / "sub" / "e" / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o751)
    for _ in range(3):
        r = _restore(w, ts)
        assert r.returncode == 0, r.stdout + r.stderr
        [run] = _runs(w)
        pyc = run / "aside" / "sub" / "__pycache__" / "x.pyc"
        assert r.stdout.count(str(pyc)) == 1, r.stdout
        assert pyc.read_bytes() == b"cache"
        kept = run / "aside" / "sub" / "e" / "nested"
        assert kept.is_dir() and kept.stat().st_mode & 0o777 == 0o751, sorted(run.rglob("*"))
    assert "x.pyc" not in _check(w, ts).stdout
    assert isinstance(w.report()["restore"]["restored_at"], str)


@pytest.mark.parametrize(
    ("q", "covered"),
    [
        ("sub/s1", True),  # the held path
        ("sub", True),  # above it
        ("sub/s1/inner", True),  # below it
        ("sub/t", False),  # a sibling
        ("sub/s10", False),  # a name sharing its prefix
        ("", False),  # nothing named: the whole run is not stopped
    ],
)
def test_a_hold_covers_its_path_above_and_below_only(q, covered):
    from tests.test_scripts.test_live_move_restore_write import _kit

    kit = _kit()
    hold = ["20260101T000000Z-9-20260101T000000Z-1-live-move-restore", "sub/s1", "node"]
    assert kit.held_covers([hold], q) is covered
    old = ["20260101T000000Z-9-live-move-restore", "sub/s1", "node"]  # before #4011
    assert kit.held_covers([hold, old], q) is True, "an old-form hold stops every part"
    assert kit.held_stops_all([old]) and not kit.held_stops_all([hold])
    assert kit.held_covers([], q) is False


def test_an_aside_it_cannot_read_still_holds_its_path(w):
    """PR3 port review: an aside the restore cannot read holds its path like a
    held one. Here the in-flight slot is empty (the last run finished), the
    aside under sub/ was written after its run and then made unsearchable,
    and the checkout's sub/s1 is as the repair left it: nothing is written
    there, the report stays unresolved, and --check agrees."""
    import subprocess

    ts = _repaired(w, _foreign_edit)
    fd = os.open(w.root / "sub" / "s1", os.O_WRONLY)
    try:
        assert _restore(w, ts).returncode == 0
        os.pwrite(fd, b"newer\n", 0)
    finally:
        os.close(fd)
    [run] = _runs(w)
    assert w.report()["restore"]["inflight"] is None
    subprocess.run(["git", "-C", str(w.root), "checkout", "--", "sub/s1"], check=True)
    left = (w.root / "sub" / "s1").read_bytes()
    (run / "aside" / "sub").chmod(0)
    try:
        c, r = _check(w, ts), _restore(w, ts)
        for out in (c, r):
            assert out.returncode == 1, out.stdout
            assert _parts(out)[("sub/s1", "node")].endswith("other:inflight-unresolved"), out.stdout
        assert (w.root / "sub" / "s1").read_bytes() == left
        assert w.report()["restore"]["restored_at"] is None
    finally:
        (run / "aside" / "sub").chmod(0o700)
