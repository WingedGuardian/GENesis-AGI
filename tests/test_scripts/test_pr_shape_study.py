"""Tests for scripts/pr_shape_study.py — the analysis half only (no gh, no git)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "pr_shape_study.py"
_spec = importlib.util.spec_from_file_location("pr_shape_study", _PATH)
study = importlib.util.module_from_spec(_spec)
sys.modules["pr_shape_study"] = study
_spec.loader.exec_module(study)


def _rows(pairs):
    return [{"counted": c, "plain": c, "rounds": r} for c, r in pairs]


def test_bucket_table_medians_and_counts():
    table = study.bucket_table(_rows([(10, 1), (20, 1), (30, 2), (300, 3), (2000, 5)]), "counted")
    first = table[0]
    assert (first["bucket"], first["n"], first["median"]) == ("0-50", 3, 1)
    assert table[2]["n"] == 1 and table[2]["median"] == 3
    # One 1000+ bucket, as the table recorded in pr_shape.py has (review finding).
    assert table[-1]["bucket"] == "1000-+" and table[-1]["median"] == 5
    assert table[1]["n"] == 0 and table[1]["median"] is None


def test_implied_thresholds_take_first_qualifying_bucket():
    rows = _rows([(10, 1)] * 5 + [(100, 2)] * 5 + [(250, 3)] * 5 + [(450, 4)] * 5)
    assert study.implied_thresholds(study.bucket_table(rows, "counted")) == (200, 400)


def test_implied_thresholds_ignore_buckets_under_minimum_n():
    rows = _rows([(10, 1)] * 5 + [(250, 4)] * (study.MIN_BUCKET_N - 1))
    assert study.implied_thresholds(study.bucket_table(rows, "counted")) == (None, None)


def test_spearman_perfect_and_inverse_and_ties():
    assert study.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert study.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    # Undefined is None, never a measured 0.0 (review finding).
    assert study.spearman([1, 1, 1], [1, 2, 3]) is None
    assert study.spearman([1], [1]) is None
    assert study.spearman([], []) is None


def _count(diff):
    return study.pr_shape.count_diff(diff)


def test_plain_size_skips_excluded_files_and_blank_lines_but_keeps_comments():
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,1 +1,3 @@\n-old\n+new\n+\n+# a comment\n"
        "diff --git a/tests/t.py b/tests/t.py\n--- a/tests/t.py\n+++ b/tests/t.py\n"
        "@@ -0,0 +1,1 @@\n+test line\n"
    )
    result = _count(diff)
    assert (result["plain"], result["counted"]) == (3, 2)


def test_plain_size_counts_a_deleted_files_removed_lines():
    diff = (
        "diff --git a/src/gone.py b/src/gone.py\ndeleted file mode 100644\n"
        "--- a/src/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
    )
    assert _count(diff)["plain"] == 2


def test_plain_size_reads_a_quoted_path():
    """Review finding: a Git-quoted (non-ASCII) path dropped every line."""
    diff = (
        'diff --git "a/src/\\303\\251.py" "b/src/\\303\\251.py"\n'
        '--- "a/src/\\303\\251.py"\n+++ "b/src/\\303\\251.py"\n'
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
    )
    assert _count(diff)["plain"] == 2


@pytest.mark.parametrize(
    "old, new, plain",
    [
        ("src/a.py", "tests/a.py", 1),  # code -> test: the removed code side counts
        ("tests/a.py", "src/a.py", 1),  # test -> code: only the added code side counts
    ],
)
def test_plain_size_takes_each_rename_side_on_its_own_path(old, new, plain):
    """Review finding: one new-side path was applied to both sides of a rename."""
    diff = (
        f"diff --git a/{old} b/{new}\nsimilarity index 50%\nrename from {old}\n"
        f"rename to {new}\n--- a/{old}\n+++ b/{new}\n@@ -1,1 +1,1 @@\n-x = 1\n+y = 2\n"
    )
    assert _count(diff)["plain"] == plain


def test_cache_drops_only_a_torn_last_line(tmp_path):
    cache = tmp_path / "rows.jsonl"
    good = json.dumps({"repo": "o/r", "pr": 1})
    cache.write_text(good + "\n" + '{"repo": "o/r", "pr"')
    assert set(study.load_cache(cache, "o/r")) == {1}
    assert cache.read_text() == good + "\n"


def test_cache_refuses_a_bad_line_before_the_end(tmp_path):
    cache = tmp_path / "rows.jsonl"
    cache.write_text("not json\n" + json.dumps({"repo": "o/r", "pr": 1}) + "\n")
    with pytest.raises(SystemExit):
        study.load_cache(cache, "o/r")


def test_cache_refuses_rows_from_another_repository(tmp_path):
    """Review finding: a reused --out mixed two repositories' rows."""
    cache = tmp_path / "rows.jsonl"
    cache.write_text(json.dumps({"repo": "other/r", "pr": 1}) + "\n")
    with pytest.raises(SystemExit):
        study.load_cache(cache, "o/r")


@pytest.mark.parametrize(
    "log, commits, squash",
    [
        ("p1\nfeat: x (#7)", 3, True),
        ("p1\nlast rebased commit", 1, True),
        ("p1\nlast rebased commit", 3, False),  # rebase merge: one commit is not the PR
        ("p1 p2\nMerge pull request #7", 3, False),  # a merge commit
    ],
)
def test_is_squash(monkeypatch, log, commits, squash):
    monkeypatch.setattr(study, "_sh", lambda *a: log)
    assert study._is_squash("abc", 7, lambda: commits) is squash
