"""The ``--check-pr`` no-change note: a push after a review that changed no file.

A contributor can force-push a commit whose tree is identical to the one the
reviewers already saw (a message-only rewrite, an empty commit) and then say the
finding is fixed. The note says so at pickup, with no delivery marker needed.

Every case drives ``_no_change_note`` with a synthetic budget and the guard's
existing seams, so nothing here touches GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_budget as rb  # noqa: E402


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location(
        "git_push_guard_no_change_note", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _fresh_files_memo(guard):
    guard._reset_pr_files_cache()
    yield
    guard._reset_pr_files_cache()


REPO = "o/r"
BASE = "b" * 40
REVIEWED = "1" * 40  # the head the reviewers saw
REWRITE = "2" * 40  # same tree as REVIEWED, new message
MERGE = "3" * 40  # a merge of main on top of REWRITE
MAIN = "4" * 40  # main's tip, the merge's second parent
CODE = "5" * 40  # a real change: a different tree
TREE = "a" * 40
TREE_CODE = "c" * 40
TREE_MERGE = "d" * 40
PR_FILE = "tests/test_thing.py"
BASE_TIP = "c0" * 20  # the base branch tip now


def _budget(head, commits, marks, unresolved=()):
    return {
        "status": "ok",
        "current_head": head,
        "commits": [
            {"sha": sha, "tree": tree, "parents": parents} for sha, tree, parents in commits
        ],
        "marks": [{"head": h, "at": at} for h, at in marks],
        "unresolved_clean_heads": [{"head": h, "at": at} for h, at in unresolved],
    }


def _seams(
    monkeypatch,
    *,
    infos=None,
    merge_files=None,
    pr_files=(PR_FILE,),
    head=None,
    reviewed_files=(PR_FILE,),
):
    # The head the note re-reads before firing across a merge; unset means an
    # empty read, which never equals a real head.
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", head or "")
    monkeypatch.setenv("_TEST_GH_COMMIT_INFO", json.dumps(infos or {}))
    # The base tip, and the reviewed head's own contribution over it: what the
    # reviewed head changed, which a stepped-over merge must not touch either.
    monkeypatch.setenv("_TEST_GH_BASE_OID", BASE_TIP)
    contrib = {
        f"{BASE_TIP[:12]}..{REVIEWED[:12]}": {
            "mb": BASE,
            "status": "ahead",
            "files": [{"filename": f} for f in reviewed_files],
        }
    }
    for (left, right), files in (merge_files or {}).items():
        contrib[f"{left[:12]}..{right[:12]}"] = {
            "mb": left,
            "status": "ahead",
            "files": [{"filename": f} for f in files],
        }
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", json.dumps(contrib))
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", "\n".join(json.dumps({"filename": f}) for f in pr_files)
    )


# The reviewed head was force-pushed out, so it is not in the PR's commit list:
# its tree comes from the commits API.
_REVIEWED_INFO = {REVIEWED: {"sha": REVIEWED, "tree": TREE, "parents": [BASE]}}


def test_a_message_only_rewrite_after_a_review_prints_the_note(guard, monkeypatch):
    _seams(monkeypatch, infos=_REVIEWED_INFO)
    budget = _budget(REWRITE, [(REWRITE, TREE, [BASE])], [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None
    assert REWRITE[:12] in note and REVIEWED[:12] in note
    assert "changed no file" in note


def test_the_note_survives_a_later_merge_of_main(guard, monkeypatch):
    """#4611's real shape: rewrite, then a merge of main that touches no PR file."""
    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(REWRITE, MERGE): ["src/other.py"]},
        head=MERGE,
    )
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None and REWRITE[:12] in note


def test_a_plain_merge_of_main_over_the_reviewed_head_is_not_flagged(guard, monkeypatch):
    """The wrong fix the spec names: walking back over a merge reaches the
    reviewed head itself after every routine catch-up. That is not a rewrite."""
    _seams(monkeypatch, infos=_REVIEWED_INFO, merge_files={(REVIEWED, MERGE): ["src/other.py"]})
    budget = _budget(
        MERGE,
        [(REVIEWED, TREE, [BASE]), (MERGE, TREE_MERGE, [REVIEWED, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_head_that_moved_during_the_report_prints_nothing(guard, monkeypatch):
    """The PR's files are read after the budget; if the head moved since, they
    may describe a newer head than the one judged, so the note stays silent."""
    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(REWRITE, MERGE): ["src/other.py"]},
        head=CODE,
    )
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_stale_files_memo_cannot_hide_a_merge_that_touches_the_pr(guard, monkeypatch):
    """Earlier report rows fill the files memo against the head THEY verified,
    which can be older than the budget's. The note must read the files fresh: a
    memo that leaves out the file the merge touched would step over that merge."""
    import os

    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(REWRITE, MERGE): ["src/added_by_new_head.py"]},
        pr_files=(PR_FILE, "src/added_by_new_head.py"),
        head=MERGE,
    )
    key = ("7", REPO, os.environ.get("_TEST_GH_PR_FILES"))
    guard._PR_FILES_CACHE[key] = [PR_FILE]  # the older head's list
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_merge_that_touches_a_pr_file_stops_the_walk(guard, monkeypatch):
    """Code added while resolving a merge (an evil merge) must not hide behind it."""
    _seams(monkeypatch, infos=_REVIEWED_INFO, merge_files={(REWRITE, MERGE): [PR_FILE]})
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_real_change_after_the_review_is_not_flagged(guard, monkeypatch):
    _seams(monkeypatch, infos=_REVIEWED_INFO)
    budget = _budget(CODE, [(CODE, TREE_CODE, [BASE])], [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_clean_re_review_of_the_rewritten_head_does_not_silence_it(guard, monkeypatch):
    """#4611 as it stands: a reviewer re-reviewed the rewritten head, and the
    note's point (the push claimed as a fix changed no file) still holds. The
    head's own mark is skipped as a candidate, never used as R."""
    _seams(monkeypatch, infos=_REVIEWED_INFO)
    budget = _budget(
        REWRITE,
        [(REWRITE, TREE, [BASE])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00"), (REWRITE, "2026-11-02T00:00:00+00:00")],
    )
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None and REVIEWED[:12] in note


def test_a_codex_only_review_of_a_pushed_out_head_still_counts(guard, monkeypatch):
    """The budget drops a clean comment whose head left the PR; the note reads
    those heads from ``unresolved_clean_heads`` instead, by their short sha."""
    _seams(monkeypatch, infos={REVIEWED[:10]: _REVIEWED_INFO[REVIEWED]})
    budget = _budget(
        REWRITE,
        [(REWRITE, TREE, [BASE])],
        [],
        unresolved=[(REVIEWED[:10], "2026-11-01T00:00:00+00:00")],
    )
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None and REVIEWED[:12] in note


def test_anything_unreadable_prints_nothing(guard, monkeypatch):
    # The reviewed head's tree cannot be read.
    _seams(monkeypatch, infos={})
    budget = _budget(REWRITE, [(REWRITE, TREE, [BASE])], [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    assert guard._no_change_note("7", REPO, budget) is None
    # The PR's files cannot be read, so no merge can be stepped over.
    monkeypatch.setenv("_TEST_GH_COMMIT_INFO", json.dumps(_REVIEWED_INFO))
    monkeypatch.setenv("_TEST_GH_PR_FILES", "__error__")
    guard._reset_pr_files_cache()
    merged = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, merged) is None
    # A budget that is not ok.
    assert guard._no_change_note("7", REPO, {"status": "unknown"}) is None


MERGE2 = "6" * 40
MAIN2 = "7" * 40
TREE_MERGE2 = "e" * 40


def test_one_more_merge_after_a_re_review_does_not_silence_it(guard, monkeypatch):
    """#4611 one push later: the rewritten-then-merged head was re-reviewed, then
    main was merged again. The newest earlier review is now the merge head, which
    is not the reference that matters: the walk must still find the rewrite."""
    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(MERGE, MERGE2): ["src/a.py"], (REWRITE, MERGE): ["src/b.py"]},
        head=MERGE2,
    )
    budget = _budget(
        MERGE2,
        [
            (REWRITE, TREE, [BASE]),
            (MERGE, TREE_MERGE, [REWRITE, MAIN]),
            (MERGE2, TREE_MERGE2, [MERGE, MAIN2]),
        ],
        [(REVIEWED, "2026-11-01T00:00:00+00:00"), (MERGE, "2026-11-02T00:00:00+00:00")],
    )
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None and REVIEWED[:12] in note and REWRITE[:12] in note


def test_a_late_re_review_of_an_older_head_does_not_hide_a_rewrite(guard, monkeypatch):
    older = "8" * 40
    infos = {
        **_REVIEWED_INFO,
        older: {"sha": older, "tree": "f" * 40, "parents": [BASE]},
    }
    _seams(monkeypatch, infos=infos)
    budget = _budget(
        REWRITE,
        [(REWRITE, TREE, [BASE])],
        # REVIEWED was reviewed first; the older head's late review is newest.
        [(REVIEWED, "2026-11-01T00:00:00+00:00"), (older, "2026-11-03T00:00:00+00:00")],
    )
    note = guard._no_change_note("7", REPO, budget)
    assert note is not None and REVIEWED[:12] in note


def test_a_rename_into_a_pr_file_stops_the_walk(guard, monkeypatch):
    _seams(monkeypatch, infos=_REVIEWED_INFO)
    contrib = {
        f"{REWRITE[:12]}..{MERGE[:12]}": {
            "mb": REWRITE,
            "status": "ahead",
            "files": [{"filename": "src/new_name.py", "previous_filename": PR_FILE}],
        }
    }
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", json.dumps(contrib))
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_a_merge_at_the_compare_file_cap_stops_the_walk(guard, monkeypatch):
    many = [f"src/f{i}.py" for i in range(guard._COMPARE_FILE_CAP)]
    _seams(monkeypatch, infos=_REVIEWED_INFO, merge_files={(REWRITE, MERGE): many})
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_the_walk_stops_at_its_step_cap(guard, monkeypatch):
    """A chain of merges longer than the cap prints nothing rather than walking on."""
    n = guard._NO_CHANGE_MAX_STEPS + 1
    merges = [f"{i:x}".rjust(40, "9") for i in range(n)]
    commits = [(REWRITE, TREE, [BASE])]
    merge_files = {}
    below = REWRITE
    for i, m in enumerate(merges):
        commits.append((m, f"{i:x}".rjust(40, "0"), [below, MAIN]))
        merge_files[(below, m)] = ["src/other.py"]
        below = m
    _seams(monkeypatch, infos=_REVIEWED_INFO, merge_files=merge_files)
    budget = _budget(below, commits, [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    assert guard._no_change_note("7", REPO, budget) is None
    # One merge fewer is within the cap, and the same history fires.
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", merges[-2])
    budget = _budget(merges[-2], commits[:-1], [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    assert guard._no_change_note("7", REPO, budget) is not None


def test_the_time_bound_gives_up_silently(guard, monkeypatch):
    _seams(monkeypatch, infos=_REVIEWED_INFO)
    budget = _budget(REWRITE, [(REWRITE, TREE, [BASE])], [(REVIEWED, "2026-11-01T00:00:00+00:00")])
    ticks = iter([0.0, guard._NO_CHANGE_BUDGET_S + 1])
    assert guard._no_change_note("7", REPO, budget, clock=lambda: next(ticks)) is None


def test_the_rounds_row_hands_back_the_budget(guard, monkeypatch):
    monkeypatch.delenv("_TEST_ROUNDS_ROW", raising=False)
    fake = {"status": "unknown", "errors": ["x"]}
    monkeypatch.setattr(guard._review_budget, "evaluate_pr", lambda *a, **k: fake)
    out: list = []
    guard._rounds_row("7", REPO, budget_out=out)
    assert out == []  # only an ok budget is handed back
    ok = {"status": "ok", "count": 0, "rounds": []}
    monkeypatch.setattr(guard._review_budget, "evaluate_pr", lambda *a, **k: ok)
    monkeypatch.setattr(guard, "_format_rounds", lambda r: "fmt")
    import review_remaining

    monkeypatch.setattr(review_remaining, "remaining_report", lambda *a, **k: None)
    guard._rounds_row("7", REPO, budget_out=out)
    assert out == [ok]


def test_the_budget_records_clean_comments_on_heads_no_longer_in_the_pr():
    pushed_out = "9" * 40
    comment = {
        "login": rb.CODEX_REVIEW_BOT,
        "type": "Bot",
        "body": f"Codex Review: Didn't find any major issues.\nReviewed commit: `{pushed_out[:10]}`",
        "created_at": "2026-07-01T00:00:00Z",
    }
    got = rb.evaluate_evidence(
        current_head=REWRITE,
        commit_heads=(REWRITE,),
        reviews=(),
        issue_comments=(comment,),
        changed_files=("src/x.py",),
        cutover="2026-06-01T00:00:00+00:00",
    )
    assert got["status"] == "ok", got
    assert got["unresolved_clean_heads"] == [
        {"head": pushed_out[:10], "at": "2026-07-01T00:00:00+00:00"}
    ]
    assert got["marks"] == []  # still not a mark: round counting is unchanged


def test_a_merge_that_drops_a_reviewed_file_stops_the_walk(guard, monkeypatch):
    """Round-1 finding (Codex P2, Devin): the reviewed head changed two files; a
    merge restored one of them to the base, so it left the PR's current file
    list. The merge changed reviewed code, and must not be stepped over."""
    dropped = "src/dropped.py"
    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(REWRITE, MERGE): [dropped, "src/main_side.py"]},
        pr_files=(PR_FILE,),  # dropped.py is no longer in the PR's diff
        reviewed_files=(PR_FILE, dropped),
        head=MERGE,
    )
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def test_an_unreadable_reviewed_contribution_prints_nothing_across_a_merge(guard, monkeypatch):
    _seams(
        monkeypatch,
        infos=_REVIEWED_INFO,
        merge_files={(REWRITE, MERGE): ["src/main_side.py"]},
        head=MERGE,
    )
    monkeypatch.setenv("_TEST_GH_BASE_OID", "")  # base tip unreadable
    budget = _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )
    assert guard._no_change_note("7", REPO, budget) is None


def _merged_budget():
    return _budget(
        MERGE,
        [(REWRITE, TREE, [BASE]), (MERGE, TREE_MERGE, [REWRITE, MAIN])],
        [(REVIEWED, "2026-11-01T00:00:00+00:00")],
    )


def test_a_missing_or_capped_reviewed_contribution_prints_nothing(guard, monkeypatch):
    _seams(
        monkeypatch, infos=_REVIEWED_INFO, merge_files={(REWRITE, MERGE): ["src/x.py"]}, head=MERGE
    )
    contrib = json.loads(os.environ["_TEST_GH_CONTRIBUTION"])
    reviewed_key = f"{BASE_TIP[:12]}..{REVIEWED[:12]}"
    missing = {k: v for k, v in contrib.items() if k != reviewed_key}
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", json.dumps(missing))
    assert guard._no_change_note("7", REPO, _merged_budget()) is None
    capped = dict(contrib)
    capped[reviewed_key] = {
        "mb": BASE,
        "status": "ahead",
        "files": [{"filename": f"src/f{i}.py"} for i in range(guard._COMPARE_FILE_CAP)],
    }
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", json.dumps(capped))
    assert guard._no_change_note("7", REPO, _merged_budget()) is None


def test_a_reviewed_rename_source_counts_as_a_reviewed_file(guard, monkeypatch):
    """The reviewed head renamed old.py to new.py; a merge touching old.py changed
    reviewed code, so the walk must not step over it."""
    _seams(
        monkeypatch, infos=_REVIEWED_INFO, merge_files={(REWRITE, MERGE): ["src/old.py"]}, head=MERGE
    )
    contrib = json.loads(os.environ["_TEST_GH_CONTRIBUTION"])
    contrib[f"{BASE_TIP[:12]}..{REVIEWED[:12]}"]["files"] = [
        {"filename": PR_FILE},
        {"filename": "src/new.py", "previous_filename": "src/old.py"},
    ]
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", json.dumps(contrib))
    assert guard._no_change_note("7", REPO, _merged_budget()) is None
