"""deploy_candidates rebuild: what goes live, what is excluded and why, and when
nothing may move at all.

The scratch world is tests/test_scripts/_deploy_candidates_world.py. Every
exclusion has a control that goes live, so a rebuild that excludes everything
(or nothing) fails the suite.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess

import pytest

from tests.test_scripts._deploy_candidates_world import SCRIPTS

# ── merging ────────────────────────────────────────────────────────────────


def test_rebuild_merges_two_candidates_onto_origin_main(dc, dc_ready, capsys):
    w = dc_ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.live_merges() == [("feat/a", ha), ("feat/b", hb)]
    assert (w.root / "x.txt").exists() and (w.root / "y.txt").exists()
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "live"


def test_rebuild_without_the_lock_is_refused(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=False) == 1
    assert "update.lock" in capsys.readouterr().err
    assert w.live_merges() == []


def test_a_lock_fd_this_run_does_not_hold_is_refused(dc, dc_ready, capsys):
    """The fd names update.lock but another process holds it (shared): the
    engine must not proceed on a lock it does not hold."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    lock_path = w.home / ".genesis" / "locks" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    mine = os.open(lock_path, os.O_WRONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_SH)
        env = {"DEPLOY_CANDIDATES_LOCK_FD": str(mine)}
        assert w.run(dc, "rebuild", lock=False, extra_env=env) == 1
        assert "held by another process" in capsys.readouterr().err
    finally:
        os.close(mine)
        os.close(holder)


def test_the_rebuild_merges_the_pinned_head_never_the_branch_tip(dc, dc_ready, capsys):
    """A branch that moved after it was added is excluded until it is added again:
    a candidate is a commit, not a branch name."""
    w = dc_ready
    h1 = w.candidate("feat/a", {"x.txt": "added\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    h2 = w.candidate("feat/a", {"x.txt": "NOT added\n"})
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*moved to " + h2[:12], out), out
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "x.txt").exists()
    assert h1 not in w.git(w.root, "rev-list", "refs/heads/live").stdout
    # Adding the new head puts it back.
    assert w.add(dc, "feat/a") == 0
    assert w.run(dc, "rebuild") == 0
    assert ("feat/a", h2) in w.live_merges()


def test_a_conflicting_candidate_is_excluded_and_the_rest_go_live(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert "a.txt" in out
    assert w.live_merges() == [("feat/b", hb)]
    assert (w.root / "a.txt").read_text() == "a1\nMAIN-SIDE\na3\n"


def test_a_candidate_with_nothing_beyond_its_base_never_wedges_the_rebuild(dc, dc_ready, capsys):
    w = dc_ready
    w.git(w.root, "branch", "feat/empty", "origin/main")
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/empty"), w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    assert "contained: feat/empty" in capsys.readouterr().out
    assert w.live_merges() == [("feat/a", ha)]
    assert w.run(dc, "rebuild") == 0
    assert "checkout: unchanged (" in capsys.readouterr().out


def test_the_rebuild_trailer_is_a_real_trailer_and_pre_push_refuses_it(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    tip = w.rev("refs/heads/live")
    trailer = w.git(
        w.root, "log", "-1", "--format=%(trailers:key=Deploy-rebuild,valueonly)", tip
    ).stdout.strip()
    assert trailer, "the Deploy-rebuild trailer must be readable as a git trailer"
    # The pre-push hook, invoked directly with git's stdin ref lines, refuses
    # publishing that commit; a branch without it is the control.
    zero = "0" * 40
    hook = SCRIPTS / "hooks" / "pre-push"

    def publish(ref: str, sha: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(hook), "fork", "url"],
            input=f"{ref} {sha} {ref} {zero}\n",
            cwd=w.root,
            env=w.env,
            capture_output=True,
            text=True,
        )

    res = publish("refs/heads/live", tip)
    assert res.returncode == 1 and "Deploy-rebuild" in res.stdout, res.stdout + res.stderr
    ctl = publish("refs/heads/feat/a", w.rev("refs/heads/feat/a"))
    assert ctl.returncode == 0, ctl.stdout + ctl.stderr


def _record_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", recording_run)
    return calls


def _checkouts(calls):
    return [c for c in calls if any(x in ("switch", "checkout") for x in c[3:5])]


def test_an_unchanged_set_moves_nothing_and_a_changed_one_checks_out_once(
    dc, dc_ready, capsys, monkeypatch
):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    calls = _record_git(monkeypatch)
    assert w.run(dc, "rebuild") == 0
    assert len(_checkouts(calls)) == 1, _checkouts(calls)
    tip = w.rev("refs/heads/live")
    reflog_before = w.git(w.root, "reflog", "show", "--format=%H", "refs/heads/live").stdout
    calls.clear()
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    assert _checkouts(calls) == []
    assert w.rev("refs/heads/live") == tip
    # Not even the ref moved: same origin/main, same candidate heads.
    assert w.git(w.root, "reflog", "show", "--format=%H", "refs/heads/live").stdout == reflog_before
    assert "checkout: unchanged (" in capsys.readouterr().out
    # A new candidate changes the tree: exactly one checkout.
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    calls.clear()
    assert w.run(dc, "rebuild") == 0
    assert len(_checkouts(calls)) == 1, _checkouts(calls)


def test_the_same_tree_under_new_commits_moves_the_ref_without_a_checkout(
    dc, dc_ready, capsys, monkeypatch
):
    """main absorbs the candidate's exact change (as a squash would): the tree is
    unchanged, so live's ref moves onto the new main without touching the files."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.advance_main({"x.txt": "x\n"}, "squash of feat/a")
    calls = _record_git(monkeypatch)
    assert w.run(dc, "rebuild") == 0
    assert _checkouts(calls) == []
    assert (
        w.git(
            w.root, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "refs/heads/live"
        ).returncode
        == 0
    )
    assert w.git(w.root, "status", "--porcelain").stdout == ""


def test_local_main_is_fast_forwarded(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    new_main = w.advance_main({"m.txt": "m\n"})
    assert w.rev("refs/heads/main") != new_main
    assert w.run(dc, "rebuild") == 0
    assert w.rev("refs/heads/main") == new_main
    assert "main: fast-forwarded" in capsys.readouterr().out


def test_local_main_checked_out_in_another_worktree_is_not_moved(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0  # the checkout is on live now; main is free
    old_main = w.rev("refs/heads/main")
    w.git(w.root, "worktree", "add", "-q", str(w.tmp / "main-wt"), "main")
    new_main = w.advance_main({"m.txt": "m\n"})
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert w.rev("refs/heads/main") == old_main != new_main
    assert "not fast-forwarded" in out
    assert w.git(w.root, "merge-base", "--is-ancestor", new_main, "refs/heads/live").returncode == 0


def test_a_dirty_checkout_refuses_and_names_the_files(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / "b.txt").write_text("edited in place\n")
    (w.root / "AGENTS.md").write_text("machine-written\n")  # untracked: not dirty
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "b.txt" in err and "a branch cut from origin/main (never from `live`" in err
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    # Control: an excused (ephemeral) tracked edit alone does not refuse.
    w.git(w.root, "checkout", "-q", "--", "b.txt")
    (w.root / "AGENTS.md").unlink()
    w.advance_main({"AGENTS.md": "tracked\n"})
    w.git(w.root, "pull", "-q", "--ff-only")
    w.serving_sha = w.rev("HEAD")
    (w.root / "AGENTS.md").write_text("rewritten by the indexer\n")
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()


def test_an_ignored_file_in_the_way_refuses_before_anything_changes(dc, dc_ready, capsys):
    """git overwrites an IGNORED file in the way of a checkout without asking (a
    local settings or secrets file): the move refuses first, and nothing changes,
    not even live's git configuration."""
    w = dc_ready
    w.candidate("feat/a", {"local.cfg": "from the candidate\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / ".git" / "info" / "exclude").write_text("local.cfg\n")
    (w.root / "local.cfg").write_text("install-local secret\n")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "local.cfg" in err and "in the way" in err
    assert (w.root / "local.cfg").read_text() == "install-local secret\n"
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    got = w.git(w.root, "config", "--get", "gc.refs/heads/live.reflogExpire", check=False)
    assert got.stdout == ""


def _track_dir(w) -> None:
    """origin/main and the checkout hold a tracked directory d/ (one file)."""
    w.advance_main({"d/one.txt": "one\n"})
    w.git(w.root, "pull", "-q", "--ff-only")
    w.serving_sha = w.rev("HEAD")


def _dir_becomes_file(w) -> str:
    """A candidate that replaces the tracked directory d/ with a file d."""
    w.candidate("feat/f", {"d/one.txt": None})
    (w.tmp / "wt-feat-f" / "d").rmdir()
    return w.candidate("feat/f", {"d": "now a file\n"})


def test_a_tracked_file_that_becomes_a_directory_moves(dc, dc_ready, capsys):
    """Tracked content in the way is git's to replace: b.txt (tracked) becomes
    the directory b.txt/."""
    w = dc_ready
    h = w.candidate("feat/d", {"b.txt": None, "b.txt/inner.txt": "inner\n"})
    w.write_manifest([w.entry("feat/d")])
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.live_merges() == [("feat/d", h)]
    assert (w.root / "b.txt" / "inner.txt").read_text() == "inner\n"


def test_a_tracked_directory_that_becomes_a_file_moves(dc, dc_ready, capsys):
    w = dc_ready
    _track_dir(w)
    h = _dir_becomes_file(w)
    w.write_manifest([w.entry("feat/f")])
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.live_merges() == [("feat/f", h)]
    assert (w.root / "d").read_text() == "now a file\n"


def test_an_untracked_file_where_a_directory_must_go_refuses_first(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/d", {"u.txt/inner.txt": "inner\n"})
    w.write_manifest([w.entry("feat/d")])
    (w.root / "u.txt").write_text("untracked, in the way\n")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "untracked file is in the way at u.txt" in err, err
    assert (w.root / "u.txt").read_text() == "untracked, in the way\n"
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_an_untracked_file_inside_a_directory_that_becomes_a_file_refuses_first(
    dc, dc_ready, capsys
):
    w = dc_ready
    _track_dir(w)
    _dir_becomes_file(w)
    w.write_manifest([w.entry("feat/f")])
    (w.root / "d" / "extra.txt").write_text("untracked, in the way\n")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    # The refusal names the untracked file itself, not just the directory.
    assert "in the way" in err and "d/extra.txt" in err, err
    assert (w.root / "d" / "extra.txt").read_text() == "untracked, in the way\n"
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_a_rebuild_whose_tree_changed_moves_even_with_the_same_merges(dc, dc_ready, capsys):
    """Same base, same heads, same order, but a different tree: a repository-local
    merge driver changed how two candidates merge. The checkout must follow the
    tree the rebuild computed, not keep the old one because the merges match."""
    w = dc_ready
    w.advance_main({"f.txt": "base\n"})
    w.git(w.root, "pull", "-q", "--ff-only")
    w.serving_sha = w.rev("HEAD")
    w.candidate("feat/a", {"f.txt": "A\n"})
    w.candidate("feat/b", {"f.txt": "B\n"})
    (w.root / ".git" / "info" / "attributes").write_text("f.txt merge=pick\n")
    w.git(w.root, "config", "merge.pick.driver", "true")  # keeps ours (A)
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert [b for b, _ in w.live_merges()] == ["feat/a", "feat/b"]
    assert (w.root / "f.txt").read_text() == "A\n"
    w.git(w.root, "config", "merge.pick.driver", "cp %B %A")  # now takes theirs (B)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert (w.root / "f.txt").read_text() == "B\n"


def test_a_switch_that_fails_partway_says_what_changed(dc, dc_ready, capsys):
    """git can rewrite part of the working tree and then fail (a required smudge
    filter exiting non-zero: MEASURED, git 2.43), leaving HEAD where it was. The
    refusal must name what changed, never claim nothing moved. The filter is set
    in .git/info/attributes (repo-local), NOT a candidate .gitattributes, which
    admission refuses (a candidate's attributes could transform protected files)."""
    w = dc_ready
    w.candidate("feat/a", {"b.txt": None, "z.bin": "data\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    (w.root / ".git" / "info" / "attributes").write_text("*.bin filter=bad\n")
    w.git(w.root, "config", "filter.bad.smudge", "false")
    w.git(w.root, "config", "filter.bad.required", "true")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "failed partway" in err, err
    assert "z.bin" in err or "b.txt" in err, err  # a path the move touched
    assert "nothing moved" not in err, err
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_a_partial_switch_is_seen_even_in_an_untracked_dir_or_an_ignored_file(dc, dc_ready, capsys):
    """The holes a plain `--porcelain` status leaves: a file written inside an
    ALREADY-untracked directory (git collapses the dir to one line, so the new
    file inside is invisible), an IGNORED file git wrote, and a
    `status.showUntrackedFiles=no` config. The before/after status uses
    `-uall --ignored` (and the flag overrides the config), so the partial move
    is named rather than reported as 'nothing moved'."""
    w = dc_ready
    w.candidate("feat/a", {"newdir/a.txt": "y\n", "keep.log": "L\n", "zz.bin": "data\n"})
    w.write_manifest([w.entry("feat/a")])
    # newdir is untracked BEFORE the move; keep.log is ignored; untracked files
    # are hidden from a plain status by config — all three would mask the move.
    (w.root / "newdir").mkdir()
    (w.root / "newdir" / "x.untracked").write_text("x\n")
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    (w.root / ".git" / "info" / "exclude").write_text("*.log\n")
    (w.root / ".git" / "info" / "attributes").write_text("*.bin filter=bad\n")
    w.git(w.root, "config", "status.showUntrackedFiles", "no")
    w.git(w.root, "config", "filter.bad.smudge", "false")
    w.git(w.root, "config", "filter.bad.required", "true")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    wrote = [p for p in ("newdir/a.txt", "keep.log") if (w.root / p).exists()]
    assert wrote, "the scenario did not produce a partial write"
    assert "failed partway" in err, err
    assert "nothing moved" not in err, err
    assert any(p in err for p in wrote), err


def test_a_large_untracked_tree_in_the_way_is_named_by_count(dc, dc_ready, capsys):
    """An ignored tree the size of node_modules would otherwise print every
    file: the refusal names ten and counts the rest."""
    w = dc_ready
    _track_dir(w)
    _dir_becomes_file(w)
    w.write_manifest([w.entry("feat/f")])
    for i in range(12):
        (w.root / "d" / f"extra{i:02}.txt").write_text("untracked\n")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "d/extra09.txt, and 2 more" in err and "extra10" not in err, err


def test_an_excused_edit_the_move_would_overwrite_refuses_first(dc, dc_ready, capsys):
    """The dirty check excuses machine-written tracked files (AGENTS.md), but git
    refuses to overwrite one with uncommitted changes: the move refuses first."""
    w = dc_ready
    w.advance_main({"AGENTS.md": "tracked\n"})
    w.git(w.root, "pull", "-q", "--ff-only")
    w.serving_sha = w.rev("HEAD")
    w.candidate("feat/a", {"AGENTS.md": "from the candidate\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / "AGENTS.md").write_text("rewritten by the indexer\n")
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "AGENTS.md (it has uncommitted changes" in err, err
    assert (w.root / "AGENTS.md").read_text() == "rewritten by the indexer\n"
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_live_checked_out_in_another_worktree_refuses_the_rebuild(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.git(w.root, "switch", "-q", "main")
    w.git(w.root, "worktree", "add", "-q", str(w.tmp / "wt-live"), "live")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "another worktree" in capsys.readouterr().err


def test_a_foreign_commit_on_live_refuses_the_rebuild(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.commit(w.root, {"hand.txt": "by hand\n"}, "a commit made by hand on live")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "by hand" in capsys.readouterr().err


def test_a_hand_made_commit_carrying_the_trailer_is_still_foreign(dc, dc_ready, capsys):
    """Only the engine's own merges (its committer identity, two parents, the
    trailer) are taken as rebuild merges: a hand-made two-parent commit that
    copies the trailer does not whitelist its second parent's history."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    side = w.candidate("side", {"s.txt": "smuggled\n"})
    tip = w.rev("refs/heads/live")
    tree = w.git(w.root, "write-tree").stdout.strip()
    fake = w.git(
        w.root,
        "commit-tree",
        tree,
        "-p",
        tip,
        "-p",
        side,
        "-m",
        "Deploy rebuild x: merge side\n\nDeploy-rebuild: x\nDeploy-candidate: side",
    ).stdout.strip()
    w.git(w.root, "update-ref", "refs/heads/live", fake)
    w.git(w.root, "reset", "-q", "--hard", fake)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "neither a rebuild merge" in capsys.readouterr().err


# ── one candidate per commit: a shared unmerged commit excludes every sharer ──


def _sharers(w, shape: str) -> list[str]:
    """Candidates that share feat/s's first commit, in the shape named."""
    s1 = w.candidate("feat/s", {"s.txt": "shared\n"})
    w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    if shape == "fork":  # a second branch cut from the same commit
        w.candidate("feat/u", {"u.txt": "u\n"}, base=s1)
        return ["feat/s", "feat/t", "feat/u"]
    if shape == "dependency-gained":  # feat/s moved on after feat/t was cut from it
        w.candidate("feat/s", {"s2.txt": "s2\n"})
    return ["feat/s", "feat/t"]


@pytest.mark.parametrize("shape", ["stack", "fork", "dependency-gained"])
def test_candidates_sharing_an_unmerged_commit_all_go_out_by_name(dc, dc_ready, capsys, shape):
    """Git cannot say whose code a shared commit is, so no rule could keep one
    sharer live and leave the other out: every sharer is excluded, named, and an
    independent control goes live."""
    w = dc_ready
    names = _sharers(w, shape)
    hc = w.candidate("feat/c", {"c.txt": "c\n"})
    w.write_manifest([w.entry(n) for n in names] + [w.entry("feat/c")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    for n in names:
        assert re.search(rf"EXCLUDED: {re.escape(n)} .*shares unmerged commits with", out), out
    assert w.live_merges() == [("feat/c", hc)]
    assert not (w.root / "s.txt").exists() and (w.root / "c.txt").exists()


def test_a_stack_listed_by_its_top_branch_goes_live_whole(dc, dc_ready, capsys):
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "shared\n"})
    ht = w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    w.write_manifest([w.entry("feat/t")])
    assert w.run(dc, "rebuild") == 0
    assert w.live_merges() == [("feat/t", ht)], capsys.readouterr().out
    assert (w.root / "s.txt").exists() and (w.root / "t.txt").exists()


def test_a_commit_shared_only_through_origin_main_is_not_shared(dc, dc_ready, capsys):
    """Two branches cut from one commit that has since landed upstream share
    nothing unmerged: both go live."""
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "shared\n"})
    ht = w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    hu = w.candidate("feat/u", {"u.txt": "u\n"}, base=s1)
    w.git(w.root, "push", "-q", str(w.origin), f"{s1}:refs/heads/main")
    w.write_manifest([w.entry("feat/t"), w.entry("feat/u")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "shares unmerged commits" not in out, out
    assert w.live_merges() == [("feat/t", ht), ("feat/u", hu)]


def test_sharing_and_conflict_exclusions_combine(dc, dc_ready, capsys):
    w = dc_ready
    names = _sharers(w, "stack")
    w.candidate("feat/x", {"a.txt": "a1\nX-SIDE\na3\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    hc = w.candidate("feat/c", {"c.txt": "c\n"})
    w.write_manifest([w.entry(n) for n in names] + [w.entry("feat/x"), w.entry("feat/c")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    for n in names:
        assert re.search(rf"EXCLUDED: {re.escape(n)} .*shares unmerged commits with", out), out
    assert re.search(r"EXCLUDED: feat/x .*conflict", out), out
    assert w.live_merges() == [("feat/c", hc)]


def test_shared_with_pairs_every_sharer_and_skips_a_head_that_is_gone(dc, dc_ready):
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "shared\n"})
    ht = w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    hc = w.candidate("feat/c", {"c.txt": "c\n"})
    base = w.rev("refs/remotes/origin/main")
    engine = dc.Engine(w.root, w.env)
    heads = {"feat/s": s1, "feat/t": ht, "feat/c": hc, "feat/gone": "f" * 40}
    assert dc.plan.shared_with(engine, base, heads) == {
        "feat/s": ["feat/t"],
        "feat/t": ["feat/s"],
    }


# ── per-candidate checks re-derived at every rebuild ─────────────────────────


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"state": "CLOSED"}, "closed without merging"),
        ({"baseRefName": "release"}, "targets 'release'"),
        ({"headRefOid": "3" * 40}, "not the pinned"),
    ],
)
def test_a_pr_that_stopped_qualifying_is_excluded_at_the_next_rebuild(
    dc, dc_ready, capsys, kw, why
):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    assert ("feat/a", w.rev("refs/heads/feat/a")) in w.live_merges()
    w.gh_states[11].update(kw)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    assert why in capsys.readouterr().out
    assert w.live_merges() == [("feat/b", hb)]


@pytest.mark.parametrize(("rc", "outcome"), [(1, "gone"), (128, "unknown")])
def test_a_branch_git_cannot_resolve_is_unknown_not_gone(dc, rc, outcome):
    """`rev-parse --verify -q` exits 1 only for a ref that does not exist. Any
    other failure is git unable to answer: an UNKNOWN, which refuses the whole
    command, never the known negative "the branch does not exist"."""

    class Stub:
        def git(self, *args, check=True):
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="fatal: boom")

    cand = {"branch": "feat/a", "pr": None, "verified_head": "1" * 40}
    if outcome == "gone":
        assert dc.gate.gate_failure(Stub(), "0" * 40, cand) == "the branch does not exist here"
    else:
        with pytest.raises(dc.core.Unknown, match="cannot resolve the branch feat/a"):
            dc.gate.gate_failure(Stub(), "0" * 40, cand)


def test_admission_is_checked_again_at_rebuild(dc, dc_ready, capsys):
    """A manifest entry written by hand (or before a rule existed) still meets
    admission: the hook dir is refused at rebuild, not only at add."""
    w = dc_ready
    w.candidate("feat/h", {"scripts/hooks/pre-commit": "#!/bin/sh\nexit 0 # changed\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/h"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    assert re.search(r"EXCLUDED: feat/h .*admission: .*hook", capsys.readouterr().out)
    assert w.live_merges() == [("feat/b", hb)]


def test_a_candidate_changing_gitattributes_is_refused(dc, dc_ready, capsys):
    """A candidate's .gitattributes transforms how git writes files on the
    working-tree switch to `live` (a broad `* text eol=crlf` rewrites a protected
    hook's shebang to CRLF, unexecutable), so admission refuses it like a hook
    change. The off-tree merge uses --attr-source=base and is unaffected; the
    checkout switch is not."""
    w = dc_ready
    w.candidate("feat/attr", {".gitattributes": "* text eol=crlf\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/attr"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    assert re.search(r"EXCLUDED: feat/attr .*\.gitattributes", capsys.readouterr().out)
    assert w.live_merges() == [("feat/b", hb)]


def test_path_refusal_covers_gitattributes_at_any_depth(dc):
    """Root and nested .gitattributes both transform files on checkout."""
    assert dc.gate.path_refusal(".gitattributes") is not None
    assert dc.gate.path_refusal("scripts/hooks/.gitattributes") is not None
    assert dc.gate.path_refusal("src/genesis/.gitattributes") is not None
    assert dc.gate.path_refusal("src/genesis/app.py") is None  # ordinary file unaffected


# ── an unknown never moves `live` ─────────────────────────────────────────────


def _built(w, dc):
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    return w.rev("refs/heads/live"), w.manifest_path.read_text()


def test_an_unreadable_pr_state_refuses_the_rebuild_and_moves_nothing(dc, dc_ready, capsys):
    w = dc_ready
    tip, manifest = _built(w, dc)
    w.advance_main({"z.txt": "z\n"})
    w.gh_fail.add(11)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "cannot read PR #11" in err and "nothing changed" in err
    assert w.rev("refs/heads/live") == tip and w.manifest_path.read_text() == manifest
    assert not (w.root / "z.txt").exists()


def test_a_failed_fetch_refuses_the_rebuild_and_moves_nothing(dc, dc_ready, capsys):
    w = dc_ready
    tip, manifest = _built(w, dc)
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": "4" * 40})
    w.git(w.root, "remote", "set-url", "origin", str(w.tmp / "nowhere.git"))
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "nothing changed" in capsys.readouterr().err
    assert w.rev("refs/heads/live") == tip and w.manifest_path.read_text() == manifest


# ── retirement: only on proof that the change is in the fetched main ─────────


@pytest.mark.parametrize("after", ["unchanged", "evolved", "reverted"])
def test_a_squash_merged_pr_retires_whatever_main_did_afterwards(dc, dc_ready, capsys, after):
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/a")
    if after == "evolved":
        w.advance_main({"a.txt": "a1\nA-SIDE-v2\na3\n"})
    elif after == "reverted":
        w.git(w.up, "revert", "--no-edit", sq)
        w.git(w.up, "push", "-q", "origin", "main")
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq})
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert "retired: feat/a" in capsys.readouterr().out
    assert w.manifest()["candidates"] == []
    assert w.live_merges() == []


def test_a_merged_candidate_is_not_retired_while_a_listed_one_shares_its_commits(
    dc, dc_ready, capsys
):
    """A hand-listed pair [feat/s, feat/t stacked on it]: feat/s squash-merges and
    main then reverts it. Retiring feat/s would leave feat/t alone, and the next
    rebuild would put s's reverted code back with it. So feat/s stays listed and
    both stay out until one is dropped; status says the same."""
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "s\n"})
    w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    w.pr(5, "feat/s", s1)
    w.write_manifest([w.entry("feat/s", pr=5), w.entry("feat/t")])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/s")
    w.git(w.up, "revert", "--no-edit", sq)
    w.git(w.up, "push", "-q", "origin", "main")
    w.gh_states[5].update(state="MERGED", mergeCommit={"oid": sq})
    w.git(w.root, "fetch", "-q", "origin")  # status judges the last-fetched main
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "retires at the next rebuild" not in out, out
    assert "not retired: feat/t shares its unmerged commits" in out, out
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/s .*not retired: feat/t shares its unmerged commits", out), (
        out
    )
    assert re.search(r"EXCLUDED: feat/t ", out) and "retired: feat/s" not in out, out
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/s", "feat/t"]
    assert w.live_merges() == [] and not (w.root / "s.txt").exists()
    # Control: listed alone, the same merged feat/s retires.
    w.write_manifest([w.entry("feat/s", pr=5, head=s1)])
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert "retired: feat/s" in capsys.readouterr().out
    assert w.manifest()["candidates"] == []


def test_a_retirement_names_commits_the_branch_gained_after_its_pinned_head(dc, dc_ready, capsys):
    """`live` ran only the pinned head. A branch that moved on before its PR
    merged still retires, and the commits after the pinned head are named as
    never live, so they are not lost silently with the manifest entry."""
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/a")
    w.candidate("feat/a", {"later.txt": "after it was added\n"})
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq})
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "retired: feat/a" in out
    assert "feat/a has 1 commit(s) after its pinned head that were never live" in out
    assert w.manifest()["candidates"] == []
    assert not (w.root / "later.txt").exists()


def test_a_retirement_warns_when_the_pr_merged_without_the_pinned_commit(dc, dc_ready, capsys):
    """The PR head was rewritten (force-pushed) after it was added and merged: it still
    retires (main holds the reviewed final version), but the report says that
    what ran on `live` is not what main holds."""
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    rewritten = w.candidate("feat/a-rewritten", {"other.txt": "rewritten\n"})
    sq = w.squash_merge("feat/a-rewritten")
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq}, headRefOid=rewritten)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "retired: feat/a" in out
    assert "merged without the pinned commit" in out


def test_a_retirement_says_when_it_cannot_tell_what_the_pr_merged(dc, dc_ready, capsys):
    """The PR head was rewritten elsewhere and never fetched here: the warning
    cannot be computed, so the output says so instead of staying silent."""
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/a")
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq}, headRefOid="9" * 40)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "retired: feat/a" in out
    assert "cannot tell whether PR #11 merged the pinned commit" in out


def test_a_pr_merged_into_another_branch_is_excluded_not_retired(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.pr(11, "feat/a", state="MERGED", baseRefName="release", mergeCommit={"oid": "5" * 40})
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*merged into 'release'", out), out
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/a"]


def test_a_merge_commit_that_is_not_in_the_fetched_main_retires_nothing(dc, dc_ready, capsys):
    w = dc_ready
    head = w.candidate("feat/a", {"x.txt": "x\n"})
    w.pr(11, "feat/a", state="MERGED", mergeCommit={"oid": head})
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    assert re.search(r"EXCLUDED: feat/a .*not in origin/main", capsys.readouterr().out)
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/a"]


def test_a_retirement_is_not_saved_when_the_checkout_cannot_move(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/a")
    w.advance_main({"n.txt": "new on main\n"})
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq})
    (w.root / "n.txt").write_text("untracked, in the way\n")
    before = w.manifest_path.read_text()
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "in the way" in capsys.readouterr().err
    assert w.manifest_path.read_text() == before


def test_a_retirement_never_removes_an_entry_re_added_meanwhile(dc, dc_ready, capsys, monkeypatch):
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.pr(11, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=11)])
    assert w.run(dc, "rebuild") == 0
    sq = w.squash_merge("feat/a")
    w.gh_states[11].update(state="MERGED", mergeCommit={"oid": sq})
    h2 = w.candidate("feat/a", {"later.txt": "after the merge\n"})
    real_move = dc.plan.move_checkout

    def move_then_re_add(repo, plan, branch):
        moved = real_move(repo, plan, branch)
        w.write_manifest([w.entry("feat/a", head=h2)])  # a concurrent re-add
        return moved

    monkeypatch.setattr(dc.plan, "move_checkout", move_then_re_add)
    assert w.run(dc, "rebuild") == 0
    [e] = w.manifest()["candidates"]
    assert e["verified_head"] == h2


# ── readiness ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "what",
    [
        "serving-unknown",
        "serving-old",
        "pr-c-missing",
        "hook-stale",
        "helper-stale",
        "hook-missing",
    ],
)
def test_readiness_refuses_each_unmet_condition(dc, dc_ready, capsys, monkeypatch, what):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    hooks = w.root / ".git" / "hooks"
    needle = {
        "serving-unknown": "serving commit is unknown",
        "serving-old": "a newer fix",
        "pr-c-missing": "PR C",
        "hook-stale": "pre-push",
        "helper-stale": "db_admission_check.py",
        "hook-missing": "commit-msg",
    }[what]
    if what == "serving-unknown":
        w.serving_sha = None
    elif what == "serving-old":
        newer = w.advance_main({"z.txt": "z\n"})
        w.git(w.root, "fetch", "-q", "origin")
        monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", (("a newer fix", newer),))
    elif what == "pr-c-missing":
        new = w.advance_main({"scripts/lib/deploy_live.sh": None}, "drop the PR C file")
        w.git(w.root, "fetch", "-q", "origin")
        w.serving_sha = new
        monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", ())
    elif what == "hook-stale":
        (hooks / "pre-push").write_text("#!/bin/sh\nexit 0 # edited\n")
    elif what == "helper-stale":
        (hooks / "db_admission_check.py").write_text("# edited\n")
    elif what == "hook-missing":
        (hooks / "commit-msg").unlink()
    assert w.run(dc, "rebuild") == 1, what
    err = capsys.readouterr().err
    assert "not ready" in err and needle in err, err
    assert w.live_merges() == []
    assert w.add(dc, "feat/a") == 1
    assert needle in capsys.readouterr().err


def test_readiness_reads_pr_c_from_the_servers_base_never_the_working_tree(
    dc, dc_ready, capsys, monkeypatch
):
    """The PR C file present only in the working tree (or only in a candidate)
    does not make the install ready: it is read from the server's base."""
    w = dc_ready
    new = w.advance_main({"scripts/lib/deploy_live.sh": None}, "drop the PR C file")
    w.git(w.root, "pull", "-q", "--ff-only")
    w.serving_sha = new
    monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", ())
    (w.root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    (w.root / "scripts" / "lib" / "deploy_live.sh").write_text("# untracked copy\n")
    w.candidate("feat/c", {"scripts/lib/deploy_live.sh": "# a candidate copy\n"})
    assert w.add(dc, "feat/c") == 1
    assert "PR C" in capsys.readouterr().err


def test_readiness_checks_the_directory_git_runs_hooks_from(dc, dc_ready, capsys, tmp_path):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    elsewhere = tmp_path / "hooks-elsewhere"
    elsewhere.mkdir()
    w.git(w.root, "config", "core.hooksPath", str(elsewhere))
    assert w.add(dc, "feat/a") == 1
    err = capsys.readouterr().err
    assert "core.hooksPath points git at" in err and "hooks-elsewhere" in err, err
    assert "sync-hooks.sh installs into" in err
    assert "set core.hooksPath in this repository to" in err, err  # a global setting
    # Control: a hooksPath that resolves to the directory sync-hooks.sh installs
    # into is not a divergence. git resolves this symlink itself when it prints
    # an absolute path, so this case passes even without the realpath compare.
    link = tmp_path / "hooks-link"
    link.symlink_to(w.root / ".git" / "hooks")
    w.git(w.root, "config", "core.hooksPath", str(link))
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    # Control that only the realpath compare passes: no core.hooksPath, but
    # .git/hooks is itself a symlink. git names the link's target, while
    # sync-hooks.sh installs into $GIT_COMMON_DIR/hooks and writes through the
    # link, so both are one directory.
    w.git(w.root, "config", "--unset", "core.hooksPath")
    moved = tmp_path / "hooks-moved"
    (w.root / ".git" / "hooks").rename(moved)
    (w.root / ".git" / "hooks").symlink_to(moved)
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()


def test_the_dirty_list_matches_the_deploy_scripts_pipeline(dc, dc_ready):
    w = dc_ready
    (w.root / "a.txt").write_text("edit\n")
    (w.root / "AGENTS.md").write_text("generated\n")
    w.git(w.root, "add", "AGENTS.md")
    w.git(w.root, "commit", "-q", "-m", "track AGENTS.md")
    (w.root / "AGENTS.md").write_text("regenerated\n")
    engine = dc.Engine(w.root, w.env)
    got = sorted(ln[3:] for ln in engine.dirty_lines())
    regex = w.env["DEPLOY_CANDIDATES_EPHEMERAL_RE"]
    porcelain = w.git(w.root, "status", "--porcelain", "--no-renames").stdout.splitlines()
    want = sorted(
        ln[3:] for ln in porcelain if not ln.startswith("??") and not re.search(regex, ln)
    )
    assert got == want and "a.txt" in got
