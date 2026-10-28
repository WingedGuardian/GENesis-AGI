"""The live-move repair's READERS (#3492 PR D1, readers split out of #3648):
deploy_health.collect_live_repairs / live_repair_findings / _reapplied, the
guardian collector's report pick (#3627), and the derive_findings wiring.

A row reads re-applied exactly when running its printed re-apply step would
change nothing, so every row kind the writer emits is reported after the
repair and cleared by its own printed steps, run through bash. The scratch
world is tests/test_scripts/_live_repair_world.py; the `w` and `mod` fixtures
come from this directory's conftest.py.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    MODULE,
    Killed,
    _ancestor_file,
    _deep_desc,
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
    _gitlink_desc,
    _hash,
    _killed_mid_repair,
    _lock_aside,
    _lock_aside_if_any,
    _retarget,
    _run_steps,
    _skip_worktree_desc,
    _staged_desc,
    killed_tag_side,
    tag_snapshot,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")

LATER = 2 * 3600  # past REPAIR_UNFINISHED_S: a report with no result is a killed run


def _dh():
    return importlib.import_module("genesis.observability.snapshots.deploy_health")


def _saved(w) -> list[str]:
    return _dh().collect_live_repairs(w.gen)["saved"]


def _moved_on(w) -> list[str]:
    """The informational lines: rows no longer offered (the checkout moved on)."""
    return _dh().collect_live_repairs(w.gen)["moved_on"]


def _cleared(w) -> bool:
    """Re-applied: neither offered nor read as moved on (both leave `saved` empty)."""
    found = _dh().collect_live_repairs(w.gen)
    return found["saved"] == [] and found["moved_on"] == []


def _dismiss(w, ts: str) -> None:
    d = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert d.returncode == 0, d.stderr


def _reported_then_cleared_by_its_steps(w) -> None:
    """Every foreign row is reported with its own step after the repair, and the
    printed steps, run through bash, clear every one of them."""
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    steps = w.report()["reapply"]
    assert steps, "control: the repair found nothing foreign"
    assert _saved(w) == steps, "a foreign row was not reported before its step ran"
    _run_steps(w)
    found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == [], "re-applied, still reported"
    assert found["moved_on"] == [], "re-applied, read as moved on"


# ── every row kind the writer emits ─────────────────────────────────────────


@pytest.mark.parametrize(
    "make",
    [
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
    ],
)
def test_td3_a_foreign_row_is_reported_until_its_step_ran(w, make):
    """The td3 kinds (the reader half of the writer's T-D3)."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: make(w))
    _reported_then_cleared_by_its_steps(w)


@pytest.mark.parametrize(
    "make", [_staged_desc, _skip_worktree_desc, _deep_desc, _gitlink_desc, _ancestor_file]
)
def test_r4_an_index_entry_under_a_row_path_is_reported_until_reapplied(w, make):
    """Index-descendant rows (one per index entry under a directory row, their
    saved_as inside the parent's whole-directory copy) and an index-ancestor
    file `sub` above sub/s1."""
    killed_tag_side(w, "prepared")
    _lock_aside_if_any(w, lambda: make(w))
    _reported_then_cleared_by_its_steps(w)


def test_r5_an_index_ancestor_row_is_reported_until_reapplied(w):
    """#3680: a staged file `sub` above sub/s1, the disk holding sub/."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _ancestor_file(w))
    _reported_then_cleared_by_its_steps(w)
    assert "sub" in {r["path"] for r in w.report()["paths"] if r["class"] == "foreign"}


def _unmerged_ancestor(w):
    oids = [_hash(w, f"UNMERGED {n}\n") for n in (1, 2, 3)]
    recs = "".join(f"100644 {o} {n}\tsub\0" for n, o in zip((1, 2, 3), oids, strict=True))
    subprocess.run(
        ["git", "-C", str(w.root), "update-index", "-z", "--index-info"],
        input=recs.encode(),
        check=True,
        env=_git_env(),
    )


def test_an_unmerged_ancestor_row_is_reported_only_while_its_step_would_act(w):
    """Stages 1-3 of an ancestor `sub` beside a stage-0 sub/s1: the restore
    keeps them (MEASURED, git 2.43), so the row's step would change nothing
    and it is not reported; once they are gone it is, until the step runs."""
    killed_tag_side(w, "prepared")
    _lock_aside_if_any(w, lambda: _unmerged_ancestor(w))
    assert w.repair().returncode == 0
    [step] = w.report()["reapply"]
    assert _cleared(w)
    _git(w.root, "update-index", "--force-remove", "--", "sub")
    assert _saved(w) == [step]
    _run_steps(w)
    assert _cleared(w)
    assert "UU sub" in _git(w.root, "status", "--porcelain")


@pytest.mark.parametrize("ancestor", [False, True], ids=["staged-child", "ancestor-file"])
def test_r5_an_unreachable_row_is_judged_by_its_index_only(w, tmp_path, ancestor):
    """#3680: a row seen beyond a symlinked parent gets its index state only; the
    reader never compares its disk part through the link (the outside file
    differs from the saved bytes), and the printed steps clear it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s1").write_text("not the checkout's\n")
    killed_tag_side(w, "Z3")

    def plant():
        if ancestor:
            _ancestor_file(w)
        else:
            (w.root / "sub" / "s1").write_text("STAGED S1\n")
            _git(w.root, "add", "--", "sub/s1")
        shutil.rmtree(w.root / "sub")
        os.symlink(outside, w.root / "sub")

    _lock_aside(w, plant)
    if not ancestor:
        r = w.repair()
        assert r.returncode == 0, r.stdout + r.stderr
        row = {r["path"]: r for r in w.report()["paths"]}["sub/s1"]
        assert row["unreachable_through"] == "sub" and row["disk_changed"], row
        assert len(_saved(w)) == len(w.report()["reapply"])
        _run_steps(w)
        assert _saved(w) == [], "an index-only row was judged through the link"
        assert _moved_on(w) == [], "an index-only row was judged through the link"
    else:
        _reported_then_cleared_by_its_steps(w)
    assert (outside / "s1").read_text() == "not the checkout's\n"


def test_r1_a_fifo_row_is_reported_until_its_step_makes_it_again(w):
    killed_tag_side(w, "Z3")
    os.mkfifo(w.root / "Z5", 0o640)
    _reported_then_cleared_by_its_steps(w)


def test_r1_a_fifo_back_with_another_mode_is_not_reapplied(w):
    """Neither the saved FIFO (its mode differs) nor what the repair left (nothing):
    not re-applied, and no longer offered."""
    killed_tag_side(w, "Z3")
    os.mkfifo(w.root / "Z5", 0o640)
    assert w.repair().returncode == 0
    os.mkfifo(w.root / "Z5", 0o600)
    assert _saved(w) == []
    assert len(_moved_on(w)) == 1


def test_r1_a_socket_row_clears_only_once_a_socket_is_back(w, monkeypatch):
    """A socket holds nothing to save: its step removes the path and says so.
    The row is reported until whatever made the socket binds it again."""
    killed_tag_side(w, "Z3")
    monkeypatch.chdir(w.root)  # a unix socket path is capped near 108 bytes
    with socket.socket(socket.AF_UNIX) as s:
        s.bind("Z5")
        r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    [step] = w.report()["reapply"]
    assert _saved(w) == [step]
    _run_steps(w)
    assert _saved(w) == [step], "nothing re-made the socket"
    with socket.socket(socket.AF_UNIX) as s:
        s.bind("Z5")
        assert _cleared(w)


def test_never_compares_through_a_symlinked_parent(w, tmp_path):
    """A parent that became a symlink after the repair: the saved bytes, found
    outside the checkout through it, are not the row re-applied."""
    killed_tag_side(w, "Z3")
    (w.root / "sub" / "s1").write_text("mine\n")
    assert w.repair().returncode == 0
    [step] = w.report()["reapply"]
    outside = tmp_path / "outside"
    shutil.copytree(w.root / "sub", outside)
    (outside / "s1").write_text("mine\n")
    shutil.rmtree(w.root / "sub")
    os.symlink(outside, w.root / "sub")
    assert step not in _saved(w), "a removing step offered through a symlinked parent"
    assert len(_moved_on(w)) == 1


def test_rows_pair_with_their_steps_in_the_writers_order(tmp_path):
    """The writer lists rows by path and steps parents first (by path
    components): `a-b` sorts before `a/b` as a string, after it by components."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").mkdir()
    (repo / "a" / "b").write_text("back\n")  # the tag's: what the repair left, offered
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "t")
    blob = ["100644", _git(repo, "rev-parse", "HEAD:a/b")]
    d = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair"
    d.mkdir(parents=True)
    row = {"class": "foreign", "disk": "absent", "disk_changed": True, "tag": None}
    row.update(other=None, index=[], flag=None, special=None, saved_as=None)
    rows = [{**row, "path": "a-b"}, {**row, "path": "a/b", "tag": blob, "index": [[*blob, "0"]]}]
    report = {"result": "healed", "repo": str(repo), "paths": rows, "reapply": ["A/B", "A-B"]}
    report["tag"] = _git(repo, "rev-parse", "HEAD")
    (d / "REPAIR.json").write_text(json.dumps(report))
    [step] = _dh().collect_live_repairs(tmp_path)["saved"]
    assert step == f"rm -rf -- {repo}/a/b", step  # a/b's own step, regenerated


# ── what the snapshot reports ───────────────────────────────────────────────


def _guardian_pick(home: Path) -> dict | None:
    """The host guardian's container-side report pick (#3627), run against this HOME."""
    from genesis.guardian import collector as gc

    out = subprocess.run(
        [sys.executable, "-I", "-S", "-"],
        input=gc._REPAIR_PICK,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=60,
    ).stdout
    return json.loads(out.split(gc._REPAIR_MARK.encode(), 1)[1]) if out else None


def test_the_guardian_picks_what_this_writer_left_eligible(w, mod, monkeypatch):
    """#3627: the guardian's pick reads the fields THIS writer writes. A run
    killed partway (result unset) is eligible until the next run marks it
    superseded; the healed report is then the one shown, until --dismiss."""
    killed_tag_side(w, "Z3")
    real = mod._git

    def die_at_restore(repo, *args, **kw):
        if args[:1] == ("restore",):
            raise Killed
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", die_at_restore)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    killed = _guardian_pick(w.home)
    assert killed is not None and killed["result"] is None, killed
    assert w.repair(mode="--boot").returncode == 0
    healed = _guardian_pick(w.home)
    assert healed is not None and healed["result"] == "healed", healed
    _dismiss(w, healed["ts"])
    assert _guardian_pick(w.home) is None  # one superseded, one dismissed: nothing to show


def test_td19_a_step_is_offered_only_while_the_path_is_as_the_repair_left_it(w):
    """F1: re-applied clears; an edit made AFTER re-applying is someone's newer
    content, which the step (remove, then the old copy) would destroy: never
    offered again, only an informational line naming the backup, until dismissed."""
    dh = _dh()
    killed_tag_side(w, "Z3")
    (w.root / "sub" / "s1").write_text("mine\n")
    assert w.repair().returncode == 0
    found = dh.collect_live_repairs(w.gen)
    assert dh.live_repair_findings(found) == ["live_repair_saved:1"], found
    assert found["saved"] == w.report()["reapply"] and found["moved_on"] == []
    _run_steps(w)
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen)) == []
    (w.root / "sub" / "s1").write_text("mine v2, a later edit\n")
    found = dh.collect_live_repairs(w.gen)
    assert found["saved"] == [], "a step offered over content the repair never put there"
    assert dh.live_repair_findings(found) == ["live_repair_moved_on:1"], found
    [line] = found["moved_on"]
    ts = w.report()["ts"]
    assert ts in line and str(w.backups()[-1]) in line and "1 saved path(s)" in line, line
    _dismiss(w, ts)
    assert w.report()["dismissed_at"]
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen)) == []


def test_f1_a_checkout_moved_off_the_tag_is_not_offered_a_step(w):
    """F1: the next update completed (HEAD left the repair's tag): the old row's
    step would put the old edit over the new release. Not offered; one line."""
    killed_tag_side(w, "Z3")
    (w.root / "sub" / "s1").write_text("mine\n")
    assert w.repair().returncode == 0
    assert len(_saved(w)) == 1, "control: an untouched row is offered"
    _git(w.root, "switch", "-q", "-C", "live", w.to)
    found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == []
    [line] = found["moved_on"]
    assert w.report()["ts"] in line and str(w.backups()[-1]) in line, line


def test_f1_a_head_moved_off_the_tag_withholds_even_an_untouched_path(w):
    """HEAD alone decides too: the path still holds the tag's bytes, but the
    checkout is on another commit, so no step is offered."""
    killed_tag_side(w, "Z3")
    (w.root / "sub" / "s1").write_text("mine\n")
    assert w.repair().returncode == 0
    _git(w.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "moved on")  # fmt: skip
    assert _saved(w) == [] and len(_moved_on(w)) == 1


def test_f1_a_partly_run_step_list_still_offers_the_rest(w):
    """Steps run one at a time, parents first: a row whose path holds the saved
    state already (its parent's step put it back) still offers its own step,
    which re-writes the same bytes and its index part: nothing is lost."""
    killed_tag_side(w, "prepared")
    _lock_aside_if_any(w, lambda: _staged_desc(w))
    assert w.repair().returncode == 0
    steps = w.report()["reapply"]
    assert len(steps) >= 2 and _saved(w) == steps
    r = subprocess.run(["bash", "-c", steps[0]], capture_output=True, text=True, env=w.env())
    assert r.returncode == 0, r.stderr
    assert _saved(w) == steps[1:]
    assert _moved_on(w) == []


# -- what the reader must tell apart (mutation survivors of the readers audit) --


def test_m1_a_flag_only_row_is_offered_until_its_flag_is_back(w):
    """A skip-worktree flag on a move path still at the tag's bytes: a flag-only row."""
    killed_tag_side(w, "Z3")  # sub/s1 is still the tag's bytes
    _lock_aside(w, lambda: _git(w.root, "update-index", "--skip-worktree", "--", "sub/s1"))
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    row = {r["path"]: r for r in w.report()["paths"]}["sub/s1"]
    assert row["flag"] == "S" and not row["disk_changed"], row
    assert _saved(w) == w.report()["reapply"]
    _run_steps(w)
    assert _cleared(w)


def test_m2_an_extra_flag_after_an_index_write_is_not_reapplied(w):
    """The step re-adds the index entry (which resets flags): a flag set since is
    neither the step's result nor the repair's leftover."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_staged(w))
    _reported_then_cleared_by_its_steps(w)
    _git(w.root, "update-index", "--skip-worktree", "--", "A")
    assert _saved(w) == [] and len(_moved_on(w)) == 1


def test_m6_an_extra_entry_in_a_saved_directory_is_not_reapplied(w):
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_inside_dir(w))
    _reported_then_cleared_by_its_steps(w)
    (w.root / "P" / "newer.txt").write_text("written after the re-apply\n")
    assert _saved(w) == [] and len(_moved_on(w)) == 1


def test_m10_a_socket_inside_a_saved_directory_is_ignored(w, monkeypatch):
    """A socket (or device) holds nothing the repair could save: its presence in
    a re-applied directory does not make the directory differ."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_inside_dir(w))
    _reported_then_cleared_by_its_steps(w)
    monkeypatch.chdir(w.root / "P")
    with socket.socket(socket.AF_UNIX) as s:
        s.bind("sock")
        found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == [] and found["moved_on"] == [], found


def test_m11_a_socket_row_is_not_reapplied_by_a_node_of_another_type(w, monkeypatch):
    killed_tag_side(w, "Z3")
    monkeypatch.chdir(w.root)
    with socket.socket(socket.AF_UNIX) as s:
        s.bind("Z5")
        assert w.repair().returncode == 0
    os.mkfifo(w.root / "Z5")
    found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == [] and len(found["moved_on"]) == 1, found


# -- an untrusted report: its paths, its cost --


def _row(path: str, **over) -> dict:
    row = {"path": path, "class": "foreign", "disk": "100644", "disk_changed": True}
    row.update(tag=None, other=None, index=[], flag=None, special=None, saved_as=None)
    return {**row, **over}


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "kept").write_text("kept\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "t")
    return repo, _git(repo, "rev-parse", "HEAD")


def _collect_bounded(home: Path, seconds: float = 10.0) -> dict:
    """collect_live_repairs on a thread, failing (not hanging) past ``seconds``."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(_dh().collect_live_repairs(home)), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"collect_live_repairs still running after {seconds}s"
    return out


@pytest.mark.parametrize(
    "over",
    [
        {"path": "/x"},
        {"path": "../x"},
        {"path": "a/../../x"},
        {"path": "a//b"},
        {"path": "./a"},
        {"path": "a\0b"},
        {"saved_as": "/etc/passwd"},
        {"saved_as": "../../x"},
        {"saved_as": "tree/../../x"},
    ],
    ids=[
        "abs",
        "dotdot",
        "inner-dotdot",
        "empty-part",
        "dot",
        "nul",
        "saved-abs",
        "saved-dotdot",
        "saved-inner-dotdot",
    ],
)
def test_f2_an_unsafe_row_path_is_skipped_promptly(tmp_path, over):
    """F2/F4: REPAIR.json is user-writable, so untrusted. An absolute path sent
    the writer's _parents() into an unbounded loop; a saved_as outside the
    backup read any file. Each is skipped as malformed, fast, never raised."""
    repo, head = _git_repo(tmp_path)
    (repo / "Z").write_text("not the saved bytes\n")
    (tmp_path / "x").write_text("outside\n")
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[_row(**{"path": "Z", **over})], reapply=["the step"])  # fmt: skip
    found = _collect_bounded(tmp_path)
    assert found["saved"] == [] and found["moved_on"] == [], found
    assert found["malformed"] == 1, found


def test_f3_a_large_file_is_compared_without_reading_it_whole(tmp_path):
    """F3: a saved file and the checkout's are compared by type and size, then
    in bounded chunks: never read whole into memory, on every snapshot."""
    repo, head = _git_repo(tmp_path)
    size = 32 * 1024 * 1024
    d = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair"
    (d / "tree").mkdir(parents=True)
    for f in (repo / "big", d / "tree" / "big"):
        with open(f, "wb") as fh:
            fh.truncate(size)  # sparse: no disk used, reads as zeros
    report = {"result": "healed", "repo": str(repo), "tag": head, "reapply": ["s"]}
    report["paths"] = [_row("big", saved_as="tree/big")]
    (d / "REPAIR.json").write_text(json.dumps(report))
    tracemalloc.start()
    try:
        found = _dh().collect_live_repairs(tmp_path)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found["saved"] == [] and found["moved_on"] == [], found  # re-applied
    assert peak < size // 4, f"peak {peak} bytes for a {size}-byte file"


def _spy_index_reads(monkeypatch) -> list[str]:
    dh = _dh()
    reads: list[str] = []
    real = dh._script

    def script(path):
        module = real(path)
        if hasattr(module, "_index_state"):
            inner = module._index_state

            def counted(repo):
                reads.append(repo)
                return inner(repo)

            module._index_state = counted
        return module

    monkeypatch.setattr(dh, "_script", script)
    return reads


def test_f3_the_index_is_read_once_per_repo_per_snapshot(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    reads = _spy_index_reads(monkeypatch)
    for n in (1, 2):
        _report_at(tmp_path, f"20260101T00000{n}Z-{n}", result="healed", repo=str(repo),
                   tag=head, paths=[_row(f"a{n}"), _row(f"b{n}")],
                   reapply=[f"s{n}a", f"s{n}b"])  # fmt: skip
    _dh().collect_live_repairs(tmp_path)
    assert reads == [str(repo)], reads


def test_f3_a_gone_repo_costs_no_git_call_per_row(tmp_path, monkeypatch):
    reads = _spy_index_reads(monkeypatch)
    gone = tmp_path / "no-such-repo"
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(gone), tag="0" * 40,
               paths=[_row("a"), _row("b"), _row("c")], reapply=["sa", "sb", "sc"])  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path)
    assert len(reads) <= 1, reads
    assert found["saved"] == [] and len(found["moved_on"]) == 1, found


def test_r2_a_mode_only_edit_is_not_reapplied_until_its_mode_is_back(w):
    """Codex c4385987787: the reader compares what the writer compares, the
    executable bit (the one permission git tracks) as well as the bytes."""
    killed_tag_side(w, "Z3")  # sub/s1 is still the tag's bytes
    (w.root / "sub" / "s1").chmod(0o755)
    assert w.repair().returncode == 0
    assert not os.access(w.root / "sub" / "s1", os.X_OK)
    [step] = w.report()["reapply"]
    assert _saved(w) == [step]
    _run_steps(w)
    assert os.access(w.root / "sub" / "s1", os.X_OK)
    assert _cleared(w)


def test_td19_an_unfinished_repair_is_unresolved_while_the_tree_is_mixed(w, mod, monkeypatch):
    """The reader derives "unresolved" from the guard at read time: a report
    that never finished, an hour old, counts only while the shared guard still
    calls the checkout half-written; a later heal clears it."""
    dh = _dh()
    killed_tag_side(w, "Z3")
    _killed_mid_repair(w, mod, monkeypatch, "at_apply")
    later = time.time() + LATER
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen, now=time.time())) == []
    found = dh.collect_live_repairs(w.gen, now=later)
    assert dh.live_repair_findings(found) == ["live_repair_unresolved"]
    findings = dh.derive_findings(
        missing_units=[], tier2_pending=[], host_gateway={}, commits_behind=0, live_repairs=found
    )
    assert findings == ["live_repair_unresolved"]
    assert w.repair().returncode == 0
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen, now=later)) == []


@pytest.mark.parametrize("window", ["before_clear", "before_finish"])
def test_r1_an_interrupted_report_is_not_unresolved_once_healed(w, mod, monkeypatch, window):
    """The reader half of the writer's r1 test: killed after its restore, before
    its result was written, the next run heals; nothing reads unresolved."""
    killed_tag_side(w, "Z3")

    def die(self, *a, **kw):
        raise Killed

    monkeypatch.setattr(mod.Repair, "_after" if window == "before_clear" else "_finish", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    assert w.repair().returncode == 0 and not w.state.exists()
    dh = _dh()
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen, now=time.time() + LATER)) == []


def test_r2_a_killed_repair_on_a_tag_directory_reads_resolved(w, mod, monkeypatch):
    """Audit S2, the reader half: killed after its restore on a path that is a
    directory in the tag, then healed by the next run: nothing unresolved."""
    _retarget(w, _dir_becomes_file)
    killed_tag_side(w, "prepared")
    _killed_mid_repair(w, mod, monkeypatch, "after_apply")
    assert w.snapshot() == tag_snapshot() and w.state.exists()
    assert w.repair().returncode == 0 and not w.state.exists()
    assert _dh().collect_live_repairs(w.gen, now=time.time() + LATER)["unresolved"] is False


# ── loading only what a report needs ────────────────────────────────────────


def _report_at(home: Path, name: str, **report) -> None:
    d = home / "deploy-backups" / f"{name}-live-move-repair"
    d.mkdir(parents=True)
    (d / "REPAIR.json").write_text(json.dumps(report))


def _spy_scripts(monkeypatch) -> list[str]:
    dh = _dh()
    loaded: list[str] = []
    real = dh._script

    def script(path):
        loaded.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(dh, "_script", script)
    return loaded


def test_the_repair_module_loads_only_for_an_undismissed_foreign_row(tmp_path, monkeypatch):
    loaded = _spy_scripts(monkeypatch)
    engine = {"path": "A", "class": "engine", "disk": "absent", "disk_changed": True}
    foreign = {**engine, "class": "foreign", "index": [], "tag": None, "flag": None}
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=[engine])
    _report_at(tmp_path, "20260101T000001Z-2", result="healed", repo=str(tmp_path),
               paths=[foreign], reapply=["step"], dismissed_at="2026-01-01T00:00:00Z")  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path)
    assert (found["saved"], found["unresolved"]) == ([], False), found
    assert loaded == [], "a module loaded with no undismissed foreign row"
    _report_at(tmp_path, "20260101T000002Z-3", result="healed", repo=str(tmp_path),
               paths=[foreign], reapply=["step"])  # fmt: skip
    _dh().collect_live_repairs(tmp_path)
    assert loaded == ["live_move_repair.py"], loaded


def test_the_guard_runs_only_when_a_report_reads_unresolved(tmp_path, monkeypatch):
    loaded = _spy_scripts(monkeypatch)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=[])
    assert _dh().collect_live_repairs(tmp_path)["unresolved"] is False
    assert loaded == []
    _report_at(tmp_path, "20260101T000001Z-2", result="unresolved: x", repo=str(tmp_path))
    _dh().collect_live_repairs(tmp_path)
    assert loaded == ["update_state_guard.py"], loaded


# ── a malformed report never blanks the snapshot ────────────────────────────


@pytest.mark.parametrize(
    "paths",
    [
        ["oops"],
        "x",
        [{"path": ["a"], "class": "foreign"}],
        [{"path": "p", "class": "foreign"}, 7],
        [{"path": "p", "class": "foreign", "disk_changed": True, "saved_as": 5, "index": "x"}],
    ],
)
def test_sf3_a_malformed_report_never_blanks_the_snapshot(tmp_path, paths):
    """SF3: a REPAIR.json of the wrong shape is skipped row by row, never raised
    (an exception there blanks the whole deploy-health snapshot)."""
    dh = _dh()
    report = {"result": "healed", "paths": paths, "reapply": ["step", 5], "repo": ["r"]}
    _report_at(tmp_path, "20260101T000000Z-1", **report)
    found = dh.collect_live_repairs(tmp_path)
    assert isinstance(found["saved"], list)
    assert all(isinstance(s, str) for s in found["saved"])


def test_sf3_a_non_list_reapply_counts_as_none(tmp_path):
    report = {"result": "healed", "paths": [{"path": "p", "class": "foreign"}], "reapply": "s"}
    _report_at(tmp_path, "20260101T000000Z-1", **report)
    assert _dh().collect_live_repairs(tmp_path)["saved"] == []


def test_f2_a_saved_as_through_a_symlink_in_the_backup_is_skipped(tmp_path):
    """F4: `saved_as` is resolved, not only parsed: a `tree` symlinked out of the
    backup dir reads nothing outside it."""
    repo, head = _git_repo(tmp_path)
    (repo / "Z").write_text("same\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Z").write_text("same\n")
    d = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair"
    d.mkdir(parents=True)
    os.symlink(outside, d / "tree")
    report = {"result": "healed", "repo": str(repo), "tag": head, "reapply": ["s"]}
    report["paths"] = [_row("Z", saved_as="tree/Z")]
    (d / "REPAIR.json").write_text(json.dumps(report))
    assert _collect_bounded(tmp_path)["malformed"] == 1


def test_f1_a_parent_is_withheld_while_a_row_below_it_moved_on(tmp_path):
    """A parent's step drops the index entries of the rows below it before it
    re-adds its own: while one of those holds someone's newer staging, the
    parent's step would drop it, so it is withheld too."""
    repo, _ = _git_repo(tmp_path)
    (repo / "a").mkdir()
    (repo / "a" / "b").write_text("tag\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
    head = _git(repo, "rev-parse", "HEAD")
    tag_b = ["100644", _git(repo, "rev-parse", "HEAD:a/b")]
    saved = _git(repo, "hash-object", "-w", "--", "kept")
    (repo / "a" / "b").write_text("newer staging\n")
    _git(repo, "add", "a/b")  # neither the tag's entry nor the saved one
    (repo / "a" / "b").write_text("tag\n")
    parent = _row("a", disk_changed=False, index=[["100644", saved, "0"]])
    child = _row("a/b", disk_changed=False, tag=tag_b, index=[["100644", saved, "0"]])
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[parent, child], reapply=["parent step", "child step"])  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path)
    assert found["saved"] == [] and found["moved_on_paths"] == 2, found
    _git(repo, "reset", "-q", "--", "a/b")  # the child back as the repair left it
    parent_step, child_step = _dh().collect_live_repairs(tmp_path)["saved"]
    assert parent_step.startswith("printf") and "\ta/b" in parent_step  # drops a/b first
    assert "\ta/b" in child_step and "\ta'" not in child_step


# -- the offered step is regenerated, never read from REPAIR.json --


def _tamper(w, **fields) -> None:
    path = w.backups()[-1] / "REPAIR.json"
    report = json.loads(path.read_text())
    report.update(fields)
    path.write_text(json.dumps(report))


@pytest.mark.parametrize("make", [_foreign_edit, _foreign_inside_dir, _foreign_staged])
def test_a_tampered_stored_step_is_never_shown(w, tmp_path, make):
    """REPAIR.json is user-writable: the reader regenerates each offered step
    with the repair's own reapply_step from the row, and never shows the
    stored text (here a command that would run anything)."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: make(w))
    assert w.repair().returncode == 0
    written = w.report()["reapply"]
    pwned = tmp_path / "pwned"
    _tamper(w, reapply=[f"touch {pwned}"] * len(written))
    saved = _saved(w)
    assert saved == written, "the regenerated steps differ from what the repair wrote"
    assert not any(str(pwned) in step for step in saved), saved


def test_steps_are_offered_even_when_the_stored_list_is_gone(w):
    """Parity, with nothing stored to lean on: the reader's steps equal the
    writer's for every row (parents first, each with the writer's `below`)."""
    killed_tag_side(w, "prepared")
    _lock_aside_if_any(w, lambda: _staged_desc(w))
    assert w.repair().returncode == 0
    written = w.report()["reapply"]
    assert len(written) >= 2
    _tamper(w, reapply=[])
    assert _saved(w) == written


@pytest.mark.parametrize(
    "over",
    [
        {"special": {"type": "fifo", "mode": "0640; touch x"}, "disk": "other"},
        {"blobs": ["../../x"], "index": [["100644", "a" * 40, "0"]]},
        {"blobs": ["/etc/passwd"], "index": [["100644", "a" * 40, "0"]]},
        {"flag": "x; touch y"},
        {"index": [["100644", "not-an-oid", "0"]]},
        {"index": [["100644", "a" * 40, "7"]]},
        {"tag": ["100644", "zz"]},
        {"unreachable_through": "../up"},
    ],
    ids=["fifo-mode", "blob-dotdot", "blob-abs", "flag", "oid", "stage", "tag", "through"],
)
def test_a_row_field_a_step_would_interpolate_is_validated(tmp_path, over):
    """Fields reapply_step builds the step from are checked before it is built:
    one that is not what the writer writes skips the row as malformed."""
    repo, head = _git_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[_row(**{"path": "Z", "disk": "absent", **over})], reapply=[])  # fmt: skip
    found = _collect_bounded(tmp_path)
    assert found["saved"] == [] and found["malformed"] == 1, found


# -- every index entry a step drops is checked before it is offered --


def _staged_at(w, path: str) -> str:
    return _git(w.root, "ls-files", "-s", "--", path)


def test_p1_an_engine_row_below_holding_newer_staging_withholds_its_parent(w):
    """Codex c4399950919: the parent `sub`'s step drops sub/t (an engine row the
    repair put back at the tag) before re-adding `sub`. Once someone stages
    sub/t again, running the parent's step would drop that staging and no step
    puts it back: the parent is withheld, the rest still offered."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _ancestor_file(w))
    assert w.repair().returncode == 0
    rows = {r["path"]: r["class"] for r in w.report()["paths"]}
    assert (rows["sub"], rows["sub/t"]) == ("foreign", "engine"), rows
    parent_step, child_step = w.report()["reapply"]
    assert _saved(w) == [parent_step, child_step], "control: an untouched repair offers both"
    oid = _hash(w, "NEWER T\n")
    _git(w.root, "update-index", "--cacheinfo", f"100644,{oid},sub/t")
    found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == [child_step], found
    assert found["moved_on_paths"] == 1, found
    for step in found["saved"]:
        subprocess.run(["bash", "-c", step], check=True, env=w.env())
    assert oid in _staged_at(w, "sub/t"), "an offered step dropped someone's staging"


def _parent_and_child(tmp_path: Path, child_flag: str | None) -> tuple[Path, str]:
    """A foreign parent `a` (a staged file where the tag has a/) and a foreign
    child a/b below it, the checkout as the repair left both."""
    repo, _ = _git_repo(tmp_path)
    (repo / "a").mkdir()
    (repo / "a" / "b").write_text("tag\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
    head = _git(repo, "rev-parse", "HEAD")
    tag_b = ["100644", _git(repo, "rev-parse", "HEAD:a/b")]
    saved = _git(repo, "hash-object", "-w", "--", "kept")
    parent = _row("a", disk_changed=False, index=[["100644", saved, "0"]])
    child = _row("a/b", disk_changed=False, tag=tag_b, index=[["100644", saved, "0"]])
    child["flag"] = child_flag
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[parent, child], reapply=["parent step", "child step"])  # fmt: skip
    return repo, head


@pytest.mark.parametrize("staged", [False, True], ids=["untouched", "newer-staging"])
def test_p1_a_malformed_row_below_withholds_its_parent(tmp_path, staged):
    """Devin c4399914277: a child row skipped as malformed (here its flag) is
    still in the parent's drop list. It cannot be judged, so the parent is
    never offered: with newer staging at a/b, the step would drop it."""
    repo, _ = _parent_and_child(tmp_path, child_flag="1")
    if staged:
        (repo / "a" / "b").write_text("newer staging\n")
        _git(repo, "add", "a/b")
        (repo / "a" / "b").write_text("tag\n")
    found = _dh().collect_live_repairs(tmp_path)
    assert found["saved"] == [], found
    assert found["malformed"] == 1 and found["moved_on_paths"] == 1, found


def test_p1_a_well_formed_child_as_left_still_offers_its_parent(tmp_path):
    """The control for the two above: nothing written since, both offered."""
    _parent_and_child(tmp_path, child_flag=None)
    parent_step, child_step = _dh().collect_live_repairs(tmp_path)["saved"]
    assert "\ta/b" in parent_step and "\ta/b" in child_step


# -- a repair module that cannot load never blanks the snapshot --


def test_an_unloadable_repair_module_keeps_every_other_finding(tmp_path, monkeypatch):
    """Devin c4399914406 / CodeRabbit c4399949029 / Codex c4399950954: with
    the repair's module unreadable, no helper of it is called; the foreign row
    reads moved on and deploy_health keeps its other findings."""
    import asyncio

    import genesis.env

    dh = _dh()
    repo, head = _git_repo(tmp_path)
    home = tmp_path / "gh"
    _report_at(home, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[_row("Z")], reapply=["s"])  # fmt: skip

    def unreadable(path):
        raise PermissionError(f"{path}: denied")

    monkeypatch.setattr(dh, "_script", unreadable)
    found = dh.collect_live_repairs(home)
    assert found["saved"] == [] and found["moved_on_paths"] == 1, found
    monkeypatch.setattr(genesis.env, "genesis_home", lambda: home)
    monkeypatch.setattr(genesis.env, "repo_root", lambda: repo)
    snap = asyncio.run(dh.deploy_health(None))
    assert snap["status"] != "error", snap
    assert "live_repair_moved_on:1" in snap["findings"], snap["findings"]


# -- a failed open never leaks a descriptor --


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_checkout_file_leaks_no_descriptor(tmp_path):
    """Devin c4399914732 / Codex c4399950945: the saved copy opens, the
    checkout's file does not; the saved copy's descriptor is closed anyway."""
    dh = _dh()
    saved, here = tmp_path / "saved", tmp_path / "here"
    saved.write_text("same\n")
    here.write_text("same\n")
    here.chmod(0o000)
    try:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            with pytest.raises(OSError):
                dh._same_bytes(str(saved), str(here))
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        here.chmod(0o644)


# -- malformed rows are a finding of their own --


def test_malformed_rows_are_their_own_finding(tmp_path):
    """Devin c4399914846: `live_repair_malformed:<n>` names the count, and the
    reports holding them, until each is dismissed."""
    dh = _dh()
    repo, head = _git_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[_row("../x"), _row("Z", flag="1")], reapply=[])  # fmt: skip
    found = dh.collect_live_repairs(tmp_path)
    assert found["malformed"] == 2 and found["malformed_in"] == ["20260101T000000Z-1"], found
    assert dh.live_repair_findings(found) == ["live_repair_malformed:2"]
    report = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair" / "REPAIR.json"
    data = json.loads(report.read_text())
    report.write_text(json.dumps({**data, "dismissed_at": "2026-01-02T00:00:00Z"}))
    found = dh.collect_live_repairs(tmp_path)
    assert found["malformed"] == 0 and dh.live_repair_findings(found) == [], found


# -- each offered step carries the report it came from --


def test_each_offered_step_names_its_report(tmp_path):
    """Devin c4399914589: steps from two reports, each step paired with the ts
    of the report that holds it."""
    repo, head = _git_repo(tmp_path)
    for n, ts in enumerate(("20260101T000000Z-1", "20260102T000000Z-2")):
        rows = [_row(f"d{n}/f{i}", disk_changed=False, flag="S") for i in range(4)]
        _report_at(tmp_path, ts, result="healed", repo=str(repo), tag=head, paths=rows,
                   reapply=[])  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path)
    assert len(found["saved"]) == 8, found
    assert found["saved_from"] == ["20260101T000000Z-1"] * 4 + ["20260102T000000Z-2"] * 4
