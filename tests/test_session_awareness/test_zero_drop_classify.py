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
    OPAQUE_KEY_PREFIX,
    PUSH_ABSENT,
    PUSH_BEHIND,
    PUSH_DIVERGED,
    PUSH_EXACT,
    PUSH_UNKNOWN,
    classify_branches,
    classify_worktrees,
    index_prs_by_head,
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
    detached_young = _wt(path="/w/dy", branch=None, detached=True, mtime=NOW - timedelta(minutes=1))

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


def test_an_UNSAFE_identity_is_DERIVED_not_dropped():
    """The identity is the ack KEY: it round-trips verbatim to a model and back
    through `zero_drop_ack`, so it is the one field a sanitiser must not touch —
    mangling a key merges identities. A detached worktree keys on its PATH,
    which unlike a git ref name may contain newlines and escapes.

    The earlier resolution was to REFUSE such a value, and this test pinned
    that. It was wrong: a refused identity entered neither `present` nor
    `held`, so `apply_sweep` resolved the row and the uncommitted work it named
    left the board silently (Codex P2, PR #1794). Refusal is only right when
    the alternative is a key that LIES, and an opaque digest is neither. So the
    row survives, keyed on something safe, and the readable form travels as
    `worktree_path` — which every surface renders through `neutralise`.
    """
    old = NOW - timedelta(days=2)
    out = classify_worktrees(
        [
            {
                "path": "/tmp/evil\n[injected] · row",
                "branch": None,
                "detached": True,
                "entries": [("??", "x")],
                "newest_mtime": old,
            },
            {
                "path": "/tmp/fine",
                "branch": "feat/ok",
                "detached": False,
                "entries": [("??", "y")],
                "newest_mtime": old,
            },
        ],
        now=NOW,
    )
    assert out["opaque_identities"] == 1
    identities = [f["branch"] for f in out["findings"]]
    assert len(identities) == 2, "the unsafe worktree must still produce a finding"
    opaque = [i for i in identities if i.startswith(OPAQUE_KEY_PREFIX)]
    assert len(opaque) == 1
    # The KEY carries nothing that could deceive a reader...
    assert "\n" not in opaque[0] and "[" not in opaque[0]
    # ...and the readable path is still on the row as evidence.
    paths = {f["worktree_path"] for f in out["findings"]}
    assert "/tmp/evil\n[injected] · row" in paths
    # The terminal stages still sum: the opaque count is META, not a stage.
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
            [row],
            tip_date=NOW - timedelta(days=10),
            tip_sha="c" * 40,
            push_state=state,
            ancestry={f"{'c' * 40}..{'d' * 40}": True},
        )
        assert contained[0] == "closed", f"{state}: proof of containment must suppress"
        assert contained[1]["proof"] == "head_oid_or_ancestor"

        disproven = pr_coverage(
            [row],
            tip_date=NOW - timedelta(days=10),
            tip_sha="c" * 40,
            push_state=state,
            ancestry={f"{'c' * 40}..{'d' * 40}": False},
        )
        assert disproven[0] == "closed_local_only", f"{state}: disproof must flag"


def test_an_ABSENT_branch_is_not_suppressed_by_a_clock_alone():
    """`not local_only` was doing duty for "the tip is on the server", and
    ABSENT — 159 of 221 refs here — sat in the gap between them. A five-valued
    push state does not reduce to one boolean and its negation."""
    recent = (NOW - timedelta(days=1)).isoformat()
    no_sha = _pr("MERGED", merged=recent, oid=None)
    assert (
        pr_coverage([no_sha], tip_date=NOW - timedelta(days=10), push_state=PUSH_ABSENT)[0]
        == "merged_predates_tip"
    )
    for state in (PUSH_EXACT, PUSH_BEHIND):
        assert (
            pr_coverage([no_sha], tip_date=NOW - timedelta(days=10), push_state=state)[0]
            == "merged"
        )


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
            prs,
            tip_date=NOW - timedelta(days=10),
            tip_sha=sha,
            push_state=push,
            ancestry=ancestry,
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
            {
                "path": "/w/a‮b",
                "branch": None,
                "detached": True,
                "entries": [("??", "x")],
                "newest_mtime": old,
            },
            {
                "path": "/w/z​z",
                "branch": None,
                "detached": True,
                "entries": [("??", "y")],
                "newest_mtime": old,
            },
            {
                "path": "/w/fine",
                "branch": "feat/ok",
                "detached": False,
                "entries": [("??", "z")],
                "newest_mtime": old,
            },
        ],
        now=NOW,
    )
    assert out["opaque_identities"] == 2
    keys = [f["branch"] for f in out["findings"]]
    assert len(keys) == 3, "both reordering identities must still produce findings"
    assert sum(1 for k in keys if k.startswith(OPAQUE_KEY_PREFIX)) == 2
    assert len(set(keys)) == 3, "two unsafe identities must not collapse onto one key"
    assert "feat/ok" in keys


# ── Class B: a NAME is not an IDENTITY (Codex round 1, PR #1794) ─────────────


def test_a_RENAMED_branch_is_still_covered_by_its_merged_pr_head_sha():
    """The finding that says this PR's own thesis was unfinished.

    Rename a branch after its PR merged and the historical `headRefName` no
    longer matches anything local, so a name-only lookup hands `pr_coverage`
    an EMPTY row list — and the SHA proof sitting in `headRefOid`, the
    strongest evidence this module recognises, never gets to speak. The
    detector then files a stranded finding for work that demonstrably shipped.
    """
    tip = "f" * 40
    out = _run(
        [_branch(name="feat/renamed", tip=tip)],
        prs=[_pr("MERGED", merged=OLD, head="feat/ORIGINAL-name", oid=tip, number=7)],
    )
    assert out["stages"]["covered_merged_pr"] == 1
    assert out["stages"]["flagged_no_pr"] == 0
    assert out["findings"][CLASS_PUSHED_NO_PR] == []


def test_an_exact_head_sha_covers_an_OPEN_pr_on_a_ref_that_no_longer_exists():
    """Same rename, PR still open. Push state is ABSENT — there is no remote
    ref of the NEW name to consult — but `headRefOid` IS the commit GitHub
    holds as that PR's head, which proves the tip is on the server directly.
    Push state is tier 3 evidence read off a name; the SHA is tier 1.
    """
    tip = "c" * 40
    out = _run(
        [_branch(name="feat/renamed", tip=tip)],
        prs=[_pr("OPEN", head="feat/gone", oid=tip, number=9)],
        push={"feat/renamed": PUSH_ABSENT},
    )
    assert out["stages"]["covered_open_pr"] == 1
    assert out["stages"]["flagged_local_ahead_of_open_pr"] == 0


def test_an_open_pr_at_a_DIFFERENT_sha_still_flags_an_absent_branch():
    """The control for the test above, and the one that keeps the widening
    honest: without an exact SHA match an ABSENT branch is still not covered
    by an open PR, exactly as before.

    The PR must MATCH BY NAME. An earlier version of this test used a head ref
    the branch does not carry, so the lookup returned nothing, `pr_coverage`
    answered "none", and the OPEN branch this test exists to constrain was
    never entered at all — it would have passed with the SHA comparison
    deleted. `flagged_no_pr` was the tell: a genuinely non-covering open PR
    produces `flagged_local_ahead_of_open_pr`, which is what is asserted now.
    """
    out = _run(
        [_branch(name="feat/renamed", tip="c" * 40)],
        prs=[_pr("OPEN", head="feat/renamed", oid="d" * 40, number=9)],
        push={"feat/renamed": PUSH_ABSENT},
    )
    assert out["stages"]["covered_open_pr"] == 0
    assert out["stages"]["flagged_local_ahead_of_open_pr"] == 1
    assert out["stages"]["flagged_no_pr"] == 0


def test_a_fork_pr_cannot_cover_a_local_branch_through_the_SHA_index_either():
    """The fork filter governs BOTH maps. It would be defensible to keep forks
    in the SHA map — an exact head OID is the same commit, not a name
    coincidence — but that opens a new SUPPRESSION path on third-party data,
    and the wrong direction here is the silent one."""
    tip = "b" * 40
    out = _run(
        [_branch(name="feat/x", tip=tip)],
        prs=[_pr("MERGED", merged=OLD, head="their-branch", oid=tip, owner="contributor")],
        owner="us",
    )
    assert out["stages"]["covered_merged_pr"] == 0
    assert out["stages"]["flagged_no_pr"] == 1
    assert out["ignored_forks"] == 1


def test_a_pr_matching_by_BOTH_name_and_sha_is_passed_once():
    """The ordinary case is both indexes returning the same row. Deduplicated
    by object identity: `number` is untrusted input and may be missing, which
    would collapse every unnumbered row onto a single None."""
    tip = "a" * 40
    index, _ = index_prs_by_head([_pr("MERGED", merged=OLD, head="feat/x", oid=tip)])
    rows = index.for_branch("feat/x", tip)
    assert len(rows) == 1


def test_name_rows_LEAD_so_the_sha_index_can_only_add_evidence():
    """A branch that was never renamed must see exactly the sequence it saw
    before the SHA index existed — the union adds, it never reorders."""
    tip = "a" * 40
    named = _pr("MERGED", merged=OLD, head="feat/x", oid="e" * 40, number=1)
    by_sha = _pr("MERGED", merged=OLD, head="feat/other", oid=tip, number=2)
    index, _ = index_prs_by_head([by_sha, named])
    assert [p["number"] for p in index.for_branch("feat/x", tip)] == [1, 2]


def test_no_PRODUCTION_caller_looks_a_branch_up_in_ONE_index():
    """The LOCK behind `for_branch`, because a docstring is a convention.

    The whole Class B defect was a call site reaching for the name map
    directly, and nothing stopped it. `for_branch` is only a chokepoint while
    every caller is forced through it, so this walks the AST of the modules
    that consume a `PrIndex` and fails on any attribute access to a raw index.
    A new lookup written next year fails here until it goes through the union —
    which is what a convention could not do.

    Scoped to `src/`: a test may read `by_name` to assert what the index HOLDS,
    which is a statement about the data structure, not a lookup.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src/genesis/session_awareness"
    offenders = []
    for path in (root / "zero_drop.py", root / "zero_drop_worker.py"):
        tree = ast.parse(path.read_text(), str(path))
        inside = {
            node
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef) and cls.name == "PrIndex"
            for node in ast.walk(cls)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in ("by_name", "by_head_sha")
                and node not in inside
            ):
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    assert not offenders, (
        "a raw index lookup bypasses PrIndex.for_branch, which is what gated "
        f"SHA proof behind a name join in the first place: {offenders}"
    )


def test_a_pr_with_no_head_ref_NAME_is_still_reachable_by_its_head_sha():
    """A row is dropped from the name map when it carries no usable name — it
    used to be dropped from the join entirely, discarding an immutable SHA."""
    tip = "a" * 40
    index, _ = index_prs_by_head([{"number": 3, "headRefOid": tip, "state": "MERGED"}])
    assert index.by_name == {}
    assert [p["number"] for p in index.for_branch(None, tip)] == [3]


def test_ALL_closed_prs_are_scanned_before_a_negative_verdict_is_chosen():
    """Head-ref names are REUSED — MEASURED 35 of 1586 names here, one carrying
    7 PRs — so one branch can carry several unrelated closed PRs, and the
    listing order is evidence about nothing. Returning on the first disproven
    row let a stale PR override SHA proof sitting in a later one."""
    tip = "a" * 40
    verdict, evidence = pr_coverage(
        [
            _pr("CLOSED", closed=OLD, oid="9" * 40, number=1),
            _pr("CLOSED", closed=OLD, oid=tip, number=2),
        ],
        tip_date=NOW - timedelta(days=10),
        tip_sha=tip,
        push_state=PUSH_DIVERGED,
        ancestry={f"{tip}..{'9' * 40}": False},
    )
    assert verdict == "closed"
    assert evidence["pr"] == 2


def test_every_closed_pr_disproven_still_flags_and_names_the_FIRST():
    """The control. Scanning further must not weaken the negative verdict when
    nothing positive turns up, and the reported row is stable."""
    tip = "a" * 40
    verdict, evidence = pr_coverage(
        [
            _pr("CLOSED", closed=OLD, oid="9" * 40, number=1),
            _pr("CLOSED", closed=OLD, oid="8" * 40, number=2),
        ],
        tip_date=NOW - timedelta(days=10),
        tip_sha=tip,
        push_state=PUSH_DIVERGED,
        ancestry={f"{tip}..{'9' * 40}": False, f"{tip}..{'8' * 40}": False},
    )
    assert verdict == "closed_local_only"
    assert evidence["pr"] == 1


def test_one_branch_in_TWO_worktrees_gets_two_independently_ackable_rows():
    """`git worktree add --force` checks out a branch that is already checked
    out elsewhere. Keyed on the branch alone the two collapse onto one
    identity, `apply_sweep` keeps the first sighting, and the second worktree's
    uncommitted work can never be acknowledged or tracked (Codex P2, #1794)."""
    rows = classify_worktrees(
        [
            _wt(path="/w/one", entries=[("M ", "a.py")]) | {"branch_duplicated": True},
            _wt(path="/w/two", entries=[("M ", "b.py")]) | {"branch_duplicated": True},
        ],
        now=NOW,
        min_age_hours=6,
    )
    identities = [f["branch"] for f in rows["findings"]]
    assert len(set(identities)) == 2
    assert all(i.startswith("feat/x:") for i in identities)
    assert ":" in identities[0], "an identity must stay unrepresentable as a ref"
    # The path is carried where it can be rendered safely; the KEY is a digest.
    assert {f["worktree_path"] for f in rows["findings"]} == {"/w/one", "/w/two"}


def test_discriminating_an_identity_never_widens_the_QUARANTINE():
    """The false suppression this discriminator introduced before it was caught.

    A worktree PATH may legally contain a newline — this subsystem's own parser
    was redesigned around exactly that — and `classify_worktrees` quarantines an
    identity carrying a control character. A quarantined worktree lands in
    NEITHER `present` NOR `held`, and `apply_sweep` resolves anything absent
    from both, so splicing a raw path into a branch-keyed identity could resolve
    a live finding about uncommitted work.

    The invariant, stated exactly: discriminating changes whether a worktree is
    quarantined for nobody. A hex digest is safe by construction, so only the
    BRANCH NAME can ever decide.
    """
    hostile = "/w/line\nbreak\x07"
    dup = _wt(path=hostile) | {"branch_duplicated": True}
    solo = _wt(path=hostile)

    out_dup = classify_worktrees([dup], now=NOW, min_age_hours=6)
    out_solo = classify_worktrees([solo], now=NOW, min_age_hours=6)

    assert out_dup["opaque_identities"] == 0
    assert out_solo["opaque_identities"] == 0
    assert out_dup["stages"]["flagged_dirty"] == out_solo["stages"]["flagged_dirty"] == 1
    ident = out_dup["findings"][0]["branch"]
    assert "\n" not in ident and "\x07" not in ident, ident
    # And the control: a hostile BRANCH is still quarantined either way, so the
    # digest did not smuggle an unsafe identity through.
    bad_branch = _wt(branch="feat/‮x") | {"branch_duplicated": True}
    bad_out = classify_worktrees([bad_branch], now=NOW)
    assert bad_out["opaque_identities"] == 1
    assert bad_out["findings"], "an unsafe BRANCH name must still produce a finding"


def test_the_duplicate_digest_is_FULL_never_shortened():
    """Two paths colliding on a prefix would transfer one worktree's
    acknowledgement to another worktree's work — the rule `dirty_state_key`
    states, for the same reason."""
    import hashlib

    ident = worktree_identity({"path": "/w/one", "branch": "b", "branch_duplicated": True})
    assert ident == "b:" + hashlib.sha256(b"/w/one").hexdigest()
    assert len(ident.split(":", 1)[1]) == 64


def test_a_branch_checked_out_ONCE_keeps_its_BARE_identity():
    """The discriminator is CONDITIONAL and this is why: the identity is the
    ACK KEY, so applying a path suffix unconditionally would change every
    existing worktree identity at once and silently expire every
    acknowledgement ever written. MEASURED 2026-09-12: 0 of 162 worktrees on
    this install share a branch, so the conditional form is a no-op here."""
    rows = classify_worktrees([_wt(path="/w/only")], now=NOW, min_age_hours=6)
    assert [f["branch"] for f in rows["findings"]] == ["feat/x"]


def test_the_HELD_key_of_a_duplicated_worktree_matches_its_finding_key():
    """The invariant that makes the stamp live on the LISTING rather than being
    derived per consumer: a hold computed one way and a finding the other holds
    a key nothing matches, silently restoring resolve-on-absence."""
    young = _wt(path="/w/y", mtime=NOW - timedelta(minutes=1)) | {"branch_duplicated": True}
    old = _wt(path="/w/o") | {"branch_duplicated": True}

    out = classify_worktrees([young, old], now=NOW, min_age_hours=6)

    assert out["held"] == {worktree_identity(young)}
    assert [f["branch"] for f in out["findings"]] == [worktree_identity(old)]
    assert out["held"].isdisjoint({f["branch"] for f in out["findings"]})


# ── Family F: a timestamp the code assumed could not be in the FUTURE ───────
#
# Seven time comparisons exist in this subsystem; four HOLD or DEBOUNCE, and
# only those can wedge — a future timestamp answers "too new" forever. The
# other three fail toward FLAGGING, which is the direction a detector is
# allowed to be wrong in, so they are deliberately left alone.


def test_a_branch_dated_in_the_FUTURE_is_judged_not_held_forever():
    """Git accepts a future commit date. Held, the branch is neither reported
    nor resolved until wall time catches up — which for a year-ahead stamp
    means never, so stranded work nobody is told about (Codex P2, PR #1794).

    Resolved to an EXISTING state rather than a new one: a future tip is read
    exactly like an unparseable one, which this classifier already documents as
    "judged on its merits rather than excused".
    """
    future = (NOW + timedelta(days=365)).isoformat()
    out = _run([_branch(date=future)])
    assert out["stages"]["too_young"] == 0, "a future tip must not be held forever"
    assert out["stages"]["flagged_no_pr"] == 1


def test_a_branch_dated_MOMENTS_ahead_is_still_young():
    """The control, and the reason there is a tolerance at all: a commit made
    seconds ago can carry a timestamp a hair ahead of `now` through ordinary
    clock drift, and flagging THAT would be a false positive on the freshest
    work in the tree."""
    just_ahead = (NOW + timedelta(seconds=30)).isoformat()
    out = _run([_branch(date=just_ahead)])
    assert out["stages"]["too_young"] == 1
    assert out["stages"]["flagged_no_pr"] == 0


def test_a_worktree_whose_mtime_is_in_the_FUTURE_is_still_judged():
    """The sibling gate. A restored snapshot or a backwards clock step yields a
    future mtime, and this gate HOLDS its worktree — so the uncommitted work
    stays off the board for as long as the wrong date stands."""
    out = classify_worktrees([_wt(mtime=NOW + timedelta(days=400))], now=NOW, min_age_hours=6)
    assert out["stages"]["too_young"] == 0
    assert out["stages"]["flagged_dirty"] == 1
    assert out["held"] == set()


def test_a_worktree_edited_MOMENTS_ago_is_still_young():
    """Its control: ordinary drift must not turn somebody's live typing into a
    finding."""
    out = classify_worktrees([_wt(mtime=NOW + timedelta(seconds=20))], now=NOW, min_age_hours=6)
    assert out["stages"]["too_young"] == 1
    assert out["stages"]["flagged_dirty"] == 0


def test_an_UNMEASURED_branch_is_reported_as_such_not_merely_held():
    """The blocker a fresh-context audit found, and the worst kind: silent.

    Holding is TWO facts. "Do not resolve this row" — every hold site had that
    right. And "this run did not MEASURE this branch", which is the detector's
    own blindness signal. Only the first was recorded.

    The consequence is the stale confident zero this whole subsystem exists to
    prevent. A sweep whose ancestry probes all hit the budget or the wall-clock
    deadline holds every affected branch; `apply_sweep` correctly resolves
    nothing; both classes still APPLY (with an empty present-set), so `frozen`
    comes back empty, `coverage` reads "all classes swept", `degraded` stays
    empty, `status` is "ok" and `blind` is False. The board announces a clean
    full sweep of refs it never looked at.

    `too_young` is deliberately NOT unmeasured: that branch was looked at and
    judged too recent to report. Wiring a judgement into the blindness alarm
    would make the alarm permanent furniture on any repo with recent work.
    """
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=30)).isoformat()
    fresh = now.isoformat()

    out = classify_branches(
        [
            {"branch": "a", "tip_sha": "a" * 40, "ahead": None, "tip_date": old},
            {"branch": "b", "tip_sha": "b" * 40, "ahead": 3, "tip_date": old},
            {"branch": "c", "tip_sha": "c" * 40, "ahead": 3, "tip_date": fresh},
        ],
        push_states={"b": PUSH_UNKNOWN},
        prs=[],
        now=now,
        min_age_hours=12,
    )

    assert out["held"] == {"a", "b", "c"}, "all three are held — that part was right"
    assert out["unmeasured"] == {"ahead_unknown": 1, "push_unknown": 1}, (
        "only the branches we FAILED to measure are blindness; the age-gated "
        f"one is a judgement: {out['unmeasured']}"
    )


def test_every_branch_hold_site_declares_whether_it_MEASURED():
    """The lock, not the instance.

    Three hold sites existed and all three forgot to record the second fact.
    Fixing them one by one leaves the next one free to forget again, which is
    how this class has already recurred once in this file's history.

    So holding routes through `_hold(name, stage, measured=...)`, where the
    declaration is a REQUIRED keyword — there is no spelling of the call that
    omits it. This walks the AST and fails on any direct `held.add(...)` inside
    `classify_branches`, so a hold site written next year cannot reintroduce
    the silent variant. The same argument the module already makes for routing
    every git invocation through `_git()`.
    """
    import ast
    import pathlib

    from genesis.session_awareness import zero_drop as _zd

    src = pathlib.Path(_zd.__file__).read_text()
    tree = ast.parse(src, _zd.__file__)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "classify_branches"
    )

    # The chokepoint's OWN body is the one legitimate `held.add` — excluding it
    # is not a carve-out that weakens the lock, it is the difference between
    # "route through the helper" and "the helper may not exist". (Caught by
    # running this test: its first version flagged `_hold` itself.)
    helper = next(n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n.name == "_hold")
    inside_helper = {id(n) for n in ast.walk(helper)}

    offenders = [
        f"line {n.lineno}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "held"
        and id(n) not in inside_helper
    ]
    assert not offenders, (
        "a branch hold must go through `_hold(..., measured=...)` so it cannot "
        f"forget to declare blindness; direct held.add at {offenders}"
    )

    holds = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_hold"
    ]
    assert len(holds) >= 3, f"expected every hold site routed through _hold, saw {len(holds)}"
    for call in holds:
        assert any(kw.arg == "measured" for kw in call.keywords), (
            f"_hold at line {call.lineno} omits `measured=` — the whole point"
        )
