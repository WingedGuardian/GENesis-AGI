"""Classification is the whole precision story — pin it against real shapes.

MEASURED on this install 2026-09-05: a naive "branch is ahead of origin/main"
query returned 145 candidates of which ~18 were real (~12% precision), because
this repo squash-merges (``mergeCommitAllowed: false``) so a merged branch tip
is NEVER an ancestor of main and reads permanently ahead. The PR-history join
is what recovers precision, and the ``mergedAt`` time guard is what keeps it
honest: head-ref names are REUSED (35 of 1586 names, one carrying 7 PRs), and
commits land on a branch after its PR merges.

These tests are the shapes that join has to get right. The live acceptance
replay (both known-stranded corpus branches flagged, stage counts matching the
hand measurements) is in the PR body — this file pins the logic that produced it.
"""

from datetime import UTC, datetime, timedelta

from genesis.session_awareness.zero_drop import (
    CLASS_PUSHED_NO_PR,
    CLASS_UNPUSHED,
    DETACHED_KEY_PREFIX,
    PUSH_ABSENT,
    PUSH_BEHIND,
    PUSH_DIVERGED,
    PUSH_EXACT,
    PUSH_UNKNOWN,
    classify_branches,
    classify_worktrees,
    pr_coverage,
    worktree_identity,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
OLD = (NOW - timedelta(days=10)).isoformat()
RECENT = (NOW - timedelta(hours=2)).isoformat()


def _branch(name="feat/x", *, ahead=3, tip="aaa111", date=OLD):
    return {"branch": name, "tip_sha": tip, "ahead": ahead, "behind": 0, "tip_date": date}


def _pr(state, *, merged=None, closed=None, head="feat/x", number=1, oid=None, owner=None):
    return {
        "number": number,
        "headRefName": head,
        "headRefOid": oid,
        "state": state,
        "mergedAt": merged,
        "closedAt": closed,
        "headRepositoryOwnerLogin": owner,
    }


def _run(branches, *, remote=(), prs=(), min_age_hours=12, push=None, owner=None, ancestry=None):
    """Default push state is EXACT — "the tip is on the remote" — because most
    tests here are about the PR join, not about push state. Tests that care
    pass `push=` explicitly."""
    states = {b["branch"]: PUSH_EXACT for b in branches}
    states.update(push or {})
    if remote:
        for name in states:
            if name not in remote:
                states[name] = PUSH_ABSENT
    return classify_branches(
        branches,
        push_states=states,
        prs=list(prs),
        now=NOW,
        min_age_hours=min_age_hours,
        repo_owner=owner,
        ancestry=ancestry,
    )


def test_stage_counts_sum_to_the_ref_total():
    """Suppression you cannot add up is suppression you cannot audit."""
    out = _run(
        [
            _branch("a"),
            _branch("b", ahead=0),
            _branch("c", date=RECENT),
            _branch("d", ahead=None),
            _branch("e"),
        ],
        prs=[_pr("MERGED", merged=(NOW - timedelta(days=1)).isoformat(), head="e")],
    )
    stages = out["stages"]
    assert stages["refs_total"] == 5
    assert sum(v for k, v in stages.items() if k != "refs_total") == 5


def test_squash_merged_branch_is_suppressed_by_the_join():
    """The whole reason the join exists: ahead-count alone says 'stranded'."""
    merged_after = (NOW - timedelta(days=1)).isoformat()
    out = _run([_branch()], prs=[_pr("MERGED", merged=merged_after)])
    assert out["stages"]["covered_merged_pr"] == 1
    assert out["findings"][CLASS_UNPUSHED] == []


def test_merge_that_PREDATES_the_tip_is_still_a_finding():
    """The time guard. A merged PR proves the branch merged ONCE — commits
    landed since, and those commits are in no pipeline. MEASURED: this guard
    cost 1 of 115 suppressions and that one was a TRUE positive."""
    merged_before = (NOW - timedelta(days=30)).isoformat()
    out = _run([_branch(date=OLD)], remote=("feat/x",), prs=[_pr("MERGED", merged=merged_before)])
    assert out["stages"]["flagged_merge_predates_tip"] == 1
    found = out["findings"][CLASS_PUSHED_NO_PR]
    assert [f["branch"] for f in found] == ["feat/x"]
    assert found[0]["details"]["reason"] == "merged_predates_tip"


def test_name_reuse_an_open_pr_on_a_reused_name_still_covers():
    """35 of 1586 head-ref names carry more than one PR. An OPEN PR on the name
    means somebody is looking at that branch right now."""
    out = _run(
        [_branch()],
        prs=[
            _pr("CLOSED", number=1),
            _pr("MERGED", merged=(NOW - timedelta(days=90)).isoformat(), number=2),
            _pr("OPEN", number=3),
        ],
    )
    assert out["stages"]["covered_open_pr"] == 1
    assert out["findings"][CLASS_UNPUSHED] == []


def test_closed_unmerged_pr_is_suppressed_but_counted():
    """A deliberate abandonment, decided AFTER the tip existed."""
    closed_after = (NOW - timedelta(days=1)).isoformat()
    out = _run([_branch()], prs=[_pr("CLOSED", closed=closed_after)])
    assert out["stages"]["suppressed_closed_pr"] == 1
    assert out["findings"][CLASS_PUSHED_NO_PR] == []


def test_a_closed_pr_with_no_closedAt_proves_nothing():
    """The exact mirror of the MERGED rule: an undated close is not evidence.

    Suppressing on a field gh never populated would clear a finding on
    information we did not get, which is the direction that goes silent.
    """
    out = _run([_branch()], prs=[_pr("CLOSED", closed=None)])
    assert out["stages"]["flagged_closed_tip_postdates"] == 1


def test_commits_made_AFTER_a_close_are_not_covered_by_it():
    """The asymmetry this fixes: MERGED had a time guard from the start and
    CLOSED had none, so closing a PR and continuing to commit suppressed the
    branch forever. Closing decides about the content that was IN the PR."""
    closed_before = (NOW - timedelta(days=30)).isoformat()
    out = _run([_branch(date=OLD)], prs=[_pr("CLOSED", closed=closed_before)])
    assert out["stages"]["flagged_closed_tip_postdates"] == 1
    assert out["stages"]["suppressed_closed_pr"] == 0


def test_a_closed_pr_never_covers_commits_that_are_on_NO_remote():
    """MEASURED 2026-09-06: 4 branches on this install, each holding a finished
    change with tests, silently suppressed by a closed PR. Commits that were
    never pushed were never in the PR, so they were never part of the decision
    to abandon it."""
    closed_after = (NOW - timedelta(days=1)).isoformat()
    out = _run(
        [_branch()],
        prs=[_pr("CLOSED", closed=closed_after)],
        push={"feat/x": PUSH_DIVERGED},
    )
    assert out["stages"]["flagged_closed_local_only"] == 1
    assert out["stages"]["suppressed_closed_pr"] == 0
    # Class follows the COMMITS, not the name: a ref of this name is on the
    # remote, but these commits are not, so the finding is `unpushed_branch`.
    found = out["findings"][CLASS_UNPUSHED]
    assert [f["details"]["reason"] for f in found] == ["closed_local_only"]
    # PROVEN, not merely unrefuted. An ABSENT branch reaches the same verdict on
    # weaker grounds, and a reader must be able to tell the two apart.
    assert found[0]["details"]["evidence"]["proof"] == "diverged_from_remote"


def test_merged_row_with_no_merged_at_proves_nothing():
    """gh contract violation. Treating it as covering would suppress a real
    finding on a field that was never populated."""
    out = _run([_branch()], prs=[_pr("MERGED", merged=None)])
    assert out["stages"]["flagged_no_pr"] == 1


def test_young_branch_is_not_stranded_work():
    out = _run([_branch(date=RECENT)])
    assert out["stages"]["too_young"] == 1
    assert out["findings"][CLASS_UNPUSHED] == []


def test_unknown_ahead_count_never_becomes_a_finding():
    """An old git expands %(ahead-behind:) empty. Reporting stranded work on
    evidence we failed to collect is the false-positive direction that teaches
    everyone to ignore the board."""
    out = _run([_branch(ahead=None)])
    assert out["stages"]["ahead_unknown"] == 1
    assert out["findings"] == {CLASS_UNPUSHED: [], CLASS_PUSHED_NO_PR: []}


def test_remote_presence_picks_the_class():
    out = _run([_branch("pushed"), _branch("local")], remote=("pushed",))
    assert [f["branch"] for f in out["findings"][CLASS_PUSHED_NO_PR]] == ["pushed"]
    assert [f["branch"] for f in out["findings"][CLASS_UNPUSHED]] == ["local"]


def test_pr_coverage_ignores_another_branchs_prs():
    """The index is keyed on headRefName; a PR for a different branch must not
    leak coverage onto this one."""
    out = _run([_branch("mine")], prs=[_pr("OPEN", head="theirs")])
    assert out["stages"]["flagged_no_pr"] == 1


def test_pr_coverage_verdicts_are_ordered_by_strength():
    """An OPEN PR covers only a tip the server is KNOWN to hold.

    This used to assert that an open PR beats an old merge unconditionally.
    That was the name join outranking the commits again: an open PR reviews
    what is ON THE REMOTE, so it says nothing about a tip whose presence there
    is unproven. The verdict now depends on push state — which is the point of
    the rework, and the reason the argument is no longer optional.
    """
    old_merge = (NOW - timedelta(days=90)).isoformat()
    tip = NOW - timedelta(days=10)
    rows = [_pr("MERGED", merged=old_merge), _pr("OPEN")]

    for state in (PUSH_EXACT, PUSH_BEHIND):
        assert pr_coverage(rows, tip_date=tip, push_state=state)[0] == "open"
    for state in (PUSH_ABSENT, PUSH_DIVERGED, PUSH_UNKNOWN):
        # Not suppressed. The live open PR is the actionable half, so it is
        # what the reason names — not the merge that predates the tip.
        assert pr_coverage(rows, tip_date=tip, push_state=state)[0] == "local_ahead_of_open_pr"

    assert pr_coverage([], tip_date=tip, push_state=PUSH_EXACT)[0] == "none"


def test_a_non_covering_OPEN_pr_outranks_a_closed_one_in_the_REASON():
    """Both flag, so nothing is suppressed either way — but the reason decides
    where the reader looks first, and a live PR beats an abandoned one."""
    closed_after = (NOW - timedelta(days=1)).isoformat()
    verdict, evidence = pr_coverage(
        [_pr("CLOSED", closed=closed_after, number=1), _pr("OPEN", number=2)],
        tip_date=NOW - timedelta(days=10),
        push_state=PUSH_DIVERGED,
    )
    assert verdict == "local_ahead_of_open_pr"
    assert evidence["pr"] == 2


# ── worktrees ───────────────────────────────────────────────────────────────


def _wt(path="/w/a", *, branch="feat/x", entries=(("M ", "f.py"),), mtime=None, detached=False):
    return {
        "path": path,
        "branch": branch,
        "detached": detached,
        "entries": list(entries),
        "newest_mtime": mtime if mtime is not None else NOW - timedelta(days=2),
    }


def test_dirty_worktree_ages_on_the_FILE_not_the_branch_tip():
    """A worktree with a months-old tip and a two-minute-old edit is somebody
    typing. The tip date says nothing about that."""
    out = classify_worktrees([_wt(mtime=NOW - timedelta(minutes=2))], now=NOW, min_age_hours=6)
    assert out["stages"]["too_young"] == 1
    assert out["findings"] == []


def test_untracked_files_count_as_stranded_work():
    out = classify_worktrees([_wt(entries=[("??", "new_module.py")])], now=NOW, min_age_hours=6)
    assert out["stages"]["flagged_dirty"] == 1
    details = out["findings"][0]["details"]
    assert (details["tracked_changes"], details["untracked_files"]) == (0, 1)


def test_clean_worktree_is_not_a_finding():
    out = classify_worktrees([_wt(entries=[])], now=NOW, min_age_hours=6)
    assert out["stages"]["clean"] == 1
    assert out["findings"] == []


def test_detached_worktree_gets_a_collision_proof_identity():
    """A detached worktree has no branch to key on. ':' is forbidden in a git
    ref name, so this prefix can never collide with a real branch."""
    out = classify_worktrees(
        [_wt(path="/w/detached", branch=None, detached=True)], now=NOW, min_age_hours=6
    )
    key = out["findings"][0]["branch"]
    assert key == f"{DETACHED_KEY_PREFIX}/w/detached"
    assert ":" in key, "the identity must be unrepresentable as a branch name"


def test_worktree_with_no_stat_able_paths_is_still_judged():
    """A deleted path has no mtime. newest_mtime=None must NOT read as
    'infinitely young' and silently drop the finding."""
    out = classify_worktrees(
        [_wt(entries=[(" D", "gone.py")], mtime=None)], now=NOW, min_age_hours=6
    )
    assert out["stages"]["flagged_dirty"] == 1


def test_an_undated_tip_with_a_merged_pr_is_FLAGGED_not_suppressed():
    """We know the branch is ahead; we just cannot date its tip, so we cannot
    tell whether commits landed after the merge. Suppressing there clears a
    finding on evidence we failed to collect — the wrong direction for a
    detector whose worst outcome is a false clean board."""
    merged = (NOW - timedelta(days=30)).isoformat()
    out = _run([_branch(date=None)], prs=[_pr("MERGED", merged=merged)])

    assert out["stages"]["flagged_merged_tip_undated"] == 1
    assert out["stages"]["covered_merged_pr"] == 0
    # The tip IS on the remote (default push state), so the class is
    # `pushed_no_pr`: the work is safe, it is just in no pipeline.
    found = out["findings"][CLASS_PUSHED_NO_PR]
    assert [f["details"]["reason"] for f in found] == ["merged_tip_undated"]


def test_an_undated_tip_with_an_OPEN_pr_is_still_covered():
    """An open PR needs no date to settle the question."""
    out = _run([_branch(date=None)], prs=[_pr("OPEN")])
    assert out["stages"]["covered_open_pr"] == 1


def test_age_gated_branches_are_reported_as_HELD():
    """Held, not absent. The reconciler must be able to tell "I looked and
    chose not to report" from "it is gone" — conflating them resolves rows and
    destroys acknowledgements."""
    out = _run([_branch("young", date=RECENT), _branch("old")])
    assert out["held"] == {"young"}
    assert "old" not in out["held"]


def test_dirty_worktrees_report_held_identities_that_MATCH_the_finding_key():
    """The hold set and the finding row must agree on the identity exactly —
    a key computed one way here and another way there would hold something
    nothing matches, silently restoring resolve-on-absence."""
    young = _wt(path="/w/young", branch="feat/young", mtime=NOW - timedelta(minutes=1))
    old = _wt(path="/w/old", branch="feat/old")
    detached_young = _wt(
        path="/w/dy", branch=None, detached=True, mtime=NOW - timedelta(minutes=1)
    )

    out = classify_worktrees([young, old, detached_young], now=NOW, min_age_hours=6)

    assert out["held"] == {"feat/young", f"{DETACHED_KEY_PREFIX}/w/dy"}
    assert [f["branch"] for f in out["findings"]] == ["feat/old"]
    assert worktree_identity(old) == "feat/old"
    assert worktree_identity(detached_young) == f"{DETACHED_KEY_PREFIX}/w/dy"


def test_an_unknown_ahead_count_is_HELD_not_resolved():
    """The class miss the first pass made: age-gated branches were held and
    unmeasurable ones were not, so a branch whose ahead-count we FAILED to read
    had its finding resolved — clearing it on exactly the evidence we could not
    collect. Both skips mean 'we could not determine'; both must hold."""
    out = _run([_branch("unmeasurable", ahead=None), _branch("gone", ahead=0)])

    assert out["held"] == {"unmeasurable"}, "an unreadable ahead-count must be held"
    assert "gone" not in out["held"], (
        "a branch genuinely no longer ahead has ENDED — resolving it is correct"
    )


def test_a_naive_timestamp_does_not_crash_the_classifier():
    """`now` and the cutoffs are aware; comparing an aware datetime with a naive
    one raises TypeError, which would escape the classifier, escape the branch
    leg, and surface only as a failed sweep. git's committerdate always carries
    an offset — `mergedAt` and replayed fixtures are not ours to assume."""
    naive_tip = (NOW - timedelta(days=10)).replace(tzinfo=None).isoformat()
    naive_merge = (NOW - timedelta(days=30)).replace(tzinfo=None).isoformat()

    out = _run(
        [_branch(date=naive_tip)],
        remote=("feat/x",),
        prs=[_pr("MERGED", merged=naive_merge)],
    )

    assert out["stages"]["flagged_merge_predates_tip"] == 1
    assert [f["branch"] for f in out["findings"][CLASS_PUSHED_NO_PR]] == ["feat/x"]


def test_a_dirty_worktree_finding_carries_an_EXPIRY_key():
    """Without one, `acked_tip_sha` is None, the expiry test compares None to
    None, and an acknowledged worktree stays suppressed through every later
    edit — a permanent mute the ack design refuses to offer."""
    out = classify_worktrees([_wt(entries=[("M ", "a.py")])], now=NOW, min_age_hours=6)
    key = out["findings"][0]["tip_sha"]
    assert key and len(key) == 64, f"expected a full sha256 expiry key, got {key!r}"


def test_the_expiry_key_MOVES_when_the_work_changes_and_not_otherwise():
    """That is the whole contract: an ack survives an unchanged worktree and
    dies the moment anything about the dirty set changes."""
    base = _wt(entries=[("M ", "a.py")], mtime=NOW - timedelta(days=2))
    same = classify_worktrees([base], now=NOW, min_age_hours=6)["findings"][0]["tip_sha"]
    again = classify_worktrees([base], now=NOW, min_age_hours=6)["findings"][0]["tip_sha"]
    assert same == again, "an unchanged worktree must keep its key, or every ack expires"

    for changed in (
        _wt(entries=[("M ", "a.py"), ("??", "b.py")], mtime=NOW - timedelta(days=2)),  # added
        _wt(entries=[("A ", "a.py")], mtime=NOW - timedelta(days=2)),  # status changed
        _wt(entries=[("M ", "a.py")], mtime=NOW - timedelta(days=1)),  # touched
    ):
        key = classify_worktrees([changed], now=NOW, min_age_hours=6)["findings"][0]["tip_sha"]
        assert key != same, f"the key survived a real change: {changed['entries']}"


# ── The evidence hierarchy ───────────────────────────────────────────────────
#
# One test per verdict-table row. The ORDER matters as much as the rows: these
# pin that stronger evidence wins, so a clock can never overrule a SHA.
#
# Origin (MEASURED 2026-09-06, 217 refs / 1665 PRs): the name join treated "a
# PR with this name merged after your tip date" as PROOF the work landed. Five
# branches holding commits that exist on no remote were suppressed by it.


def test_head_oid_matching_the_tip_is_PROOF_the_pr_contained_it():
    """119 of 123 merged-covered branches on this install match exactly."""
    out = _run([_branch(tip="a" * 40)], prs=[_pr("MERGED", merged=OLD, oid="a" * 40)])
    assert out["stages"]["covered_merged_pr"] == 1
    found = out["findings"][CLASS_PUSHED_NO_PR] + out["findings"][CLASS_UNPUSHED]
    assert found == []


def test_sha_proof_BEATS_a_merge_that_predates_the_tip():
    """Evidence ordering, stated as a test. The time guard says "this merge is
    older than your tip, so it cannot vouch for it" — but if the merged head IS
    your tip, the merge contained it and the clock is irrelevant. Without the
    ordering, a correct suppression would become a permanent false finding."""
    long_ago = (NOW - timedelta(days=365)).isoformat()
    out = _run([_branch(tip="b" * 40)], prs=[_pr("MERGED", merged=long_ago, oid="b" * 40)])
    assert out["stages"]["covered_merged_pr"] == 1
    assert out["stages"]["flagged_merge_predates_tip"] == 0


def test_ancestry_covers_a_tip_the_merged_head_contains():
    """A branch left BEHIND what merged: the tip is reachable from the merged
    head, so everything local was in the PR."""
    out = _run(
        [_branch(tip="c" * 40)],
        prs=[_pr("MERGED", merged=OLD, oid="d" * 40)],
        ancestry={f"{'c' * 40}..{'d' * 40}": True},
    )
    assert out["stages"]["covered_merged_pr"] == 1


def test_ancestry_DISPROVING_containment_is_a_finding_not_a_suppression():
    """The merged head does not contain this tip: the PR provably did not carry
    these commits, whatever its name or its merge time say."""
    recent_merge = (NOW - timedelta(days=1)).isoformat()
    out = _run(
        [_branch(tip="c" * 40)],
        prs=[_pr("MERGED", merged=recent_merge, oid="d" * 40)],
        ancestry={f"{'c' * 40}..{'d' * 40}": False},
    )
    assert out["stages"]["flagged_merged_local_only"] == 1
    assert out["stages"]["covered_merged_pr"] == 0


def test_an_UNANSWERABLE_ancestry_flags_with_a_hint_and_never_suppresses():
    """The merged head is not an object we hold (pushed from another machine,
    never fetched), so containment cannot be tested. Suppressing here would
    clear a finding on evidence we could not collect; the finding instead
    carries the one command that resolves it, because GitHub keeps
    refs/pull/<n>/head forever. MEASURED: 3 of 217 refs on this install."""
    recent_merge = (NOW - timedelta(days=1)).isoformat()
    out = _run(
        [_branch(tip="c" * 40)],
        prs=[_pr("MERGED", merged=recent_merge, oid="d" * 40, number=77)],
        ancestry={},  # the pair was never resolvable
    )
    assert out["stages"]["flagged_merge_unconfirmable"] == 1
    found = out["findings"][CLASS_PUSHED_NO_PR]
    hint = found[0]["details"]["evidence"]["resolve_with"]
    assert "refs/pull/77/head" in hint and "merge-base --is-ancestor" in hint


def test_an_open_pr_does_not_cover_commits_that_are_on_no_remote():
    """Kimi's mirror case, and a live one: push, open a PR, keep committing
    locally. The PR reviews what is ON THE REMOTE, so the unpushed commits are
    in no pipeline at all — but the old join read "an OPEN PR exists" and
    suppressed the branch."""
    out = _run([_branch()], prs=[_pr("OPEN", number=42)], push={"feat/x": PUSH_DIVERGED})
    assert out["stages"]["flagged_local_ahead_of_open_pr"] == 1
    assert out["stages"]["covered_open_pr"] == 0
    found = out["findings"][CLASS_UNPUSHED]
    assert found[0]["details"]["evidence"]["pr"] == 42


def test_a_diverged_branch_still_takes_SHA_PROOF_over_the_open_pr_flag():
    """Ordering again, in the direction that would otherwise produce a false
    POSITIVE: a diverged branch whose tip a merged PR provably contained is
    covered, even though an open PR for the same ref does not cover it."""
    out = _run(
        [_branch(tip="e" * 40)],
        prs=[_pr("OPEN", number=1), _pr("MERGED", merged=OLD, oid="e" * 40, number=2)],
        push={"feat/x": PUSH_DIVERGED},
    )
    assert out["stages"]["covered_merged_pr"] == 1
    assert out["stages"]["flagged_local_ahead_of_open_pr"] == 0


def test_an_UNKNOWN_push_state_is_HELD_not_flagged_and_not_resolved():
    """The tip differs from the remote tip and ancestry was unanswerable, so we
    cannot tell ahead from behind. Both a finding and a resolution would be
    claims we cannot support, and the wrong one is silent."""
    out = _run([_branch()], prs=[_pr("OPEN")], push={"feat/x": PUSH_UNKNOWN})
    assert out["stages"]["push_unknown"] == 1
    assert out["held"] == {"feat/x"}
    assert out["findings"][CLASS_UNPUSHED] == []
    assert out["findings"][CLASS_PUSHED_NO_PR] == []


def test_a_branch_merely_BEHIND_the_remote_holds_nothing_local():
    """Someone else pushed on top. Every local commit is on the server, so this
    is a `pushed_no_pr` question at most — never an unpushed-work finding."""
    out = _run([_branch()], push={"feat/x": PUSH_BEHIND})
    assert out["stages"]["flagged_no_pr"] == 1
    assert [f["branch"] for f in out["findings"][CLASS_PUSHED_NO_PR]] == ["feat/x"]
    assert out["findings"][CLASS_UNPUSHED] == []


def test_a_fork_pr_does_not_cover_a_local_branch_of_the_same_name():
    """Head-ref reuse is MEASURED at 35 of 1586 names here, and 9 of 1665 PRs
    come from forks. A contributor's `patch-1` says nothing about ours."""
    out = _run(
        [_branch("patch-1")],
        prs=[_pr("OPEN", head="patch-1", owner="a-contributor")],
        owner="the-maintainer",
    )
    assert out["ignored_forks"] == 1
    assert out["stages"]["covered_open_pr"] == 0
    assert out["stages"]["flagged_no_pr"] == 1


def test_an_unresolved_repo_owner_keeps_every_pr_rather_than_dropping_all():
    """Fail direction: an over-broad join can only SUPPRESS, and suppression is
    visible in the stage counts. Dropping every PR would flag the whole branch
    list at once."""
    out = _run(
        [_branch("patch-1")],
        prs=[_pr("OPEN", head="patch-1", owner="anyone")],
        owner=None,
    )
    assert out["ignored_forks"] == 0
    assert out["stages"]["covered_open_pr"] == 1


def test_stage_counts_still_sum_with_every_new_verdict_present():
    """The audit invariant, re-checked against the widened verdict set: a
    branch must land in exactly ONE terminal stage."""
    recent = (NOW - timedelta(days=1)).isoformat()
    out = _run(
        [
            _branch("proof", tip="a" * 40),
            _branch("disproven", tip="b" * 40),
            _branch("unconfirmable", tip="c" * 40),
            _branch("closed-local", tip="d" * 40),
            _branch("open-ahead", tip="e" * 40),
            _branch("held", tip="f" * 40),
            _branch("nopr", tip="0" * 40),
        ],
        prs=[
            _pr("MERGED", merged=recent, oid="a" * 40, head="proof"),
            _pr("MERGED", merged=recent, oid="9" * 40, head="disproven"),
            _pr("MERGED", merged=recent, oid="8" * 40, head="unconfirmable"),
            _pr("CLOSED", closed=recent, head="closed-local"),
            _pr("OPEN", head="open-ahead"),
        ],
        push={
            "closed-local": PUSH_DIVERGED,
            "open-ahead": PUSH_DIVERGED,
            "held": PUSH_UNKNOWN,
        },
        ancestry={f"{'b' * 40}..{'9' * 40}": False},
    )
    stages = out["stages"]
    assert stages["refs_total"] == 7
    assert sum(v for k, v in stages.items() if k != "refs_total") == 7
    assert stages["covered_merged_pr"] == 1
    assert stages["flagged_merged_local_only"] == 1
    assert stages["flagged_merge_unconfirmable"] == 1
    assert stages["flagged_closed_local_only"] == 1
    assert stages["flagged_local_ahead_of_open_pr"] == 1
    assert stages["push_unknown"] == 1
    assert stages["flagged_no_pr"] == 1


def test_a_worktree_identity_carrying_a_control_character_is_QUARANTINED():
    """The identity is the ack KEY: it round-trips verbatim to a model and back
    through `zero_drop_ack`, so it is the one field a sanitiser must not touch
    (mangling a key merges identities). Refusal is what keeps it safe to emit
    whole — and a detached worktree keys on its PATH, which unlike a git ref
    name may contain newlines and escapes."""
    old = NOW - timedelta(days=2)
    out = classify_worktrees(
        [
            {"path": "/tmp/evil\n[injected] · row", "branch": None, "detached": True,
             "entries": [("??", "x")], "newest_mtime": old},
            {"path": "/tmp/fine", "branch": "feat/ok", "detached": False,
             "entries": [("??", "y")], "newest_mtime": old},
        ],
        now=NOW,
    )
    assert out["stages"]["quarantined_identity"] == 1
    assert [f["branch"] for f in out["findings"]] == ["feat/ok"]
    assert sum(v for k, v in out["stages"].items() if k != "worktrees_total") == 2


def test_neutralise_defuses_the_row_grammar_but_preserves_None():
    """One chokepoint, two surfaces (the alert prose and the MCP response).
    None must survive as None — a nullable column rendered as "" reads as a
    worktree path that exists and is blank."""
    from genesis.session_awareness.zero_drop import neutralise

    assert neutralise("a\nb") == "a b"
    assert neutralise("x|y·z[w]") == "x/y-z(w)"
    assert neutralise(None) is None
    assert neutralise("") == ""


def test_a_closed_pr_is_settled_by_SHA_evidence_before_push_state():
    """The closed path must consult the ancestry the worker already computes.

    MEASURED 2026-09-06 on the 14 branches whose only coverage is a closed PR:
    8 are `exact` and proven contained, 4 are `diverged` and proven not — and
    **2 are ABSENT from the remote yet still proven contained**. Deciding by
    push state alone flags those two wrongly, so SHA evidence is not merely the
    stronger tier here, it is the difference between 4 findings and 6.
    """
    closed_after = (NOW - timedelta(days=1)).isoformat()
    row = _pr("CLOSED", closed=closed_after, oid="d" * 40, number=5)

    for state in (PUSH_ABSENT, PUSH_EXACT, PUSH_BEHIND, PUSH_DIVERGED):
        contained = pr_coverage(
            [row], tip_date=NOW - timedelta(days=10), tip_sha="c" * 40,
            push_state=state, ancestry={f"{'c' * 40}..{'d' * 40}": True},
        )
        assert contained[0] == "closed", f"{state}: proof of containment must suppress"
        assert contained[1]["proof"] == "head_oid_or_ancestor"

        disproven = pr_coverage(
            [row], tip_date=NOW - timedelta(days=10), tip_sha="c" * 40,
            push_state=state, ancestry={f"{'c' * 40}..{'d' * 40}": False},
        )
        assert disproven[0] == "closed_local_only", f"{state}: disproof must flag"


def test_an_ABSENT_branch_is_not_suppressed_by_a_clock_alone():
    """`not local_only` was doing duty for "the tip is on the server", and
    ABSENT — 159 of 221 refs here — sat in the gap between them. A five-valued
    push state does not reduce to one boolean and its negation."""
    recent = (NOW - timedelta(days=1)).isoformat()
    no_sha = _pr("MERGED", merged=recent, oid=None)
    assert pr_coverage([no_sha], tip_date=NOW - timedelta(days=10),
                       push_state=PUSH_ABSENT)[0] == "merged_predates_tip"
    for state in (PUSH_EXACT, PUSH_BEHIND):
        assert pr_coverage([no_sha], tip_date=NOW - timedelta(days=10),
                           push_state=state)[0] == "merged"


def test_every_value_in_the_EVIDENCE_blob_is_structurally_constrained():
    """`details` reaches a model UNNEUTRALISED, and that is only safe because
    nothing free-form is ever folded into it.

    A security review flagged this as unneutralised by OMISSION rather than by
    a considered argument — the sibling display fields (`worktree_path`, the
    `degraded` blob) are sanitised, and nothing recorded why this one need not
    be. The reason is real: every value is a validated 40-hex SHA, an int, a
    GitHub timestamp, a URL, or one of a closed set of literals. But a reason
    nobody wrote down is a reason the next change deletes, so it is pinned
    here: fold a raw gh error string into `evidence` and this goes red.
    """
    import re as _re

    recent = (NOW - timedelta(days=1)).isoformat()
    sha, head = "c" * 40, "d" * 40
    cases = [
        ([_pr("MERGED", merged=recent, oid=head)], {f"{sha}..{head}": False}, PUSH_EXACT),
        ([_pr("MERGED", merged=recent, oid=head)], {}, PUSH_ABSENT),
        ([_pr("MERGED", merged=recent, oid=sha)], {}, PUSH_EXACT),
        ([_pr("CLOSED", closed=recent, oid=head)], {f"{sha}..{head}": False}, PUSH_EXACT),
        ([_pr("CLOSED", closed=recent, oid=head)], {f"{sha}..{head}": True}, PUSH_EXACT),
        ([_pr("CLOSED", closed=recent)], {}, PUSH_DIVERGED),
        ([_pr("CLOSED", closed=recent)], {}, PUSH_ABSENT),
        ([_pr("OPEN")], {}, PUSH_DIVERGED),
        ([_pr("OPEN")], {}, PUSH_EXACT),
    ]
    ALLOWED_LITERALS = {
        "head_oid",
        "ancestor_of_merged_head",
        "merged_after_tip",
        "head_oid_or_ancestor",
        "not_an_ancestor_of_the_closed_head",
        "diverged_from_remote",
        "unconfirmed",
    }
    seen = 0
    for prs, ancestry, push in cases:
        _, evidence = pr_coverage(
            prs, tip_date=NOW - timedelta(days=10), tip_sha=sha,
            push_state=push, ancestry=ancestry,
        )
        for key, value in evidence.items():
            seen += 1
            if isinstance(value, int) or value is None:
                continue
            assert isinstance(value, str), f"{key}: unexpected type {type(value)}"
            constrained = (
                _re.fullmatch(r"[0-9a-f]{40}", value)  # a validated object name
                or value in ALLOWED_LITERALS  # a closed set of literals
                or _re.fullmatch(r"[0-9T:+\-.]{10,32}Z?", value)  # a GH timestamp
                or value.startswith("https://")  # a GitHub URL
                or value.startswith("git fetch origin refs/pull/")  # the hint
            )
            assert constrained, f"free-form text reached the evidence blob: {key}={value!r}"
    assert seen >= 10, "the cases must actually exercise the evidence paths"


def test_neutralise_strips_invisible_and_reordering_characters():
    """`\\s+` collapses whitespace; it does not touch the Cf category.

    Bidi overrides and zero-width characters render as nothing, or reorder what
    surrounds them, so text can display to a human or a model as something
    other than what it is. A filesystem path may legally carry them (Linux
    forbids only NUL and '/'), which is exactly the untrusted source here.
    Deleted rather than substituted: unlike `|` and `[` they have no readable
    form worth preserving.
    """
    from genesis.session_awareness.zero_drop import neutralise

    assert neutralise("a‮b") == "ab"  # RIGHT-TO-LEFT OVERRIDE
    assert neutralise("a​b") == "ab"  # ZERO WIDTH SPACE
    assert neutralise("a⁦b⁩c") == "abc"  # isolates
    assert neutralise("a﻿b") == "ab"  # BOM / zero-width no-break
    assert neutralise("feat/ordinary-name") == "feat/ordinary-name"


def test_an_identity_carrying_a_REORDERING_character_is_quarantined_too():
    """The identity is returned VERBATIM because it is the ack key, so refusal
    is the only lever — a key cannot be cleaned without merging identities.
    That makes the refusal set, not the sanitiser, the security boundary for
    this field, and it must cover everything the sanitiser would have removed.
    """
    old = NOW - timedelta(days=2)
    out = classify_worktrees(
        [
            {"path": "/w/a‮b", "branch": None, "detached": True,
             "entries": [("??", "x")], "newest_mtime": old},
            {"path": "/w/z​z", "branch": None, "detached": True,
             "entries": [("??", "y")], "newest_mtime": old},
            {"path": "/w/fine", "branch": "feat/ok", "detached": False,
             "entries": [("??", "z")], "newest_mtime": old},
        ],
        now=NOW,
    )
    assert out["stages"]["quarantined_identity"] == 2
    assert [f["branch"] for f in out["findings"]] == ["feat/ok"]
