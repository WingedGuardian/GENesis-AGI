"""Hook candidates behind the owner's approval (#2978, PR H).

A candidate that changes a git or Claude Code hook goes live only with
`add --approve-hooks <who>`, recorded per candidate for its pinned head. A
rebuild refuses hook bytes no approved candidate holds, and after any move an
installed hook equal to the old checkout's copy is replaced: the scratch world
has no .genesis-hook-versions, so sync-hooks.sh alone would keep every changed
hook as "user-modified", which is the state the replacement exists for.
"""

from __future__ import annotations

import pytest

PRE_COMMIT = "scripts/hooks/pre-commit"
BASE_HOOK = "#!/bin/sh\n# pre-commit\nexit 0\n"


def _installed(w, name: str = "pre-commit") -> str:
    return (w.root / ".git" / "hooks" / name).read_text()


def _approve(w, dc, branch: str, *extra: str) -> int:
    return w.add(dc, branch, "--approve-hooks", "owner, in chat", *extra)


# ── add ────────────────────────────────────────────────────────────────────


def test_a_hook_candidate_without_the_flag_is_refused_and_names_it(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# changed\nexit 0\n"})
    assert w.add(dc, "feat/h") == 1
    assert "--approve-hooks" in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_the_flag_records_who_approved_which_head(dc, dc_ready, capsys):
    w = dc_ready
    head = w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# changed\nexit 0\n"})
    assert _approve(w, dc, "feat/h") == 0, capsys.readouterr()
    [e] = w.manifest()["candidates"]
    assert e["hook_approval"] == {
        "head": head,
        "approved_by": "owner, in chat",
        "approved_at": e["hook_approval"]["approved_at"],
    }
    capsys.readouterr()
    assert w.run(dc, "list") == 0
    assert "hooks approved by owner, in chat" in capsys.readouterr().out


def test_the_flag_on_a_candidate_that_changes_no_hook_is_refused(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert _approve(w, dc, "feat/x") == 1
    assert "nothing for --approve-hooks to approve" in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_an_empty_approver_is_refused(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# changed\nexit 0\n"})
    assert w.add(dc, "feat/h", "--approve-hooks", "  ") == 1
    assert "--approve-hooks is empty" in capsys.readouterr().err


def test_an_approval_never_moves_to_a_new_head(dc, dc_ready, capsys):
    """The branch moves: adding it again without the flag is refused and leaves
    the old pin (with its own approval) as it was; with the flag the approval
    names the new head."""
    w = dc_ready
    first = w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# one\nexit 0\n"})
    assert _approve(w, dc, "feat/h") == 0
    before = w.manifest_path.read_text()
    second = w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# two\nexit 0\n"})
    assert w.add(dc, "feat/h") == 1
    assert w.manifest_path.read_text() == before
    assert w.manifest()["candidates"][0]["hook_approval"]["head"] == first
    assert _approve(w, dc, "feat/h") == 0
    [e] = w.manifest()["candidates"]
    assert e["verified_head"] == second and e["hook_approval"]["head"] == second
    capsys.readouterr()


def test_the_approval_waives_only_the_hook_rule(dc, dc_ready, capsys):
    """A .gitattributes under a hook directory is refused by its own rule even
    when the head's hooks are approved."""
    w = dc_ready
    w.candidate(
        "feat/h",
        {PRE_COMMIT: "#!/bin/sh\n# changed\nexit 0\n", "scripts/hooks/.gitattributes": "* text\n"},
    )
    assert _approve(w, dc, "feat/h") == 1
    assert ".gitattributes" in capsys.readouterr().err


# ── rebuild, drop and the installed hooks ───────────────────────────────────


def test_an_approved_hook_goes_live_installed_and_leaves_with_its_candidate(dc, dc_ready, capsys):
    w = dc_ready
    new = "#!/bin/sh\n# approved\nexit 0\n"
    w.candidate("feat/h", {PRE_COMMIT: new})
    assert _approve(w, dc, "feat/h") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert (w.root / PRE_COMMIT).read_text() == new
    assert _installed(w) == new, "the approved hook was not installed"
    capsys.readouterr()
    # Installed == HEAD, so the next rebuild's readiness passes.
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.run(dc, "drop", "feat/h") == 0, capsys.readouterr()
    assert (w.root / PRE_COMMIT).read_text() == BASE_HOOK
    assert _installed(w) == BASE_HOOK, "the dropped candidate's hook stayed installed"


def test_a_hand_edited_installed_hook_is_left_alone(dc, dc_ready, capsys):
    """drop runs no readiness check, so it reaches the replacement with a hook
    someone edited: equal to neither checkout's copy, it stays."""
    w = dc_ready
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# approved\nexit 0\n"})
    assert _approve(w, dc, "feat/h") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    mine = "#!/bin/sh\n# edited by hand\nexit 0\n"
    (w.root / ".git" / "hooks" / "pre-commit").write_text(mine)
    assert w.run(dc, "drop", "feat/h") == 0, capsys.readouterr()
    assert (w.root / PRE_COMMIT).read_text() == BASE_HOOK
    assert _installed(w) == mine


def test_two_approved_candidates_whose_hook_changes_merge_are_excluded(dc, dc_ready, capsys):
    """Each change alone was approved; git merges them into bytes neither was.
    Like a conflict, that excludes them by name, and the rest still go live."""
    w = dc_ready
    w.candidate("feat/a", {PRE_COMMIT: "#!/bin/sh -e\n# pre-commit\nexit 0\n"})
    w.candidate("feat/b", {PRE_COMMIT: "#!/bin/sh\n# pre-commit\nexit 0 # b\n"})
    hx = w.candidate("feat/x", {"x.txt": "x\n"})
    assert _approve(w, dc, "feat/a") == 0
    assert _approve(w, dc, "feat/b") == 0
    assert w.add(dc, "feat/x") == 0
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    predicted = capsys.readouterr().out
    assert predicted.count("drop all but one") == 2, predicted
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/a" in out and "EXCLUDED: feat/b" in out and "drop all but one" in out
    assert w.live_merges() == [("feat/x", hx)]
    assert (w.root / PRE_COMMIT).read_text() == BASE_HOOK
    assert _installed(w) == BASE_HOOK


def test_dropping_the_approval_a_blend_matched_excludes_the_blend(dc, dc_ready, capsys):
    """A and B merge into exactly C's approved bytes, so all three go live. Drop C
    and its approval goes with it: the drop plans through attribution like a
    rebuild and excludes A and B rather than leave bytes nobody approved."""
    w = dc_ready
    w.candidate("feat/a", {PRE_COMMIT: "#!/bin/sh -e\n# pre-commit\nexit 0\n"})
    w.candidate("feat/b", {PRE_COMMIT: "#!/bin/sh\n# pre-commit\nexit 0 # b\n"})
    both = "#!/bin/sh -e\n# pre-commit\nexit 0 # b\n"
    w.candidate("feat/c", {PRE_COMMIT: both})
    hx = w.candidate("feat/x", {"x.txt": "x\n"})
    for b in ("feat/a", "feat/b", "feat/c"):
        assert _approve(w, dc, b) == 0, capsys.readouterr()
    assert w.add(dc, "feat/x") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert _installed(w) == both
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/c") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/a" in out and "EXCLUDED: feat/b" in out, out
    assert w.live_merges() == [("feat/x", hx)]
    assert (w.root / PRE_COMMIT).read_text() == BASE_HOOK
    assert _installed(w) == BASE_HOOK


def test_a_drop_excludes_a_hook_on_live_that_no_listed_approval_covers(dc, dc_ready, capsys):
    """`drop --no-rebuild` takes h's approval out of the manifest while `live`
    still runs h's hook. The next drop on `live` re-plans with the approvals
    still listed: h is excluded (repair never refuses over a hook)."""
    w = dc_ready
    new = "#!/bin/sh\n# approved once\nexit 0\n"
    w.candidate("feat/h", {PRE_COMMIT: new})
    hx = w.candidate("feat/x", {"x.txt": "x\n"})
    hy = w.candidate("feat/y", {"y.txt": "y\n"})
    assert _approve(w, dc, "feat/h") == 0
    assert w.add(dc, "feat/x") == 0
    assert w.add(dc, "feat/y") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert _installed(w) == new
    assert w.run(dc, "drop", "feat/h", "--no-rebuild") == 0, capsys.readouterr()
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/y") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/h" in out and "no current hook approval" in out, out
    assert w.live_merges() == [("feat/x", hx)] and hy
    assert (w.root / PRE_COMMIT).read_text() == BASE_HOOK
    assert _installed(w) == BASE_HOOK


def test_main_changing_an_approved_hook_excludes_that_candidate_only(dc, dc_ready, capsys):
    """origin/main changes the same hook after the branch was cut: the merge
    holds bytes nobody approved. The candidate is excluded with the remedy; an
    unrelated candidate and main's own change still go live."""
    w = dc_ready
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh -e\n# pre-commit\nexit 0\n"})
    hx = w.candidate("feat/x", {"x.txt": "x\n"})
    assert _approve(w, dc, "feat/h") == 0
    assert w.add(dc, "feat/x") == 0
    main_hook = "#!/bin/sh\n# pre-commit\nexit 0 # main\n"
    w.advance_main({PRE_COMMIT: main_hook})
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "EXCLUDED: feat/h" in out and "origin/main changed scripts/hooks/pre-commit" in out
    assert w.live_merges() == [("feat/x", hx)]
    assert (w.root / PRE_COMMIT).read_text() == main_hook
    assert _installed(w) == main_hook, "main's own hook change was not installed"


def test_a_hook_name_only_a_dropped_candidate_listed_is_removed(dc, dc_ready, capsys):
    """An approved candidate adds a hook to sync-hooks.sh's list: sync installs
    it on `live`, and after the drop nothing else would ever remove it."""
    w = dc_ready
    sync = (w.root / "scripts" / "hooks" / "sync-hooks.sh").read_text()
    added = sync.replace(
        '    "pre-merge-commit"\n', '    "pre-merge-commit"\n    "post-merge"\n', 1
    )
    assert added != sync, "the fixture's sync-hooks.sh changed shape"
    w.candidate(
        "feat/h",
        {"scripts/hooks/sync-hooks.sh": added, "scripts/hooks/post-merge": "#!/bin/sh\nexit 0\n"},
    )
    assert _approve(w, dc, "feat/h") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    hook = w.root / ".git" / "hooks" / "post-merge"
    assert hook.is_file(), "the approved new hook was not installed"
    assert w.run(dc, "drop", "feat/h") == 0, capsys.readouterr()
    assert not hook.exists(), "the dropped candidate's hook is still installed"


def test_two_approved_candidates_with_identical_hook_bytes_go_live(dc, dc_ready, capsys):
    w = dc_ready
    same = "#!/bin/sh\n# same\nexit 0\n"
    w.candidate("feat/a", {PRE_COMMIT: same, "a.only": "a\n"})
    w.candidate("feat/b", {PRE_COMMIT: same, "b.only": "b\n"})
    assert _approve(w, dc, "feat/a") == 0
    assert _approve(w, dc, "feat/b") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert _installed(w) == same


def test_an_excluded_approved_candidate_takes_its_hook_with_it(dc, dc_ready, capsys):
    """The branch moves after the rebuild: the next rebuild excludes it (its pin
    no longer matches), and the hook it installed is replaced by main's."""
    w = dc_ready
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# approved\nexit 0\n"})
    assert _approve(w, dc, "feat/h") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    w.candidate("feat/h", {"later.txt": "later\n"})
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert "EXCLUDED: feat/h" in capsys.readouterr().out
    assert _installed(w) == BASE_HOOK


# ── symbolic links (sync-hooks.sh installs what a link points at) ─────────────


def _link(w, repo, path: str, target: str, msg: str) -> str:
    p = repo / path
    p.unlink(missing_ok=True)
    p.symlink_to(target)
    w.git(repo, "add", "-A")
    w.git(repo, "commit", "-q", "-m", msg)
    return w.rev("HEAD", repo)


def test_a_candidate_that_makes_a_hook_a_symlink_is_refused_even_approved(dc, dc_ready, capsys):
    """The link's blob is a path; cp installs the referent's bytes, which a
    second candidate could change without any approval."""
    w = dc_ready
    w.candidate("feat/l", {"scripts/lib/hook_body.sh": "#!/bin/sh\nexit 0\n"})
    _link(w, w.tmp / "wt-feat-l", PRE_COMMIT, "../lib/hook_body.sh", "link the hook")
    assert _approve(w, dc, "feat/l") == 1
    assert "symbolic link" in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_restore_leaves_a_hook_main_turned_into_a_symlink_to_sync(dc, dc_ready, capsys):
    """Restoring a link would install its target PATH as the hook's bytes."""
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.add(dc, "feat/x") == 0, capsys.readouterr()
    w.git(w.up, "checkout", "-q", "main")
    w.commit(w.up, {"scripts/lib/hook_body.sh": "#!/bin/sh\n# via link\nexit 0\n"}, "body")
    _link(w, w.up, PRE_COMMIT, "../lib/hook_body.sh", "main links the hook")
    w.git(w.up, "push", "-q", "origin", "main")
    w.run(dc, "rebuild")
    capsys.readouterr()
    assert _installed(w) == BASE_HOOK, "restore wrote the link's target path"
    # No sync can make a linked hook match HEAD; readiness says why, by name.
    assert w.run(dc, "rebuild") == 1
    assert "pre-commit is a symbolic link at HEAD" in capsys.readouterr().err


@pytest.mark.parametrize("hook_dir", ["scripts/hooks", ".claude/hooks"])
def test_a_candidate_that_makes_a_hook_directory_a_symlink_is_refused(
    dc, dc_ready, capsys, hook_dir
):
    """The changed path is the directory itself, with no trailing slash."""
    w = dc_ready
    w.candidate("feat/d", {"elsewhere/pre-commit": BASE_HOOK})
    wt = w.tmp / "wt-feat-d"
    w.git(wt, "rm", "-rq", "--ignore-unmatch", hook_dir)
    (wt / hook_dir).parent.mkdir(parents=True, exist_ok=True)
    (wt / hook_dir).symlink_to("../elsewhere")
    w.git(wt, "add", "-A")
    w.git(wt, "commit", "-q", "-m", "link the hook directory")
    # Admission names the link; the flag cannot get it in either.
    assert w.add(dc, "feat/d") == 1
    assert f"makes {hook_dir} a symbolic link" in capsys.readouterr().err
    assert _approve(w, dc, "feat/d") == 1
    assert not w.manifest_path.exists()
