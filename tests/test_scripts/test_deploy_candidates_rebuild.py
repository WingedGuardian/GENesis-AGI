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
    assert "b.txt" in err and "adopt" in err
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


# ── derivation: a candidate carrying an excluded one's code goes out with it ──


def test_a_candidate_derived_from_an_excluded_one_is_excluded_too(dc, dc_ready, capsys):
    w = dc_ready
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.candidate("feat/c", {"z.txt": "z\n"}, base=a1)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/c"), w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert re.search(r"EXCLUDED: feat/c .*derived from .*feat/a", out), out
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "p.txt").exists()


def test_a_dependency_of_an_excluded_candidate_stays_live(dc, dc_ready, capsys):
    w = dc_ready
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base="feat/b")
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/b"), w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/b" not in out
    assert w.live_merges() == [("feat/b", hb)]


def test_siblings_stacked_on_a_live_shared_candidate_do_not_exclude_each_other(
    dc, dc_ready, capsys
):
    w = dc_ready
    hc = w.candidate("feat/c", {"c.txt": "c\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base="feat/c")
    hb = w.candidate("feat/b", {"y.txt": "y\n"}, base=hc)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/c"), w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/b" not in out and "EXCLUDED: feat/c" not in out, out
    assert w.live_merges() == [("feat/c", hc), ("feat/b", hb)]


def test_two_siblings_cut_from_an_excluded_candidate_both_go_out(dc, dc_ready, capsys):
    """A fork: B and C are both cut from E's first commit. Each also carries E's
    code, and the other sibling must not count as the shared third candidate."""
    w = dc_ready
    e1 = w.candidate("feat/e", {"p.txt": "e's code\n"})
    w.candidate("feat/e", {"a.txt": "a1\nE-SIDE\na3\n"})
    w.candidate("feat/b", {"b2.txt": "b\n"}, base=e1)
    w.candidate("feat/c", {"c2.txt": "c\n"}, base=e1)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/e"), w.entry("feat/b"), w.entry("feat/c")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/e", out), out
    assert re.search(r"EXCLUDED: feat/c .*derived from .*feat/e", out), out
    assert w.live_merges() == []
    assert not (w.root / "p.txt").exists()


def test_a_linear_stack_on_an_excluded_candidate_goes_out_whole(dc, dc_ready, capsys):
    """E <- F <- B, F cut from E's first commit: F and B both carry E's code."""
    w = dc_ready
    e1 = w.candidate("feat/e", {"p.txt": "e's code\n"})
    w.candidate("feat/e", {"a.txt": "a1\nE-SIDE\na3\n"})
    f1 = w.candidate("feat/f", {"f.txt": "f\n"}, base=e1)
    w.candidate("feat/b", {"b2.txt": "b\n"}, base=f1)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/e"), w.entry("feat/f"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/f .*derived from .*feat/e", out), out
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/e", out), out
    assert not (w.root / "p.txt").exists()


def test_two_excluded_candidates_sharing_a_commit_do_not_cancel_out(dc, dc_ready, capsys):
    """E1 and E2 (both excluded) and B all hold commit s: B carries their code."""
    w = dc_ready
    s = w.candidate("feat/s", {"s.txt": "shared\n"})
    w.candidate("feat/e1", {"a.txt": "a1\nE1\na3\n"}, base=s)
    w.candidate("feat/e2", {"b.txt": "E2\n"}, base=s)
    w.candidate("feat/b", {"b2.txt": "b\n"}, base=s)
    w.advance_main({"a.txt": "a1\nMAIN\na3\n", "b.txt": "MAIN\n"})
    w.write_manifest([w.entry("feat/e1"), w.entry("feat/e2"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/b .*derived from", out), out
    assert not (w.root / "s.txt").exists()


def test_a_conflict_with_a_later_excluded_candidate_is_recomputed(dc, dc_ready, capsys):
    w = dc_ready
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.candidate("feat/b", {"q.txt": "from b\n"}, base=a1)
    hc = w.candidate("feat/c", {"q.txt": "from c\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b"), w.entry("feat/c")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/a", out), out
    assert w.live_merges() == [("feat/c", hc)]


def test_an_exclusion_derived_from_a_lifted_conflict_is_released(dc, dc_ready, capsys):
    """X conflicts; B carries X's code; C conflicts only with B (in its SECOND
    commit); D was cut from C's first commit, so D merges cleanly on the first
    pass and is excluded there only as carrying C's code. Once B is out, C
    merges, so D's exclusion no longer has a source and must be released: the
    answer is C and D live, never D held out by a conflict that no longer
    exists."""
    w = dc_ready
    x1 = w.candidate("feat/x", {"px.txt": "x\n"})
    w.candidate("feat/x", {"a.txt": "a1\nX-SIDE\na3\n"})
    w.candidate("feat/b", {"q.txt": "from b\n"}, base=x1)
    c1 = w.candidate("feat/c", {"c1.txt": "c1\n"})
    hc = w.candidate("feat/c", {"q.txt": "from c\n"})
    hd = w.candidate("feat/d", {"d.txt": "d\n"}, base=c1)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry(b) for b in ("feat/x", "feat/b", "feat/c", "feat/d")])
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert dict(w.live_merges()) == {"feat/c": hc, "feat/d": hd}, out
    assert "derived from excluded feat/c" not in out and "derived from excluded feat/d" not in out


def test_a_dependency_that_gained_commits_reads_as_carrying_the_excluded_code(dc, dc_ready, capsys):
    """feat/a was cut from feat/b's first commit, then b gained one. Git cannot
    tell this from b having been cut from a's first commit, so b goes out with
    a: the conservative side, reported by name. Pinned so changing it is a
    decision, not a drift."""
    w = dc_ready
    b1 = w.candidate("feat/b", {"y.txt": "y\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base=b1)
    w.candidate("feat/b", {"y2.txt": "y2\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/b"), w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/a", capsys.readouterr().out)


def test_carries_is_the_derived_dependency_rule(dc):
    assert dc.carries({"a1", "c1"}, {"a1", "a2"}, set())
    assert dc.carries({"a1", "a2", "c1"}, {"a1", "a2"}, set())
    assert not dc.carries({"b1"}, {"b1", "a1"}, set())
    assert not dc.carries({"c1"}, {"a1"}, set())
    assert not dc.carries({"c1", "b1"}, {"c1", "a1"}, {"c1"})


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
    assert "hooks-elsewhere" in capsys.readouterr().err


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
