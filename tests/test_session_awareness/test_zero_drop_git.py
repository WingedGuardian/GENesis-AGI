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
    list_remote_heads,
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
    out = await list_remote_heads(str(repo))
    assert "error" in out


async def test_ls_remote_reads_the_live_remote(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    out = await list_remote_heads(str(repo))
    assert set(out["heads"]) == {"main"}
    # The SHA is the point of this atom: local-tip vs remote-tip is the
    # direct test for commits that exist nowhere but this machine.
    head = _git(repo, "rev-parse", "main").stdout.strip()
    assert out["heads"]["main"] == head


async def test_a_successful_ls_remote_that_parses_to_NOTHING_is_an_error(repo):
    """rc=0 with no branch refs. The rc check above cannot see this (that test
    uses a repo with no remote, which fails rc≠0 and never reaches the guard),
    and an empty set does not fail neutrally: it reclassifies EVERY branch as
    never-pushed, and class is part of a finding's identity — so it forks rows
    instead of correcting them."""
    from genesis.session_awareness import zero_drop_git as g

    async def _empty(argv, timeout):
        return 0, "", ""

    assert "error" in await g.list_remote_heads(str(repo), runner=_empty)

    async def _unexpected_shape(argv, timeout):
        return 0, "deadbeef\trefs/tags/v1\n", ""

    assert "error" in await g.list_remote_heads(str(repo), runner=_unexpected_shape)


@pytest.mark.parametrize(
    "base",
    ["origin/%(objectname)", "origin/(x)", "origin/a b", "origin/a\tb", "", "x" * 300],
)
async def test_an_unsafe_base_ref_is_REFUSED_not_sanitised(repo, base):
    """`base` is spliced into a git FORMAT STRING, where `%(...)` is a
    directive — and a git ref name may legally contain `%`, `(` and `)`. A name
    carrying a directive would inject extra fields into output the classifier
    trusts to be four TAB-separated columns. A ref name that cannot be safely
    formatted is not a value we accept."""
    from genesis.session_awareness import zero_drop_git as g

    assert not g.is_safe_base_ref(base)
    out = await g.list_local_branches(str(repo), base=base)
    assert "error" in out and "unsafe base ref" in out["error"]


@pytest.mark.parametrize(
    "base", ["origin/main", "main", "origin/release/1.2", "upstream/feat_x-1@a"]
)
def test_ordinary_base_refs_are_accepted(base):
    """The guard must not reject the names a real fork actually uses."""
    from genesis.session_awareness import zero_drop_git as g

    assert g.is_safe_base_ref(base)


async def test_a_dirty_symlink_is_dated_by_ITSELF_not_by_its_target(repo, tmp_path):
    """`os.stat` follows symlinks. A dirty entry that is a symlink would then be
    dated by a file OUTSIDE the worktree — dating this worktree's work by
    something unrelated, and disclosing that file's mtime into a finding."""
    import os

    from genesis.session_awareness.zero_drop_worker import _observe_worktrees

    outside = tmp_path / "ancient.txt"
    outside.write_text("x\n")
    os.utime(outside, (0, 0))  # 1970
    os.symlink(outside, repo / "link.txt")

    out = await _observe_worktrees(str(repo))
    obs = next(o for o in out["observations"] if o["path"] == str(repo))

    assert obs["entries"], "the symlink should show as untracked"
    assert obs["newest_mtime"] is not None
    assert obs["newest_mtime"].year > 2000, (
        f"the entry was dated by its TARGET, not itself: {obs['newest_mtime']}"
    )


async def test_observe_worktrees_SKIPS_a_real_prunable_worktree(repo, tmp_path):
    """The worker-level assertion, against real git rather than a fake listing:
    a worktree whose directory is gone is ABSENT, not unreadable. Treating it as
    unreadable would freeze the whole dirty class on every sweep from then on."""
    import shutil

    from genesis.session_awareness.zero_drop_worker import _observe_worktrees

    wt = tmp_path / "doomed"
    _git(repo, "worktree", "add", "-q", "-b", "doomed", str(wt))
    shutil.rmtree(wt)

    out = await _observe_worktrees(str(repo))

    assert out["prunable"] == 1
    assert out["errors"] == [], "a gone worktree must not be reported as unreadable"
    assert out["held"] == set(), "and it must not be quarantined either — it is absent"
    assert [o["path"] for o in out["observations"]] == [str(repo)]


async def test_base_ref_resolution_returns_None_when_there_is_no_origin_HEAD(repo):
    """None, not the fallback string. Returning "origin/main" on failure makes
    "resolved to origin/main" and "guessed origin/main" the same value, so the
    caller cannot say which happened — and a wrong base inflates every
    ahead-count."""
    from genesis.session_awareness.zero_drop_worker import _resolve_base_ref

    assert await _resolve_base_ref(str(repo)) is None  # fresh repo: no origin/HEAD

    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert await _resolve_base_ref(str(repo)) == "origin/trunk"


# ── is_ancestor / count_non_merge_commits: the SHA-evidence atoms ────────────
#
# These exist because "a PR with this name merged" is a heuristic while "this
# commit is reachable from that one" is a fact. Both are THREE-valued: the
# unanswerable case is what stops a missing object from being read as proof.


async def test_is_ancestor_answers_yes_no_and_UNANSWERABLE(repo):
    """rc 0/1 are answers; rc 128 (object not in this repo) is NOT.

    Folding the missing-object case into False would turn "I cannot see that
    commit" into "those commits are stranded" — a confident finding built on
    absent evidence, which is the class of defect this module exists to avoid.
    """
    from genesis.session_awareness.zero_drop_git import is_ancestor

    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "later")
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert await is_ancestor(str(repo), first, second) is True
    assert await is_ancestor(str(repo), second, first) is False
    # A well-formed SHA this repository has never heard of.
    assert await is_ancestor(str(repo), "0" * 40, second) is None


@pytest.mark.parametrize(
    "bad",
    [
        "--upload-pack=touch /tmp/x",
        "HEAD",
        "main",
        "abc123",
        "",
        "0" * 39,
        "0" * 41,
        "G" * 40,
        # A wrong TYPE, not just a wrong value. `headRefOid` is JSON from gh and
        # a tip_sha is parsed from git output, so null is a shape either can
        # take. A validator that raises on it is worse than one that rejects
        # it: the exception escapes the classifier and kills the WHOLE sweep,
        # which is the one outcome a detector cannot afford.
        None,
        123,
        ["a" * 40],
    ],
)
async def test_is_ancestor_REFUSES_anything_that_is_not_a_full_sha(repo, bad):
    """Both arguments reach subprocess argv, and both arrive from OUTSIDE this
    repository — `headRefOid` from gh's JSON, remote tips from ls-remote. A
    refused argument returns the unanswerable None, never an answer, and never
    an exception."""
    from genesis.session_awareness.zero_drop_git import count_non_merge_commits, is_ancestor

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert await is_ancestor(str(repo), bad, head) is None
    assert await is_ancestor(str(repo), head, bad) is None
    assert await count_non_merge_commits(str(repo), bad, head) is None
    assert await count_non_merge_commits(str(repo), head, bad) is None


async def test_count_non_merge_commits_ignores_merges(repo):
    """A branch whose only local-only commit is a MERGE holds no unique work.

    Counting it would flag a branch that has merged the base in but has nothing
    of its own — noise sitting on top of the real signal, in the one class
    where a false positive costs an acknowledgement.
    """
    from genesis.session_awareness.zero_drop_git import count_non_merge_commits

    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-q", "-m", "side work")
    side = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-")
    (repo / "trunk.txt").write_text("trunk\n")
    _git(repo, "add", "trunk.txt")
    _git(repo, "commit", "-q", "-m", "trunk work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merged = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # One real commit on the side branch, not reachable from base.
    assert await count_non_merge_commits(str(repo), base, side) == 1
    # From the merge commit's side: the merge itself carries no unique work.
    assert await count_non_merge_commits(str(repo), side, merged) == 1  # trunk work only
    assert await count_non_merge_commits(str(repo), merged, merged) == 0
    assert await count_non_merge_commits(str(repo), "0" * 40, merged) is None
    assert await count_non_merge_commits(str(repo), "not-a-sha", merged) is None


# ── rc=0 with nothing parsed is a FAILURE, on every enumerator ───────────────


@pytest.mark.parametrize(
    "fn_name,kind",
    [
        ("list_local_branches", "for-each-ref"),
        ("list_remote_heads", "ls-remote"),
        ("list_worktrees", "worktree list"),
    ],
)
@pytest.mark.parametrize("payload", ["", "not a record at all\n", "   \n\n"])
async def test_rc0_with_nothing_parsed_freezes_the_class(repo, fn_name, kind, payload):
    """An empty result does not fail neutrally — it RESOLVES the whole class.

    Each of these feeds a class that reconciles, so whatever is not returned is
    treated as gone. rc=0-and-empty therefore clears every open and acknowledged
    finding in that class at once: silent, confident, complete. And empty cannot
    be a true observation — a repository always has at least one local branch,
    at least one branch on its remote, and at least one worktree.

    The guard used to exist on ls-remote ONLY. `worktree list` accepted
    unparseable garbage as a clean empty set.
    """
    from genesis.session_awareness import zero_drop_git as g

    async def _runner(argv, timeout):
        return 0, payload, ""

    out = await getattr(g, fn_name)(str(repo), runner=_runner)
    assert "error" in out, f"{kind} accepted an empty parse as success"
    # Either refusal is correct — an unreadable line and a result that parsed
    # to nothing both mean "the output was not what we think it is", and both
    # freeze the class. What must never happen is a clean empty success.
    assert any(m in out["error"] for m in ("empty set", "unrecognised", "unparseable"))


async def test_a_real_worktree_listing_still_parses(repo, tmp_path):
    """The guard-the-guard: a refusal that also refuses valid input is worse
    than no refusal, because it freezes the class permanently."""
    from genesis.session_awareness.zero_drop_git import list_worktrees

    out = await list_worktrees(str(repo))
    assert "error" not in out
    assert len(out["worktrees"]) >= 1


async def test_every_git_argv_declines_optional_locks(repo, monkeypatch, tmp_path):
    """READ-ONLY is a stated REQUIREMENT of this module, and it was not true.

    MEASURED on git 2.43: a plain `git status --porcelain` rewrites .git/index
    whenever a tracked file's mtime has moved — it refreshes the stat cache and
    takes index.lock to do it. Across ~161 worktrees that is 161 lock
    acquisitions per sweep contending with whatever else is running.

    Asserted over EVERY atom rather than the one that was caught, because the
    obligation is the kind a call site forgets one instance of.
    """
    from genesis.session_awareness import zero_drop_git as g

    seen: list[list[str]] = []

    async def _spy(argv, timeout):
        seen.append(argv)
        return 1, "", "stopped"

    await g.list_local_branches(str(repo), runner=_spy)
    await g.list_remote_heads(str(repo), runner=_spy)
    await g.list_worktrees(str(repo), runner=_spy)
    await g.worktree_status(str(repo), runner=_spy)
    await g.is_ancestor(str(repo), "a" * 40, "b" * 40, runner=_spy)
    await g.count_non_merge_commits(str(repo), "a" * 40, "b" * 40, runner=_spy)

    assert len(seen) == 6, "every atom must have issued exactly one command"
    for argv in seen:
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv, f"writes to the repo it reads: {argv}"


async def test_the_push_probe_is_BUDGETED_like_its_sibling(monkeypatch):
    """An unbounded probe loop under a GLOBAL flock starves every later sweep.

    Per-call timeouts do not bound a loop: N diverged branches x 2 calls x 30s
    is an unbounded wall-clock, and the sweep holds `detector.lock` throughout.
    A security review caught that the ancestry probe was capped and this one was
    not. Over-budget branches read as UNKNOWN, which the classifier HOLDS —
    never reported, never resolved — so the ceiling costs findings, not truth.
    """
    from genesis.session_awareness import zero_drop_worker as w

    calls = 0

    async def _counting(root, a, b, runner=None):
        nonlocal calls
        calls += 1
        return False  # never an ancestor, so each branch takes the second call too

    async def _count_zero(root, a, b, runner=None):
        return 5

    monkeypatch.setattr(w, "is_ancestor", _counting)
    monkeypatch.setattr(w, "count_non_merge_commits", _count_zero)

    branches = [{"branch": f"b{i}", "tip_sha": f"{i:040x}"} for i in range(50)]
    heads = {f"b{i}": "f" * 40 for i in range(50)}

    out = await w._resolve_push_states("/repo", branches, heads, budget=5)
    assert calls == 5, "the probe must stop at the budget, not run once per branch"
    states = out["push_states"]
    assert sum(1 for v in states.values() if v == "diverged") == 5
    assert sum(1 for v in states.values() if v == "unknown") == 45
    assert len(states) == 50, "every branch still gets a state — none is silently dropped"
