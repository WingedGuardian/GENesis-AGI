"""The skip-ceiling checker must REFUSE rather than report clean.

A dormant test — one that stopped running because a skipif broke, a marker was
lost, or a directory fell out of collection — is invisible: the suite still
prints green. This checker exists to make that visible by watching the SKIP
count for an unexplained rise.

Every test here is written against the failure that makes such a checker
worthless: a run that measured NOTHING and said so in the grammar of a pass.
"0 skips" from an empty report and "0 skips" from a healthy suite must never
be the same exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_skip_ceiling.py"

# Exit contract — asserted here so a change to it breaks a test, not a caller.
OK, EXCEEDED, REFUSED = 0, 1, 2


def run(xml: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(xml), *args],
        capture_output=True,
        text=True,
    )


# The checker's own refusal marker. Python ALSO exits 2 when it cannot open a
# script file, so asserting the code alone passes when the script is DELETED —
# measured: the five refuse-tests below all passed before the script existed.
# Every refusal assertion therefore requires output only the checker emits.
REFUSAL_MARKER = "SKIP-CEILING"


def assert_refused(r: subprocess.CompletedProcess) -> None:
    out = r.stdout + r.stderr
    assert r.returncode == REFUSED, out
    assert REFUSAL_MARKER in out, (
        f"exit 2 but no {REFUSAL_MARKER!r} in output — this is python failing to "
        f"open the script, not the checker refusing. Output: {out!r}"
    )


def write(tmp_path: Path, body: str, name: str = "junit.xml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def suite(tests: int, skipped: int) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" tests="{tests}" '
        f'errors="0" failures="0" skipped="{skipped}"></testsuite></testsuites>\n'
    )


class TestRefusesRatherThanGuesses:
    """A checker that cannot measure must not print a clean result."""

    def test_missing_report_refuses(self, tmp_path: Path) -> None:
        r = run(tmp_path / "nope.xml")
        assert_refused(r)
        assert "REFUS" in (r.stdout + r.stderr).upper()

    def test_unparseable_report_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, "<testsuites><broken"))
        assert_refused(r)

    def test_report_with_no_testsuite_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, '<?xml version="1.0"?>\n<other/>\n'))
        assert_refused(r)

    def test_zero_collected_tests_refuses(self, tmp_path: Path) -> None:
        """The empty-corpus trap: a suite that ran nothing reports 0 skips,
        which reads identically to a healthy suite unless we refuse it."""
        r = run(write(tmp_path, suite(tests=0, skipped=0)))
        assert_refused(r)
        assert "0" in r.stdout + r.stderr

    def test_missing_skipped_attribute_refuses(self, tmp_path: Path) -> None:
        body = (
            '<?xml version="1.0"?>\n<testsuites><testsuite name="pytest" '
            'tests="10"></testsuite></testsuites>\n'
        )
        r = run(write(tmp_path, body))
        assert_refused(r)


class TestReportMode:
    """With no ceiling configured the checker MEASURES and passes, so the
    first run on real CI produces the baseline instead of us inventing one."""

    def test_reports_count_and_passes(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=7)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "7" in r.stdout

    def test_report_mode_says_it_is_not_enforcing(self, tmp_path: Path) -> None:
        """MEASURED vacuous once: the old assertion allowed `or "report"` over
        stdout+stderr, and every REFUSAL message contains the word "report"
        ("REFUSING — no report at ..."). It therefore survived a mutation that
        replaced report-mode with a refusal — the very thing it names."""
        r = run(write(tmp_path, suite(tests=100, skipped=7)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "not enforcing" in r.stdout.lower()


class TestEnforcement:
    def test_under_ceiling_passes(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)), "--ceiling", "10")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_at_ceiling_passes(self, tmp_path: Path) -> None:
        """The ceiling is a maximum, not a forbidden value."""
        r = run(write(tmp_path, suite(tests=100, skipped=10)), "--ceiling", "10")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_over_ceiling_fails(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=11)), "--ceiling", "10")
        assert r.returncode == EXCEEDED, r.stdout + r.stderr
        assert "11" in r.stdout + r.stderr and "10" in r.stdout + r.stderr

    def test_far_under_ceiling_still_passes(self, tmp_path: Path) -> None:
        """Deliberately NOT a floor. Removing a skip is fixing a dormant test;
        a checker that punished that would be arguing for dormancy."""
        r = run(write(tmp_path, suite(tests=100, skipped=0)), "--ceiling", "10")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_negative_ceiling_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)), "--ceiling", "-1")
        assert_refused(r)

    def test_non_numeric_ceiling_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)), "--ceiling", "lots")
        assert_refused(r)


class TestMultipleSuites:
    def test_skips_are_summed_across_testsuite_elements(self, tmp_path: Path) -> None:
        body = (
            '<?xml version="1.0"?>\n<testsuites>'
            '<testsuite name="a" tests="10" skipped="2"></testsuite>'
            '<testsuite name="b" tests="10" skipped="3"></testsuite>'
            "</testsuites>\n"
        )
        r = run(write(tmp_path, body), "--ceiling", "4")
        assert r.returncode == EXCEEDED, r.stdout + r.stderr
        assert "5" in r.stdout + r.stderr


class TestReportCountsAreSane:
    """The script refuses a negative --ceiling; it must hold the report to the
    same invariant, and must not silently double-count a nested producer."""

    def test_negative_counts_in_report_refuse(self, tmp_path: Path) -> None:
        body = (
            '<?xml version="1.0"?>\n<testsuites><testsuite name="pytest" '
            'tests="10" skipped="-3"></testsuite></testsuites>\n'
        )
        assert_refused(run(write(tmp_path, body)))

    def test_nested_testsuite_refuses_rather_than_double_counting(
        self, tmp_path: Path
    ) -> None:
        """pytest never nests, but other junit producers do, and
        ElementTree.iter() walks all descendants — so a nested report would
        sum each suite twice and pass a ceiling it should trip."""
        body = (
            '<?xml version="1.0"?>\n<testsuites>'
            '<testsuite name="outer" tests="10" skipped="9">'
            '<testsuite name="inner" tests="10" skipped="9"></testsuite>'
            "</testsuite></testsuites>\n"
        )
        assert_refused(run(write(tmp_path, body)))


class TestCollectedFloor:
    """The skip ceiling is blind to two of the three dormancy modes, because
    both LOWER the skip count. MEASURED on a 4-test sandbox at --ceiling 1:
    dropping half the suite from collection printed "1 skipped of 2 collected.
    OK." and deleting the skip marker printed "0 skipped of 4 collected. OK."
    The collected count is the instrument for those two; these tests are the
    acceptance bar for the exact scenarios the ceiling let through."""

    def test_dropped_collection_is_caught_by_the_floor(self, tmp_path: Path) -> None:
        """The scenario the ceiling missed: half the suite stops being collected."""
        r = run(write(tmp_path, suite(tests=2, skipped=1)), "--ceiling", "1",
                "--min-collected", "4")
        assert r.returncode == EXCEEDED, r.stdout + r.stderr
        assert "2" in r.stdout + r.stderr and "4" in r.stdout + r.stderr

    def test_deleted_marker_leaves_collection_intact_and_passes(
        self, tmp_path: Path
    ) -> None:
        """A deleted skip marker LOWERS skips but does not change collection,
        so the floor correctly does not fire. Recorded so the floor's scope is
        not overclaimed: it catches tests that vanish, not skips that vanish."""
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--ceiling", "1",
                "--min-collected", "4")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_at_floor_passes(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--min-collected", "4")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_above_floor_passes(self, tmp_path: Path) -> None:
        """Adding tests must never fail: the floor is a floor, not a target."""
        r = run(write(tmp_path, suite(tests=99, skipped=0)), "--min-collected", "4")
        assert r.returncode == OK, r.stdout + r.stderr

    def test_negative_floor_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--min-collected", "-1")
        assert_refused(r)

    def test_non_numeric_floor_refuses(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--min-collected", "some")
        assert_refused(r)

    def test_floor_message_does_not_blame_skip_causes(self, tmp_path: Path) -> None:
        """The FAIL text must name only causes ITS axis can produce. A ceiling
        trip is never a lost directory; a floor trip is never an added xfail."""
        r = run(write(tmp_path, suite(tests=2, skipped=0)), "--min-collected", "4")
        # MEASURED vacuous once: asserting `"collect" in out` alone passed on
        # argparse's own error ("unrecognized arguments: --min-collected"),
        # which is exactly the absent-mechanism path. Pin the exit code and a
        # phrase only the real floor message emits.
        assert r.returncode == EXCEEDED, r.stdout + r.stderr
        out = (r.stdout + r.stderr).lower()
        assert "stopped being collected" in out
        assert "xfail" not in out

    def test_ceiling_message_excludes_collection_causes_by_name(
        self, tmp_path: Path
    ) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=11)), "--ceiling", "10")
        assert r.returncode == EXCEEDED, r.stdout + r.stderr
        out = (r.stdout + r.stderr).lower()
        # The message may NAME the collection causes, but only to EXCLUDE them
        # and redirect to the other axis. Asserting mere absence was the weaker
        # property: silence leaves the reader to guess, whereas an explicit
        # "not this, use --min-collected" is what stops the misdirection.
        assert "not a lost marker" in out
        assert "--min-collected" in out

    def test_report_mode_reports_both_axes(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=7)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--ceiling 7" in r.stdout and "--min-collected 100" in r.stdout


class TestDirtyRunBaseline:
    """`if: !cancelled()` guarantees the step runs on FAILED runs too — exactly
    when the counts describe a corpus that did not finish. Report mode must not
    hand the operator a paste-ready baseline measured on a red run: the whole
    justification for report-only is that the number is honest."""

    @staticmethod
    def _dirty(tests: int, skipped: int, errors: int, failures: int) -> str:
        return (
            f'<?xml version="1.0"?>\n<testsuites><testsuite name="pytest" '
            f'tests="{tests}" errors="{errors}" failures="{failures}" '
            f'skipped="{skipped}"></testsuite></testsuites>\n'
        )

    def test_errors_suppress_the_baseline_suggestion(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, self._dirty(100, 7, errors=37, failures=0)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--ceiling 7" not in r.stdout, "offered a baseline from a red run"
        assert "37" in r.stdout

    def test_failures_suppress_the_baseline_suggestion(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, self._dirty(100, 7, errors=0, failures=4)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--ceiling 7" not in r.stdout

    def test_clean_run_still_offers_the_baseline(self, tmp_path: Path) -> None:
        """The control: suppression must be caused by the dirt, not by the
        suggestion having been removed altogether."""
        r = run(write(tmp_path, self._dirty(100, 7, errors=0, failures=0)))
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--ceiling 7" in r.stdout and "--min-collected 100" in r.stdout

    def test_a_dirty_run_still_ENFORCES_when_configured(self, tmp_path: Path) -> None:
        """Only the SUGGESTION is gated. A configured ceiling must still bite on
        a red run — dormancy does not become acceptable because tests failed."""
        r = run(write(tmp_path, self._dirty(100, 11, errors=3, failures=0)),
                "--ceiling", "10")
        assert r.returncode == EXCEEDED, r.stdout + r.stderr


class TestHalfConfigured:
    """Ceiling-only silently reinstates the gap the floor was added to close,
    and it is the LIKELY misconfiguration: the script and the CI step are both
    named for the ceiling, so a hurried operator pastes the first flag."""

    def test_ceiling_only_warns_the_floor_is_not_enforced(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)), "--ceiling", "10")
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--min-collected" in r.stdout, "ceiling-only did not name the gap"

    def test_floor_only_warns_the_ceiling_is_not_enforced(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)), "--min-collected", "4")
        assert r.returncode == OK, r.stdout + r.stderr
        assert "--ceiling" in r.stdout

    def test_both_configured_warns_about_neither(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=100, skipped=5)),
                "--ceiling", "10", "--min-collected", "4")
        assert r.returncode == OK, r.stdout + r.stderr
        assert "not enforced" not in r.stdout.lower()


class TestRefusalTextIntegrity:
    """A blanket str.replace over a message embedding the operator's own input
    rewrites what they typed, and blamed the wrong axis for a floor error."""

    def test_floor_refusal_does_not_rewrite_the_echoed_input(
        self, tmp_path: Path
    ) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)),
                "--min-collected", "my-ceiling-value")
        assert_refused(r)
        out = r.stdout + r.stderr
        assert "my-ceiling-value" in out, "the echoed input was rewritten"

    def test_negative_floor_names_the_collected_axis(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--min-collected", "-1")
        assert_refused(r)
        out = (r.stdout + r.stderr).lower()
        assert "collected count" in out
        assert "skip count" not in out, "floor error blamed the skip axis"

    def test_negative_ceiling_names_the_skip_axis(self, tmp_path: Path) -> None:
        r = run(write(tmp_path, suite(tests=4, skipped=0)), "--ceiling", "-1")
        assert_refused(r)
        assert "skip count" in (r.stdout + r.stderr).lower()


class TestRunsTheWayCIRunsIt:
    """ci.yml invokes a bare `python` with a RELATIVE path and only the test
    extra installed. Precedent: tests/test_scripts/test_check_migration_prefixes.py
    carries an equivalent guard because a checker once exited 2 on every PR —
    enforcing nothing — while every in-venv test passed."""

    def test_bare_interpreter_relative_path_stdlib_only(self, tmp_path: Path) -> None:
        import os
        report = write(tmp_path, suite(tests=100, skipped=7))
        r = subprocess.run(
            ["python3", "-E", "-S", "scripts/check_skip_ceiling.py", str(report)],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        assert r.returncode == OK, f"CI-shaped invocation failed: {r.stdout}{r.stderr}"
        assert "7 skipped of 100 collected" in r.stdout
