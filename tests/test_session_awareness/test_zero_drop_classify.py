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


def _pr(state, *, merged=None, head="feat/x", number=1):
    return {"number": number, "headRefName": head, "state": state, "mergedAt": merged}


def _run(branches, *, remote=(), prs=(), min_age_hours=12):
    return classify_branches(
        branches,
        remote_names=set(remote),
        prs=list(prs),
        now=NOW,
        min_age_hours=min_age_hours,
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
    out = _run([_branch()], prs=[_pr("CLOSED")])
    assert out["stages"]["suppressed_closed_pr"] == 1
    assert out["findings"][CLASS_UNPUSHED] == []


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
    old_merge = (NOW - timedelta(days=90)).isoformat()
    tip = NOW - timedelta(days=10)
    assert pr_coverage([_pr("MERGED", merged=old_merge), _pr("OPEN")], tip_date=tip)[0] == "open"
    assert pr_coverage([_pr("CLOSED"), _pr("MERGED", merged=old_merge)], tip_date=tip)[0] == (
        "merged_predates_tip"
    )
    assert pr_coverage([], tip_date=tip)[0] == "none"


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
    found = out["findings"][CLASS_UNPUSHED]
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
