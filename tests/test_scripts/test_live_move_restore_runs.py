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
