"""The `main-reverts` advisory row of ``git_push_guard.py --check-pr`` (issue #2809).

The defect class: a branch's merge-from-main commit keeps the branch's STALE copy
of files main has since changed, so the PR's diff silently reverts main. Measured
instance: a PR whose head was such a merge listed 126 changed files while its own
commits touched 4.

The detector's rule: on a correctly merged branch, every file in the PR's diff
was touched by one of the PR's own NON-merge commits. A file that reaches the diff
only through a merge resolution is the signature. One refinement: a file the PR
ADDS that main's history never contained cannot be a revert of main (a changelog
fragment written inside a merge commit is the measured case), so it is reported
as a note, not a finding.

Four states, never collapsed (genesis-development, guard failure semantics rule 1):
FINDINGS, CHECKED-CLEAN, COULD-NOT-CHECK, OUT-OF-SCOPE (no merge commits). A
capped or failed API read is COULD-NOT-CHECK, never clean.

ADVISORY: the row never counts toward the report's ``failures`` and is not wired
into the merge arm. Network-free throughout via the ``_TEST_GH_*`` env seams.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import private_module

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"
gpg = private_module("git_push_guard", _GUARD)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
REPO = "owner/repo"

OWN_A = "a" * 40
OWN_B = "b" * 40
MERGE = "c" * 40


def _commits(*pairs: tuple[str, int]) -> str:
    return "\n".join(json.dumps({"sha": sha, "parents": n}) for sha, n in pairs)


def _files(
    *names: str, renames: dict[str, str] | None = None, statuses: dict[str, str] | None = None
) -> str:
    renames = renames or {}
    statuses = statuses or {}
    rows = []
    for name in names:
        rows.append(
            json.dumps(
                {
                    "filename": name,
                    "previous_filename": renames.get(name),
                    "status": statuses.get(name, "modified"),
                }
            )
        )
    return "\n".join(rows)


def _commit_files(mapping: dict[str, object]) -> str:
    out: dict[str, object] = {}
    for sha, names in mapping.items():
        if isinstance(names, str):
            out[sha] = names
        else:
            out[sha] = [{"filename": n, "previous_filename": None} for n in names]
    return json.dumps(out)


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    gpg._reset_pr_files_cache()
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_PATH_ON_BASE", "{}")
    yield
    gpg._reset_pr_files_cache()


def _seed(monkeypatch, *, commits: str, files: str, commit_files: str) -> None:
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", commits)
    monkeypatch.setenv("_TEST_GH_PR_FILES", files)
    monkeypatch.setenv("_TEST_GH_COMMIT_FILES", commit_files)


class TestDetection:
    def test_stale_merge_is_flagged_and_names_the_files(self, monkeypatch):
        """POSITIVE: the PR's own commits touched one file; the diff carries three.
        The two that arrive only through the merge are the finding."""
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", ".claude/settings.json", "scripts/hooks/git_push_guard.py"),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_FINDINGS, msg
        first, *rest = msg.splitlines()
        assert first.startswith("2 file(s)"), first
        detail = "\n".join(rest)
        assert ".claude/settings.json" in detail
        assert "scripts/hooks/git_push_guard.py" in detail
        assert "src/own.py" not in detail, "a file the PR's own commit touched is not a finding"

    def test_finding_carries_the_remedy_and_why_merging_again_is_not_enough(self, monkeypatch):
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", "docs/x.md"),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_FINDINGS
        assert "merging main again alone does not fix it" in msg.lower()
        # No copy-pasteable command: the row cannot know which remote holds the
        # checked repository, a base-deleted file needs removal rather than a
        # checkout, and a base-side rename makes the finding a false positive
        # that a restore would turn into lost work.
        assert "git checkout" not in msg
        assert "renamed" in msg.lower()
        assert "deleted" in msg.lower()

    def test_correct_merge_is_clean(self, monkeypatch):
        """NEGATIVE: merges present, but every diff file was touched by an own commit."""
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2), (OWN_B, 1)),
            files=_files("src/own.py", "tests/test_own.py"),
            commit_files=_commit_files({OWN_A: ["src/own.py"], OWN_B: ["tests/test_own.py"]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_CLEAN, msg

    def test_rename_source_is_covered_by_a_commit_rename(self, monkeypatch):
        """`_pr_changed_files` lists rename SOURCES too; a commit that performed the
        rename covers both paths, so neither may be reported."""
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits((OWN_A, 1), (MERGE, 2)))
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", _files("src/new.py", renames={"src/new.py": "src/old.py"})
        )
        monkeypatch.setenv(
            "_TEST_GH_COMMIT_FILES",
            json.dumps({OWN_A: [{"filename": "src/new.py", "previous_filename": "src/old.py"}]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_CLEAN, msg

    def test_no_merge_commits_is_out_of_scope_and_reads_nothing_else(self, monkeypatch):
        """Without a merge, the PR diff is exactly the union of its commits — the
        defect cannot occur. Proven by poisoning the other two reads: an n/a that
        consulted them would report could-not-check instead."""
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (OWN_B, 1)),
            files="__error__",
            commit_files="{}",
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_NA, msg

    def test_the_file_list_is_bounded_with_the_total_stated(self, monkeypatch):
        names = [f"src/f{i:02d}.py" for i in range(25)]
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", *names),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_FINDINGS
        assert msg.splitlines()[0].startswith("25 file(s)")
        listed = [ln for ln in msg.splitlines() if ln.strip().startswith("- src/f")]
        assert len(listed) == gpg._MAIN_REVERTS_LIST_MAX, listed
        assert f"{25 - gpg._MAIN_REVERTS_LIST_MAX} more" in msg, "a cut must say what it cut"


class TestAddedFiles:
    """An uncovered ADDED file is a revert only if main's history ever held it."""

    def _seed_added(self, monkeypatch, on_base: object) -> None:
        frag = "changelog.d/20260903130000-added-x.md"
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", frag, statuses={frag: "added"}),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        monkeypatch.setenv("_TEST_GH_BASE_OID", "ba5e" * 10)
        payload = {} if on_base is None else {frag: on_base}
        monkeypatch.setenv("_TEST_GH_PATH_ON_BASE", json.dumps(payload))

    def test_added_in_a_merge_and_never_on_main_is_not_a_finding(self, monkeypatch):
        self._seed_added(monkeypatch, False)
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_CLEAN, msg
        assert "changelog.d/20260903130000-added-x.md" in msg, "the note must still name it"

    def test_added_but_main_once_had_it_is_a_revert_of_a_deletion(self, monkeypatch):
        self._seed_added(monkeypatch, True)
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_FINDINGS, msg

    def test_added_with_unreadable_history_stays_a_finding(self, monkeypatch):
        """Unknown is never resolved toward clean."""
        self._seed_added(monkeypatch, None)
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_FINDINGS, msg


class TestCouldNotCheck:
    def test_capped_commit_list_is_could_not_check(self, monkeypatch):
        """pulls/N/commits returns at most 250 commits; a saturated read cannot
        prove the remaining commits do not touch the uncovered files."""
        pairs = [(f"{i:040x}", 1) for i in range(gpg._PR_COMMITS_CAP - 1)] + [(MERGE, 2)]
        _seed(
            monkeypatch,
            commits=_commits(*pairs),
            files=_files("src/own.py"),
            commit_files="{}",
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "250" in msg.splitlines()[0]

    def test_commit_list_api_failure_is_could_not_check(self, monkeypatch):
        _seed(monkeypatch, commits="__error__", files=_files("src/own.py"), commit_files="{}")
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg

    def test_empty_commit_list_is_could_not_check(self, monkeypatch):
        """A PR always has a commit; an empty read is a degraded response, not 'none'."""
        _seed(monkeypatch, commits="", files=_files("src/own.py"), commit_files="{}")
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg

    def test_changed_file_list_failure_is_could_not_check(self, monkeypatch):
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files="__error__",
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "3000" in msg.splitlines()[0], "the cap must be named alongside the error"

    def test_one_commit_read_failing_is_could_not_check(self, monkeypatch):
        """The failed commit might be the one that touched the 'uncovered' file."""
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (OWN_B, 1), (MERGE, 2)),
            files=_files("src/own.py", "src/other.py"),
            commit_files=_commit_files({OWN_A: ["src/own.py"], OWN_B: "__error__"}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg

    def test_capped_commit_file_list_is_could_not_check(self, monkeypatch):
        big = [f"gen/f{i}.txt" for i in range(gpg._COMMIT_FILES_CAP)]
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", "src/beyond_the_cap.py"),
            commit_files=_commit_files({OWN_A: big}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "3000" in msg.splitlines()[0]


def _report_env(monkeypatch) -> None:
    """Every OTHER gate green, so the verdict isolates the main-reverts row."""
    monkeypatch.setattr(gpg, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
    monkeypatch.setattr(gpg, "_pr_ci_status", lambda n, repo=None: ("green", []))
    monkeypatch.setattr(gpg, "_check_base_is_default", lambda n, repo=None: (False, ""))
    monkeypatch.setattr(gpg, "_check_pin_receipts", lambda n, repo=None: (False, "ok"))
    monkeypatch.setattr(gpg, "_check_codex_reviewed_head", lambda n, repo=None: (False, "", HEAD))
    monkeypatch.setattr(gpg, "_scheduled_gate_applies", lambda repo: False)
    monkeypatch.setattr(
        gpg, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
    )
    monkeypatch.setattr(
        gpg,
        "_check_inline_review_findings",
        lambda n, repo=None, force=False, uncounted_out=None: (False, ""),
    )
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)


def _row(out: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith("main-reverts"))


class TestReportRow:
    def test_findings_row_is_advisory_and_never_flips_the_verdict(self, monkeypatch, capsys):
        _report_env(monkeypatch)
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py", ".claude/settings.json"),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        rc = gpg.check_pr_report("100", repo=REPO)
        out = capsys.readouterr().out
        row = _row(out)
        assert row.startswith("main-reverts   : advisory — 1 file(s)"), row
        assert "BLOCK" not in row
        assert "  - .claude/settings.json" in out, "the detail must reach the operator"
        assert "MERGEABLE (all gates pass)" in out
        assert rc == 0, out

    def test_could_not_check_row_never_reads_ok_and_never_flips_the_verdict(
        self, monkeypatch, capsys
    ):
        _report_env(monkeypatch)
        _seed(monkeypatch, commits="__error__", files=_files("src/own.py"), commit_files="{}")
        rc = gpg.check_pr_report("100", repo=REPO)
        row = _row(capsys.readouterr().out)
        assert row.startswith("main-reverts   : could not check — "), row
        assert "ok" not in row.split("—", 1)[0]
        assert rc == 0

    def test_clean_row(self, monkeypatch, capsys):
        _report_env(monkeypatch)
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py"),
            commit_files=_commit_files({OWN_A: ["src/own.py"]}),
        )
        rc = gpg.check_pr_report("100", repo=REPO)
        assert _row(capsys.readouterr().out).startswith("main-reverts   : ok (")
        assert rc == 0

    def test_a_raising_detector_degrades_to_could_not_check(self, monkeypatch, capsys):
        """An advisory row never takes the report down with it."""
        _report_env(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("synthetic")

        monkeypatch.setattr(gpg, "_pr_commit_list", _boom)
        rc = gpg.check_pr_report("100", repo=REPO)
        out = capsys.readouterr().out
        assert _row(out).startswith("main-reverts   : could not check — "), out
        assert "verdict" in out and rc == 0


class TestBoundedFanOut:
    """The row runs on the interactive report path, which arms no merge deadline.
    A degraded API must cost at most the row's budget, never one timeout per
    commit, and a failed read must not wait for the rest of the fan-out."""

    def _slow_reads(self, monkeypatch, *, delay: float, fail_first: bool = False):
        import time as _time

        first = OWN_A

        def _read(sha, repo=None):
            if fail_first and sha == first:
                return None, "commit aaaa's file list could not be read (gh error)"
            _time.sleep(delay)
            return {"src/own.py"}, ""

        monkeypatch.setattr(gpg, "_commit_touched_files", _read)

    def test_over_budget_fan_out_is_could_not_check_and_returns_promptly(self, monkeypatch):
        import time as _time

        own = [f"{i:040x}" for i in range(1, 13)]
        _seed(
            monkeypatch,
            commits=_commits(*[(s, 1) for s in own], (MERGE, 2)),
            files=_files("src/own.py"),
            commit_files="{}",
        )
        self._slow_reads(monkeypatch, delay=2.0)
        monkeypatch.setattr(gpg, "_MAIN_REVERTS_BUDGET_S", 0.05)
        start = _time.monotonic()
        state, msg = gpg._check_main_reverts("100", REPO)
        elapsed = _time.monotonic() - start
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "budget" in msg
        # 12 reads x 2 s over 6 workers would take >= 4 s if every read were awaited.
        assert elapsed < 1.5, elapsed

    def test_first_failed_read_returns_without_waiting_for_the_rest(self, monkeypatch):
        import time as _time

        own = [OWN_A] + [f"{i:040x}" for i in range(1, 12)]
        _seed(
            monkeypatch,
            commits=_commits(*[(s, 1) for s in own], (MERGE, 2)),
            files=_files("src/own.py"),
            commit_files="{}",
        )
        self._slow_reads(monkeypatch, delay=2.0, fail_first=True)
        start = _time.monotonic()
        state, msg = gpg._check_main_reverts("100", REPO)
        elapsed = _time.monotonic() - start
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "could not be read" in msg
        assert elapsed < 1.5, elapsed

    def test_out_of_budget_before_history_lookups_is_could_not_check(self, monkeypatch):
        """An added uncovered file whose history was never looked up must not be
        counted as a revert just because the budget ran out first."""
        _seed(
            monkeypatch,
            commits=_commits((MERGE, 2)),
            files=_files("changelog.d/new.md", statuses={"changelog.d/new.md": "added"}),
            commit_files="{}",
        )
        monkeypatch.setenv("_TEST_GH_BASE_OID", "d" * 40)
        monkeypatch.setenv("_TEST_GH_PATH_ON_BASE", json.dumps({"changelog.d/new.md": False}))
        monkeypatch.setattr(gpg, "_MAIN_REVERTS_BUDGET_S", 0.0)
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "budget" in msg


class TestDegradedCommitResponse:
    def test_commit_response_without_a_file_list_is_could_not_check(self, monkeypatch):
        """A commit page with no `files` array is a degraded read, not a commit that
        touched nothing (which would shrink coverage and over-report)."""
        _seed(
            monkeypatch,
            commits=_commits((OWN_A, 1), (MERGE, 2)),
            files=_files("src/own.py"),
            commit_files=json.dumps({OWN_A: [{"__no_files__": True}]}),
        )
        state, msg = gpg._check_main_reverts("100", REPO)
        assert state == gpg.MAIN_REVERTS_UNCHECKED, msg
        assert "no file list" in msg
