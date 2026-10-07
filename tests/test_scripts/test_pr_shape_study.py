"""Tests for scripts/pr_shape_study.py — the analysis half only (no gh, no git)."""

import importlib.util
import json
import statistics
import subprocess
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
    assert (first["bucket"], first["n"], first["median"]) == ("[0,50)", 3, 1.0)
    assert table[2]["n"] == 1 and table[2]["median"] == 3
    # One 1000+ bucket, as the table recorded in pr_shape.py has (review finding).
    assert table[-1]["bucket"] == "[1000,+)" and table[-1]["median"] == 5
    assert table[-1]["past_round_4"] == 1
    assert table[1]["n"] == 0 and table[1]["median"] is None


def test_implied_thresholds_take_first_qualifying_bucket():
    rows = _rows([(10, 1)] * 5 + [(100, 2)] * 5 + [(250, 3)] * 5 + [(450, 4)] * 5)
    assert study.implied_thresholds(study.bucket_table(rows, "counted")) == {
        "shape": 200, "shape_status": "reached", "override": 400, "override_status": "reached",
    }


def test_implied_thresholds_ignore_buckets_under_minimum_n():
    rows = _rows([(10, 1)] * 5 + [(250, 4)] * (study.MIN_BUCKET_N - 1))
    # Review finding: "too few PRs to judge" must not read as "never reached".
    assert study.implied_thresholds(study.bucket_table(rows, "counted")) == {
        "shape": None, "shape_status": "insufficient_n",
        "override": None, "override_status": "insufficient_n",
    }


def test_implied_thresholds_not_reached_is_its_own_status():
    rows = _rows([(10, 1)] * 5 + [(250, 2)] * 5)
    got = study.implied_thresholds(study.bucket_table(rows, "counted"))
    assert (got["shape"], got["shape_status"]) == (None, "not_reached")


def test_p75_is_linear_interpolation_and_matches_the_median_definition():
    """Review finding: r[int(0.75*(n-1))] returned the minimum of [1, 4]."""
    table = study.bucket_table(_rows([(10, 1), (20, 4)]), "counted")
    assert table[0]["p75"] == 3.25
    assert study.bucket_table(_rows([(10, 3)]), "counted")[0]["p75"] == 3.0
    rows = _rows([(10, r) for r in (1, 2, 3, 4, 5, 9)])
    assert study._p75(sorted(r["rounds"] for r in rows)) == 4.75
    assert statistics.quantiles([1, 2, 3, 4, 5, 9], n=4, method="inclusive")[1] == statistics.median(
        [1, 2, 3, 4, 5, 9]
    )


def test_spearman_ties_match_the_standard_ranked_correlation():
    xs = [1, 2, 2, 3, 5, 5, 5, 8]
    ys = [1, 3, 2, 2, 4, 6, 4, 9]
    assert abs(study.spearman(xs, ys) - statistics.correlation(xs, ys, method="ranked")) < 1e-12


@pytest.mark.parametrize(
    "created, cohort",
    [("2026-01-01T00:00:00Z", "pre"), ("2099-01-01T00:00:00Z", "post"), ("2099-01-01T00:00:00+05:00", "post")],
)
def test_cohort_is_computed_with_timezones(created, cohort):
    assert study.cohort_of(created, "2026-09-01T00:00:00+00:00") == cohort


def test_cache_refuses_rows_from_another_schema(tmp_path):
    cache = tmp_path / "rows.jsonl"
    cache.write_text(json.dumps({"repo": "o/r", "pr": 1, "schema": 1}) + "\n")
    with pytest.raises(SystemExit):
        study.load_cache(cache, "o/r")


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
    good = json.dumps({"repo": "o/r", "pr": 1, "schema": study.ROW_SCHEMA})
    cache.write_text(good + "\n" + '{"repo": "o/r", "pr"')
    assert set(study.load_cache(cache, "o/r")) == {1}
    assert cache.read_text() == good + "\n"


def test_cache_refuses_a_bad_line_before_the_end(tmp_path):
    cache = tmp_path / "rows.jsonl"
    cache.write_text("not json\n" + json.dumps({"repo": "o/r", "pr": 1, "schema": study.ROW_SCHEMA}) + "\n")
    with pytest.raises(SystemExit):
        study.load_cache(cache, "o/r")


def test_cache_refuses_rows_from_another_repository(tmp_path):
    """Review finding: a reused --out mixed two repositories' rows."""
    cache = tmp_path / "rows.jsonl"
    cache.write_text(json.dumps({"repo": "other/r", "pr": 1, "schema": study.ROW_SCHEMA}) + "\n")
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
    monkeypatch.setattr(study, "_git", lambda *a: log)
    assert study._is_squash("abc", 7, lambda: commits) is squash


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 0\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "work")
    (tmp_path / "a.py").write_text("x = 0\n" + "".join(f"y{i} = {i}\n" for i in range(5)))
    git("commit", "-q", "-am", "work")
    return git


def _cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(_PATH.parent / "pr_shape.py"), *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )


def test_cli_prints_the_count_and_band(tmp_path):
    _repo(tmp_path)
    run = _cli(tmp_path, "--base", "main")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "5 ok"


def test_cli_ignores_an_attributes_file_that_hides_code(tmp_path):
    """Review finding: an attributes file marking *.py binary emptied the count
    (MEASURED by the premise check: 12,386 counted lines fell to 1,445)."""
    git = _repo(tmp_path)
    (tmp_path / "attrs").write_text("*.py binary\n")
    git("config", "core.attributesFile", str(tmp_path / "attrs"))
    (tmp_path / ".gitattributes").write_text("")
    git("config", "diff.hide.textconv", "true")
    assert _cli(tmp_path, "--base", "main").stdout.strip() == "5 ok"


def test_cli_fails_loudly_on_a_bad_ref(tmp_path):
    _repo(tmp_path)
    run = _cli(tmp_path, "--base", "no-such-ref")
    assert run.returncode == 2
    assert "could not read" in run.stderr
    assert run.stdout == ""


def test_study_history_reads_go_through_the_hardened_runner(monkeypatch):
    """Review finding: the study read diffs and logs with raw git, so an attributes
    file or a replace ref could change its measurement while the CLI's did not."""
    seen = []
    scope = study.pr_shape._load_sibling("review_scope.py", "_review_scope_for_pr_shape")
    monkeypatch.setattr(scope, "_git", lambda argv, cwd, *a, **k: seen.append(argv) or "x")
    study._git("diff", "--no-color", "-M", "a^1", "a")
    study._git("log", "-1", "a")
    assert seen == [["diff", "--no-color", "-M", "a^1", "a"], ["--no-replace-objects", "log", "-1", "a"]]


def test_study_git_failure_raises(monkeypatch):
    scope = study.pr_shape._load_sibling("review_scope.py", "_review_scope_for_pr_shape")
    monkeypatch.setattr(scope, "_git", lambda *a, **k: None)
    with pytest.raises(subprocess.CalledProcessError):
        study._git("log", "-1", "a")


def test_gh_reads_ignore_gh_repo(monkeypatch):
    """Review finding: an inherited GH_REPO pointed gh at another repository while
    git still read this checkout."""
    seen = {}
    monkeypatch.setenv("GH_REPO", "other/repo")
    monkeypatch.setattr(
        study.subprocess, "run",
        lambda args, **kw: seen.update(kw) or subprocess.CompletedProcess(args, 0, "x", ""),
    )
    study._sh("gh", "repo", "view")
    assert "GH_REPO" not in seen["env"]


def test_resumed_rows_take_fresh_round_counts(monkeypatch, tmp_path):
    """Review finding: a resumed run reused cached round counts, so one table could
    mix two evaluators. Sizes come from the cache; rounds are always re-read."""
    out = tmp_path / "out"
    out.mkdir()
    row = {"schema": study.ROW_SCHEMA, "repo": "o/r", "pr": 7, "counted": 10, "plain": 10,
           "rounds": 9, "created": "2026-01-01T00:00:00Z", "merged": "2026-01-02T00:00:00Z"}
    (out / "rows.jsonl").write_text(json.dumps(row) + "\n")
    stale = {"schema": study.ROW_SCHEMA, "repo": "o/r", "pr": 8, "counted": 10, "plain": 10,
             "rounds": 1, "created": "2026-01-01T00:00:00Z", "merged": "2026-01-02T00:00:00Z"}
    with (out / "rows.jsonl").open("a") as fh:
        fh.write(json.dumps(stale) + "\n")

    def fake_sh(*args):
        if args[:3] == ("gh", "repo", "view"):
            return "o/r\nmain\n"
        if args[:3] == ("gh", "pr", "list"):
            return json.dumps([{"number": 7}, {"number": 8}])
        raise AssertionError(args)

    monkeypatch.setattr(study, "_sh", fake_sh)
    monkeypatch.setattr(study, "_rounds", lambda repo, pr: {7: 2, 8: None}[pr])
    study.main(["--out", str(out)])
    report = json.loads((out / "report.json").read_text())
    assert report["excluded"]["budget_not_ok"] == 1
    assert report["used"] == 1
    assert report["max_rounds"] == 2
