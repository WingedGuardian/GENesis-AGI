"""The git atoms are parsed against REAL git output, never a hand-made fixture.

Hand-rolled parsing of a tool's output is this repo's most reliably-wrong
pattern: a fixture written from memory encodes the shape you BELIEVE git emits,
so the test and the bug agree with each other. These tests drive a real
temporary repository through the real ``git`` binary, including the shapes that
break naive parsers — a path with a space, a non-ASCII path, a rename with its
origin field, and an untracked file.

The porcelain default output C-QUOTES any path with a space, a quote or a
non-ASCII byte; ``-z`` emits it raw. That is why the atom uses ``-z``, and this
file is what proves the difference matters.
"""

import subprocess

import pytest

from genesis.session_awareness.zero_drop_git import (
    list_local_branches,
    list_remote_branch_names,
    list_worktrees,
    parse_status_z,
    worktree_status,
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "T")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-qm", "seed")
    return r


async def test_status_parses_paths_that_break_naive_parsers(repo):
    (repo / "a file with spaces.txt").write_text("x\n")
    (repo / "ünïcode.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add tricky names")

    (repo / "a file with spaces.txt").write_text("modified\n")
    _git(repo, "mv", "ünïcode.txt", "renamed ünïcode.txt")
    (repo / "untracked thing.py").write_text("x\n")

    out = await worktree_status(str(repo))
    assert "error" not in out, out
    paths = {p for _xy, p in out["entries"]}

    assert "a file with spaces.txt" in paths, "a space-containing path was mangled"
    assert "renamed ünïcode.txt" in paths, "a non-ASCII path was mangled"
    assert "untracked thing.py" in paths, "untracked work is stranded work — it counts"
    assert "ünïcode.txt" not in paths, (
        "a rename's ORIGIN field must be consumed, not reported as its own entry"
    )
    assert any(xy.strip().startswith("R") for xy, _ in out["entries"])


async def test_porcelain_default_would_have_mangled_those_paths(repo):
    """The MEASUREMENT behind the -z choice, not an assumption about it."""
    (repo / "a file with spaces.txt").write_text("x\n")
    default = _git(repo, "status", "--porcelain").stdout
    assert '"a file with spaces.txt"' in default, (
        "git no longer quotes spaced paths — the -z rationale needs re-deriving"
    )
    nul = _git(repo, "status", "--porcelain", "-z").stdout
    assert "a file with spaces.txt\0" in nul and '"' not in nul


async def test_clean_worktree_reports_no_entries(repo):
    out = await worktree_status(str(repo))
    assert out == {"entries": []}


async def test_status_on_a_nonexistent_path_is_an_error_not_a_clean_read(tmp_path):
    """The whole degraded-leg design rests on this: a failed status must NOT
    be indistinguishable from a clean worktree, or a vanished worktree would
    silently resolve its findings."""
    out = await worktree_status(str(tmp_path / "does-not-exist"))
    assert "error" in out


def test_parse_status_z_counts_what_it_could_not_read():
    """Never guess at an unrecognised record — and never silently DROP it.

    Dropping was the original behaviour and it failed in the one direction a
    detector cannot afford: a garbled status stream shrank to zero entries,
    which reads as a perfectly clean worktree. The count is what lets the
    caller tell "nothing changed" from "I could not see".
    """
    entries, unparsed = parse_status_z("M  ok.py\0garbage\0?? new.py\0")
    assert entries == [("M ", "ok.py"), ("??", "new.py")]
    assert unparsed == 1
    assert parse_status_z("") == ([], 0)


async def test_unreadable_status_output_is_an_error_not_a_clean_worktree(repo, monkeypatch):
    """The rc check catches a failed CALL; this catches a successful call whose
    OUTPUT we cannot read. Both must degrade the class."""
    from genesis.session_awareness import zero_drop_git as g

    async def _garbled(argv, timeout):
        return 0, "not\0porcelain\0at\0all\0", ""

    out = await g.worktree_status(str(repo), runner=_garbled)
    assert "error" in out and "unparseable" in out["error"]


async def test_unreadable_ref_line_fails_the_whole_branch_leg(repo):
    """The branch classes reconcile against a COMPLETE candidate set. A quietly
    short one resolves findings for branches that were simply never listed."""
    from genesis.session_awareness import zero_drop_git as g

    async def _garbled(argv, timeout):
        return 0, "only-one-field\n", ""

    out = await g.list_local_branches(str(repo), runner=_garbled)
    assert "error" in out and "unparseable" in out["error"]


async def test_a_prunable_worktree_is_reported_as_prunable(repo, tmp_path):
    """MEASURED on git 2.43: deleting a worktree's DIRECTORY leaves the
    registration behind with a `prunable` marker, and `git -C <gone> status`
    fails rc=128. Reading that as "unreadable" would freeze the whole dirty
    class on every sweep from then on — one stale registration, permanent
    blindness."""
    import shutil

    wt = tmp_path / "doomed"
    _git(repo, "worktree", "add", "-q", "-b", "doomed", str(wt))
    shutil.rmtree(wt)

    out = await list_worktrees(str(repo))
    entry = next(w for w in out["worktrees"] if w["path"] == str(wt))
    assert entry["prunable"], f"git no longer marks this prunable: {out['worktrees']}"

    dead = await worktree_status(str(wt))
    assert "error" in dead, "the status call on a gone worktree must still fail"

    live = next(w for w in out["worktrees"] if w["path"] == str(repo))
    assert not live["prunable"]


async def test_branch_sweep_reports_ahead_counts_against_the_base(repo):
    """One for-each-ref gives the whole candidate set with ahead/behind —
    no per-branch rev-list. Requires git >= 2.41 for %(ahead-behind:)."""
    _git(repo, "checkout", "-qb", "feat/ahead")
    (repo / "new.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    _git(repo, "checkout", "-q", "main")

    out = await list_local_branches(str(repo), base="main")
    assert "error" not in out, out
    by_name = {b["branch"]: b for b in out["branches"]}
    assert by_name["feat/ahead"]["ahead"] == 1
    assert by_name["feat/ahead"]["behind"] == 0
    assert by_name["main"]["ahead"] == 0
    assert by_name["feat/ahead"]["tip_sha"] and by_name["feat/ahead"]["tip_date"]


async def test_branch_sweep_against_a_missing_base_never_reports_zero_ahead(repo):
    """An unknown base must read as UNKNOWN, never as 'nothing on this branch'
    — the classifier drops unknowns rather than clearing findings on them."""
    out = await list_local_branches(str(repo), base="origin/nope")
    assert "error" in out or all(b["ahead"] is None for b in out["branches"]), out


async def test_branch_names_cannot_contain_a_tab_or_space(repo):
    """The MEASUREMENT behind the TAB-separated ref format: git itself refuses
    a name that would break the split."""
    for bad in ["has space", "has\ttab"]:
        assert _git(repo, "check-ref-format", "--branch", bad, check=False).returncode != 0


async def test_worktree_list_reports_path_and_branch(repo, tmp_path):
    wt = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "feat/linked", str(wt))

    out = await list_worktrees(str(repo))
    assert "error" not in out, out
    by_path = {w["path"]: w for w in out["worktrees"]}
    assert by_path[str(repo)]["branch"] == "main"
    assert by_path[str(wt)]["branch"] == "feat/linked"


async def test_detached_worktree_has_no_branch(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wt = tmp_path / "detached"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), head)

    out = await list_worktrees(str(repo))
    entry = next(w for w in out["worktrees"] if w["path"] == str(wt))
    assert entry["branch"] is None
    assert entry["detached"] is True


async def test_ls_remote_without_a_remote_is_an_error_not_an_empty_set(repo):
    """An empty set would classify every pushed branch as never-pushed. The
    worker freezes both branch classes on this error instead."""
    out = await list_remote_branch_names(str(repo))
    assert "error" in out


async def test_ls_remote_reads_the_live_remote(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    out = await list_remote_branch_names(str(repo))
    assert out["names"] == {"main"}


async def test_a_successful_ls_remote_that_parses_to_NOTHING_is_an_error(repo):
    """rc=0 with no branch refs. The rc check above cannot see this (that test
    uses a repo with no remote, which fails rc≠0 and never reaches the guard),
    and an empty set does not fail neutrally: it reclassifies EVERY branch as
    never-pushed, and class is part of a finding's identity — so it forks rows
    instead of correcting them."""
    from genesis.session_awareness import zero_drop_git as g

    async def _empty(argv, timeout):
        return 0, "", ""

    assert "error" in await g.list_remote_branch_names(str(repo), runner=_empty)

    async def _unexpected_shape(argv, timeout):
        return 0, "deadbeef\trefs/tags/v1\n", ""

    assert "error" in await g.list_remote_branch_names(str(repo), runner=_unexpected_shape)
