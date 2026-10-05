"""Tests for scripts/pr_shape_study.py — the analysis half only (no gh, no git)."""

import importlib.util
import sys
from pathlib import Path

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
    assert table[-1]["bucket"] == "1500-+" and table[-1]["median"] == 5
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
    assert study.spearman([1, 1, 1], [1, 2, 3]) == 0.0
    assert study.spearman([1], [1]) == 0.0


def test_plain_count_skips_excluded_files_and_blank_lines():
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,1 +1,3 @@\n-old\n+new\n+\n"
        "diff --git a/tests/t.py b/tests/t.py\n--- a/tests/t.py\n+++ b/tests/t.py\n"
        "@@ -0,0 +1,1 @@\n+test line\n"
    )
    assert study.plain_count(diff, {"tests/t.py": "test"}) == 2


def test_plain_count_counts_a_deleted_files_removed_lines():
    diff = (
        "diff --git a/src/gone.py b/src/gone.py\ndeleted file mode 100644\n"
        "--- a/src/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
    )
    assert study.plain_count(diff, {}) == 2
