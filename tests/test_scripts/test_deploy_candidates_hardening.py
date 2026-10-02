"""deploy_candidates: the engine against hostile or unusual repository state.

Round 3 of #2721 closed five classes of defect found by review and a class
audit; each test here pins one instance and fails on the round-2 engine:

  A. git/gh plumbing exposed to repository state or the environment;
  B. working out what is live from merge trailers alone;
  C. a failure after `live` moved reported as "nothing changed";
  D. status predicting a rebuild that would refuse;
  E. a git error read as git's "no".

The scratch world is tests/test_scripts/_deploy_candidates_world.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

import pytest

# ── A. plumbing ────────────────────────────────────────────────────────────


def _good_and_evil(w):
    h = w.candidate("feat/a", {"f.txt": "good\n"})
    evil = w.candidate("feat/evil", {"f.txt": "evil\n"})
    return h, evil


def test_a_replace_ref_never_changes_what_live_runs(dc, dc_ready):
    """refs/replace/<pinned head> would make merge-tree read another commit's
    files while the manifest and the merge parent still name the pinned one."""
    w = dc_ready
    h, evil = _good_and_evil(w)
    w.git(w.root, "replace", h, evil)
    w.write_manifest([w.entry("feat/a", head=h)])
    assert w.run(dc, "rebuild") == 0
    shown = w.git(w.root, "--no-replace-objects", "show", "refs/heads/live:f.txt").stdout
    assert shown == "good\n"
    assert (w.root / "f.txt").read_text() == "good\n"


def test_git_variables_from_the_caller_cannot_redirect_the_engine(dc, dc_ready):
    """GIT_REPLACE_REF_BASE points git at another replace namespace, and
    GIT_CONFIG_COUNT sets config from the environment (here: a hooks path that
    would make readiness inspect the wrong directory)."""
    w = dc_ready
    h, evil = _good_and_evil(w)
    w.git(w.root, "update-ref", f"refs/other/{h}", evil)
    w.write_manifest([w.entry("feat/a", head=h)])
    env = {
        "GIT_REPLACE_REF_BASE": "refs/other/",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/nowhere",
    }
    assert w.run(dc, "rebuild", extra_env=env) == 0
    shown = w.git(w.root, "--no-replace-objects", "show", "refs/heads/live:f.txt").stdout
    assert shown == "good\n"


def test_a_graft_cannot_change_which_candidates_carry_whose_code(dc, dc_ready, capsys):
    """A graft that made feat/a appear to descend from the conflicting feat/x
    would exclude feat/a as carrying feat/x's code."""
    w = dc_ready
    hx = w.candidate("feat/x", {"a.txt": "a1\nX-SIDE\na3\n"})
    ha = w.candidate("feat/a", {"n.txt": "independent\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    (w.root / ".git" / "info" / "grafts").write_text(f"{ha} {hx}\n")
    w.write_manifest([w.entry("feat/x", head=hx), w.entry("feat/a", head=ha)])
    assert w.run(dc, "rebuild") == 0
    assert dict(w.live_merges()) == {"feat/a": ha}, capsys.readouterr().out


def test_a_candidates_gitattributes_never_decides_how_others_merge(dc, dc_ready, capsys):
    """Once `live` holds a candidate's `.gitattributes` with merge=union, a
    rebuild FROM `live` read it from the index and concatenated two candidates
    that conflict, a result nobody reviewed and GitHub would refuse."""
    w = dc_ready
    w.candidate("feat/k", {".gitattributes": "a.txt merge=union\n"})
    w.candidate("feat/p", {"a.txt": "a1\nP\na3\n"})
    w.candidate("feat/q", {"a.txt": "a1\nQ\na3\n"})
    w.write_manifest([w.entry("feat/k"), w.entry("feat/p"), w.entry("feat/q")])
    assert w.run(dc, "rebuild") == 0
    assert "EXCLUDED: feat/q" in capsys.readouterr().out
    w.advance_main({"m.txt": "moved\n"})  # forces a new plan, built from `live`
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/q" in out, out
    assert "Q" not in w.git(w.root, "show", "refs/heads/live:a.txt").stdout


def _commit_bytes_name(w, branch: str, name: bytes) -> str:
    wt = w.tmp / f"wt-{branch.replace('/', '-')}"
    w.git(w.root, "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
    with open(os.path.join(os.fsencode(str(wt)), name), "w") as fh:
        fh.write("x\n")
    w.git(wt, "add", "-A")
    w.git(wt, "commit", "-q", "-m", "a non-UTF-8 name")
    return w.rev(f"refs/heads/{branch}")


def test_a_non_utf8_path_is_handled_never_a_traceback(dc, dc_ready, capsys):
    """git output is decoded with surrogateescape: a candidate that commits a
    non-UTF-8 name goes live, one that puts such a name under a refused path is
    refused BY NAME, and an untracked one in the checkout does not stop drop."""
    w = dc_ready
    hb = _commit_bytes_name(w, "feat/bytes", b"name\xff.txt")
    _commit_bytes_name(w, "feat/hookbytes", b"scripts/hooks/x\xff")
    w.write_manifest([w.entry("feat/bytes", head=hb)])
    assert w.run(dc, "rebuild") == 0
    assert dict(w.live_merges()) == {"feat/bytes": hb}
    assert w.run(dc, "status") == 0
    capsys.readouterr()
    assert w.add(dc, "feat/hookbytes") == 1
    assert "hook" in capsys.readouterr().err
    with open(os.path.join(os.fsencode(str(w.root)), b"junk\xff"), "w") as fh:
        fh.write("untracked\n")
    assert w.run(dc, "drop", "feat/bytes") == 0
    assert w.live_merges() == []


def test_a_hook_that_is_not_executable_fails_readiness(dc, dc_ready, capsys):
    """git skips a hook without its execute bit, so the guard is off even when
    its bytes are right."""
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    (w.root / ".git" / "hooks" / "pre-push").chmod(0o644)
    assert w.add(dc, "feat/x") == 1
    assert "not executable" in capsys.readouterr().err


def test_gh_never_sees_a_repository_chosen_by_the_environment(dc, dc_ready):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.pr(1, "feat/x")
    seen = []
    real = w.gh

    def gh(args, cwd, env):
        seen.append(dict(env))
        return real(args, cwd, env)

    w.gh = gh
    assert (
        w.run(
            dc,
            "add",
            "feat/x",
            "--owner",
            "s1",
            "--pr",
            "1",
            extra_env={"GH_REPO": "someone/else", "GH_HOST": "elsewhere.invalid"},
        )
        == 0
    )
    assert seen and all("GH_REPO" not in e and "GH_HOST" not in e for e in seen)


def test_live_in_a_worktree_whose_path_holds_a_newline_is_still_found(dc, dc_ready, capsys):
    """`git worktree list --porcelain` without -z split `<root>\\nx` into the line
    `<root>`, which named THIS checkout: the switch would then move `live` under
    the other worktree."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.git(w.root, "switch", "-q", "main")
    w.git(w.root, "worktree", "add", "-q", f"{w.root}\nx", "live")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "checked out in another worktree" in capsys.readouterr().err


def test_a_unicode_line_separator_is_not_a_trailer_line(dc, dc_ready):
    """git and the pre-push hook split messages on LF only, so NEL inside a line
    does not start a `Deploy-rebuild:` line; the engine must read it the same."""
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"}, msg="subject\n\nsome text\u0085Deploy-rebuild: x")
    assert w.add(dc, "feat/x") == 0


def test_an_upstream_force_push_is_fetched_and_named(dc, dc_ready, capsys):
    """Without "+" the fetch of a force-pushed main fails for ever, and every
    rebuild refused as UNKNOWN. With it, the fetch lands and the commits the
    force-push dropped are named as foreign, with the force-push hint."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    w.advance_main({"m.txt": "1\n"})
    assert w.run(dc, "rebuild") == 0
    w.git(w.up, "reset", "-q", "--hard", "HEAD~1")
    w.commit(w.up, {"n.txt": "2\n"}, "rewritten")
    w.git(w.up, "push", "-q", "-f", "origin", "main")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "force-pushed" in err and "fetching" not in err, err


# ── B. what is live ────────────────────────────────────────────────────────


def _descendant_listed_first(w):
    hb = w.candidate("feat/b", {"bb.txt": "b\n"})
    ha = w.candidate("feat/a", {"a2.txt": "a\n"}, base=hb)
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    return ha, hb


def test_dropping_a_contained_candidate_takes_out_what_carries_it(dc, dc_ready, capsys):
    w = dc_ready
    ha, hb = _descendant_listed_first(w)
    assert w.run(dc, "rebuild") == 0
    assert "contained: feat/b" in capsys.readouterr().out
    assert w.run(dc, "drop", "feat/b") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*derived from dropped feat/b", out), out
    assert w.live_merges() == []
    assert not (w.root / "bb.txt").exists()


def test_dropping_the_carrier_keeps_the_contained_candidate(dc, dc_ready):
    w = dc_ready
    ha, hb = _descendant_listed_first(w)
    assert w.run(dc, "rebuild") == 0
    assert w.run(dc, "drop", "feat/a") == 0
    assert w.live_merges() == [("feat/b", hb)]
    assert (w.root / "bb.txt").exists() and not (w.root / "a2.txt").exists()


def test_dropping_one_of_two_candidates_at_one_commit_keeps_the_other(dc, dc_ready):
    w = dc_ready
    hx = w.candidate("feat/x", {"x.txt": "x\n"})
    w.git(w.root, "branch", "feat/y", hx)
    w.write_manifest([w.entry("feat/x"), w.entry("feat/y")])
    assert w.run(dc, "rebuild") == 0
    assert w.run(dc, "drop", "feat/x") == 0
    assert w.live_merges() == [("feat/y", hx)]


def test_status_reports_a_contained_candidate_as_live(dc, dc_ready, capsys):
    w = dc_ready
    ha, hb = _descendant_listed_first(w)
    assert w.run(dc, "rebuild") == 0
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    block_b = capsys.readouterr().out.split("feat/b  ", 1)[1]
    assert f"live at {hb[:12]}" in block_b.split("\n", 2)[1]


# ── C. the commit point ────────────────────────────────────────────────────


def ha_tip(w) -> str:
    return w.rev("refs/heads/live")[:12]


def test_a_failure_after_live_moved_is_a_warning_not_nothing_changed(dc, dc_ready, capsys):
    """A stale config.lock makes the reflog setting fail AFTER the checkout
    moved: the old engine reported that as a refusal ("nothing changed")."""
    w = dc_ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    lock = w.root / ".git" / "config.lock"
    lock.write_text("")
    try:
        assert w.run(dc, "rebuild") == 0
    finally:
        lock.unlink()
    out = capsys.readouterr().out
    assert f"WARNING: `live` is at {ha_tip(w)}, but" in out
    assert w.live_merges() == [("feat/a", ha)]


def test_an_unexpected_exception_still_exits_1_and_says_where_live_is(
    dc, dc_ready, capsys, monkeypatch
):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(dc.gate, "readiness_failures", boom)
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "unexpected RuntimeError: boom" in err and "`live` is at" in err


# ── D. status agrees with rebuild ──────────────────────────────────────────


def test_status_names_what_the_next_rebuild_would_refuse_on(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.commit(w.root, {"hand.txt": "made on live\n"}, "a commit made by hand on live")
    (w.root / "b.txt").write_text("edited in place\n")  # a dirty tracked file
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "next rebuild would REFUSE" in out
    assert "b.txt" in out and "neither a rebuild merge nor a candidate's" in out
    # Control: rebuild does refuse.
    assert w.run(dc, "rebuild") == 1


# ── E. git errors are unknown, never "no" ──────────────────────────────────


def test_git_errors_are_unknown_never_no(dc, dc_ready):
    w = dc_ready
    repo = dc.core.Repo(w.root, w.env)
    base = w.rev("refs/remotes/origin/main")
    missing = "1" * 40
    with pytest.raises(dc.core.Unknown):
        repo.is_ancestor(missing, base)
    with pytest.raises(dc.core.Unknown):
        repo.merge_base(missing, base)
    assert repo.resolve(missing) is None  # rev-parse -q: a missing object is "none"
    assert repo.is_ancestor(base, base) is True


def test_the_old_location_only_scrub_is_gone(dc):
    """Every caller GIT_* variable but the transport ones is dropped."""
    env = dc.core.scrub_env(
        {"GIT_DIR": "x", "GIT_REPLACE_REF_BASE": "y", "GIT_SSH_COMMAND": "ssh", "HOME": "/h"}
    )
    assert env == {"GIT_SSH_COMMAND": "ssh", "HOME": "/h"}


def test_a_failing_post_checkout_hook_after_the_move_is_not_nothing_moved(dc, dc_ready, capsys):
    """git returns a post-checkout hook's exit status AFTER the checkout has
    moved (a git-lfs hook without git-lfs on PATH fails exactly like this): the
    switch's exit code says nothing about whether the checkout moved."""
    w = dc_ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    hook = w.root / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert w.run(dc, "rebuild") == 0
    out = capsys.readouterr().out
    assert "WARNING: the checkout moved to" in out and "exited 1" in out
    assert w.live_merges() == [("feat/a", ha)]
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "live"


def test_a_candidate_name_must_be_valid_utf8(dc):
    """A name holding undecodable bytes is written into the merge message and
    read back changed (commit-tree re-encodes it), so drop could never find it
    in `live` again. It is refused at the door."""
    assert dc.core.valid_candidate_name("feat/caf\u00e9")  # UTF-8 is fine
    assert not dc.core.valid_candidate_name("feat/n\udcff")  # a raw 0xff byte


def test_status_shows_the_refusals_when_no_candidates_are_left(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    assert w.run(dc, "drop", "feat/a", "--no-rebuild") == 0
    assert w.manifest()["candidates"] == []
    (w.root / "b.txt").write_text("edited in place\n")  # a dirty tracked file
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "next rebuild would REFUSE" in out and "b.txt" in out


class _GitStub:
    """A Repo.git stand-in that answers every call with one exit code."""

    def __init__(self, rc: int, stdout: str = ""):
        self.rc, self.stdout = rc, stdout

    def git(self, *args, check=True, **_kw):
        return subprocess.CompletedProcess(list(args), self.rc, self.stdout, "fatal: boom")


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("git version 2.43.0", True),
        ("git version 2.44.1", True),
        ("git version 3.0.0", True),
        ("git version 2.42.0", False),
        ("git version 2.39.5", False),
        ("not git at all", False),
    ],
)
def test_git_older_than_2_43_fails_readiness(dc, text, ok):
    """git's global --attr-source (read by merge-tree) arrived in 2.41, and 2.43 fixed a segfault
    in it (per git's release notes)."""
    assert (dc.gate.git_version_failure(_GitStub(0, text + "\n")) is None) is ok


@pytest.mark.parametrize(
    ("call", "args"),
    [("resolve", ("HEAD",)), ("blob_at", ("HEAD", "x")), ("current_branch", ())],
)
def test_every_three_valued_answer_raises_unknown_on_a_git_error(dc, call, args):
    """Exit 1 is git's documented "no such thing" for these; anything else is
    git unable to answer, never an answer."""
    repo = dc.core.Repo.__new__(dc.core.Repo)
    repo.git = _GitStub(128).git
    with pytest.raises(dc.core.Unknown):
        getattr(repo, call)(*args)
    repo.git = _GitStub(1).git  # control: exit 1 is a known "none"
    assert getattr(repo, call)(*args) is None


def test_a_too_old_git_refuses_through_readiness(dc, dc_ready, capsys, monkeypatch):
    """The version floor is wired into readiness, so rebuild refuses and status
    says so. (Raising the floor past the installed git stands in for an old git.)"""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    monkeypatch.setattr(dc.gate, "MIN_GIT", (99, 0))
    assert w.run(dc, "rebuild") == 1
    cap = capsys.readouterr()
    assert "is too old" in cap.out + cap.err
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "ready: NO" in out and "is too old" in out
    monkeypatch.setattr(dc.gate, "MIN_GIT", (2, 43))  # control: the real floor
    assert w.run(dc, "rebuild") == 0


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def test_a_failing_adopt_report_after_the_branch_exists_is_a_warning(
    dc, dc_ready, capsys, monkeypatch
):
    w = dc_ready
    (w.root / "b.txt").write_text("edited\n")
    monkeypatch.setattr(dc.Engine, "_adopt_report", _boom)
    assert w.run(dc, "adopt", "adopt/x", "--owner", "s") == 0
    out = capsys.readouterr().out
    assert "WARNING: adopt/x WAS created at" in out and "the per-file report failed" in out
    assert w.rev("refs/heads/adopt/x")


def test_a_failing_stacked_check_after_drop_is_a_warning(dc, dc_ready, capsys, monkeypatch):
    w = dc_ready
    w.candidate("feat/a", {"p.txt": "p\n"})
    w.write_manifest([w.entry("feat/a")])
    monkeypatch.setattr(dc.Engine, "_warn_stacked", _boom)
    assert w.run(dc, "drop", "feat/a", "--no-rebuild") == 0
    out = capsys.readouterr().out
    assert "WARNING: feat/a WAS dropped from the manifest" in out
    assert w.manifest()["candidates"] == []


def test_status_reports_a_rebuild_it_cannot_plan_and_still_exits_0(
    dc, dc_ready, capsys, monkeypatch
):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])

    def no_plan(*_a, **_k):
        raise dc.core.Refusal("merge-tree said no")

    monkeypatch.setattr(dc.plan, "build_plan", no_plan)
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "next rebuild would REFUSE" in out
    assert "cannot plan the next rebuild: merge-tree said no" in out


def test_a_post_move_warning_names_the_commit_live_is_actually_at(
    dc, dc_ready, capsys, monkeypatch
):
    """When nothing changed, `live` stays at its tip and the plan's tip is a
    fresh commit that never became `live`: every post-move warning (in the move
    itself and in rebuild's own later steps) must name the commit `live` is at."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    tip = w.rev("refs/heads/live")
    time.sleep(1.1)  # a new second, so the plan's tip is a different commit
    w.git(w.root, "config", "--unset", "gc.refs/heads/live.reflogExpire")
    monkeypatch.setattr(dc.plan, "fast_forward_main", _boom)
    lock = w.root / ".git" / "config.lock"
    lock.write_text("")
    capsys.readouterr()
    try:
        assert w.run(dc, "rebuild") == 0
    finally:
        lock.unlink()
    out = capsys.readouterr().out
    assert "checkout: unchanged" in out
    assert w.rev("refs/heads/live") == tip
    assert f"WARNING: `live` is at {tip[:12]}, but keeping the reflog" in out
    assert f"WARNING: `live` is at {tip[:12]}, but fast-forwarding local main" in out
    assert out.count("WARNING: `live` is at") == out.count(f"WARNING: `live` is at {tip[:12]}")
