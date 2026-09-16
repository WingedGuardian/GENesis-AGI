"""ci_failure_summary must name every failure, and must never fail itself.

The script runs only when the CI test job is ALREADY red, and its output is
the step summary — the one channel GitHub's log truncation cannot reach
(MEASURED 2026-09-14, run 34898229755: the log APIs dropped the tail of the
test step at ~44% of a 26,074-test run, and the tail is where pytest names
the failures). Two properties matter and both are asserted here:

* COMPLETENESS — every failed/errored testcase id is printed WHOLE. A summary
  that lists some of the failures reads as all of them. The single exception
  is GitHub's own 1 MiB step-summary cap, above which NOTHING is rendered;
  there the list is bounded to whole rows and the shortfall is stated with
  its denominator, so a bounded list never passes as a complete one. Both
  directions are asserted below — an implementation that always declared an
  omission would satisfy the cap test and fail the control.
* EXIT 0 ALWAYS — the job is already failing; a secondary exit code from the
  narrator would replace the real failure with a wrapper's. "Could not read
  the report" is carried by a printed notice, never by the code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "failure_summary.py"

_REPORT_WITH_FAILURES = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="2" errors="1">
    <testcase classname="tests.test_a" name="test_alpha">
      <failure message="AssertionError: expected BLOCK, got ALLOW&#10;full traceback here">boom</failure>
    </testcase>
    <testcase classname="tests.test_a" name="test_beta"/>
    <testcase classname="tests.test_b" name="test_gamma[param-1]">
      <failure>assert 1 == 2
long tail of traceback</failure>
    </testcase>
    <testcase classname="tests.test_c" name="test_delta">
      <error message="RuntimeError: fixture blew up">trace</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_every_failure_and_error_is_named_whole(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(_REPORT_WITH_FAILURES, encoding="utf-8")
    r = _run(str(report))
    assert r.returncode == 0
    # All three red testcases, ids complete — including the parametrised one,
    # whose bracket suffix is part of the id, not decoration.
    assert "tests.test_a::test_alpha" in r.stdout
    assert "tests.test_b::test_gamma[param-1]" in r.stdout
    assert "tests.test_c::test_delta" in r.stdout
    # The green one is not noise in the list.
    assert "test_beta" not in r.stdout
    # Count states the denominator.
    assert "3 failed/errored" in r.stdout
    # First message line only — the traceback tail stays in the artifact.
    assert "expected BLOCK, got ALLOW" in r.stdout
    assert "full traceback here" not in r.stdout
    # A failure node with no message attribute falls back to its text.
    assert "assert 1 == 2" in r.stdout
    assert "long tail of traceback" not in r.stdout


def test_a_missing_report_is_a_notice_not_a_failure(tmp_path):
    r = _run(str(tmp_path / "never-written.xml"))
    assert r.returncode == 0
    assert "pytest died before writing one" in r.stdout


def test_an_unparseable_report_is_a_notice_not_a_failure(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text("<testsuites><unclosed", encoding="utf-8")
    r = _run(str(report))
    assert r.returncode == 0
    assert "unparseable report" in r.stdout


def test_a_clean_report_on_a_red_job_says_the_failure_was_elsewhere(tmp_path):
    """The step only runs on failure; a no-failure report means pytest's own
    accounting did not see the death (segfault mid-run, internal error)."""
    report = tmp_path / "junit.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuites><testsuite tests="1">'
        '<testcase classname="t" name="ok"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    r = _run(str(report))
    assert r.returncode == 0
    assert "no failed or errored testcase" in r.stdout


def test_wrong_usage_still_exits_zero():
    r = _run()
    assert r.returncode == 0
    assert "usage" in r.stdout


def _report_with(count: int) -> str:
    cases = "".join(
        f'<testcase classname="tests.test_module_{i}" '
        f'name="test_a_fairly_long_and_descriptive_name_number_{i}">'
        f'<failure message="AssertionError: a representative message {i}"/>'
        "</testcase>"
        for i in range(count)
    )
    return f"<testsuites><testsuite>{cases}</testsuite></testsuites>"


def test_an_oversized_run_stays_under_the_cap_and_says_what_it_left_out(tmp_path):
    """GitHub renders NOTHING above 1 MiB, so an unbounded list does not print
    every failure — it prints none of them and takes the artifact pointer with
    it. The bound must hold, and it must announce itself."""
    report = tmp_path / "junit.xml"
    report.write_text(_report_with(12000), encoding="utf-8")
    r = _run(str(report))
    assert r.returncode == 0
    size = len(r.stdout.encode())
    assert size <= 1024 * 1024, f"summary is {size} bytes, over GitHub's cap"
    assert "not listed here" in r.stdout, (
        "the list was bounded without saying so, which reads as complete"
    )
    assert "of 12000" in r.stdout, "the shortfall was stated without its denominator"
    assert r.stdout.rstrip().endswith("artifact on this run."), (
        "the closing pointer fell off the end — the reserve did not hold, so "
        "the one line naming where the omitted failures live is gone"
    )
    listed = [ln for ln in r.stdout.splitlines() if ln.startswith("- **")]
    assert listed, "budget consumed everything; no failure is named at all"
    assert all(ln.endswith(tuple("0123456789")) for ln in listed), (
        "a row was cut mid-value rather than omitted whole"
    )


def test_an_ordinary_run_lists_everything_and_declares_no_omission(tmp_path):
    """CONTROL. Without this, an implementation that always claimed an
    omission would pass the cap test above while hiding failures on every
    ordinary red run."""
    report = tmp_path / "junit.xml"
    report.write_text(_report_with(40), encoding="utf-8")
    r = _run(str(report))
    assert r.returncode == 0
    assert "not listed here" not in r.stdout, (
        "declared an omission on a run that fits well inside the cap"
    )
    listed = [ln for ln in r.stdout.splitlines() if ln.startswith("- **")]
    assert len(listed) == 40, f"listed {len(listed)} of 40"


def test_an_unreadable_report_does_not_fail_the_narrator(tmp_path):
    """A report that EXISTS but cannot be READ raises OSError, not ParseError.

    `ElementTree.parse` raises `PermissionError` on a permission fault, so a
    handler catching only `ParseError` let it escape — and this script runs in a
    step that fires BECAUSE something already failed, so exiting non-zero there
    adds a second red herring to the one it exists to explain. Its whole
    contract is that it never fails itself.

    The file is made genuinely unreadable rather than patched, because `_run`
    spawns a SUBPROCESS: a monkeypatched parser in this process would never
    reach the code under test, and the test would pass against any behaviour.
    """
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite/>")
    os.chmod(report, 0o000)
    if os.access(report, os.R_OK):
        pytest.skip("this user ignores file modes (root), so the fault cannot be staged")

    out = _run(str(report))
    assert out.returncode == 0, (
        f"the narrator exited {out.returncode} on an unreadable report: {out.stderr}"
    )
    assert "unreadable report" in out.stdout


def test_a_crash_with_no_report_names_the_test_that_was_running(tmp_path, monkeypatch):
    """The case dropping `-v` made worse, and the reason the breadcrumb exists.

    A segfault, an OOM kill or `os._exit` inside a test never writes junit.xml.
    With `-v` the log at least named the running test; without it the log shows
    progress characters only. The conftest hook rewrites this file before every
    test and fsyncs it, so the last successful write names where the crash was.
    """
    crumb = tmp_path / "active-test.txt"
    crumb.write_text("tests/test_thing.py::test_that_segfaulted\n")
    monkeypatch.setenv("GENESIS_ACTIVE_TEST_FILE", str(crumb))

    out = _run(str(tmp_path / "does-not-exist.xml"))
    assert out.returncode == 0
    assert "tests/test_thing.py::test_that_segfaulted" in out.stdout, (
        f"a crash with no report named nothing — breadcrumb unread: {out.stdout}"
    )


def test_a_crash_with_no_breadcrumb_still_says_something_useful(tmp_path, monkeypatch):
    """The control that moves. A collection crash dies before any test starts, so
    there is no breadcrumb — the notice must still render and must NOT claim a
    test that never ran."""
    monkeypatch.delenv("GENESIS_ACTIVE_TEST_FILE", raising=False)

    out = _run(str(tmp_path / "does-not-exist.xml"))
    assert out.returncode == 0
    assert "no report at" in out.stdout
    assert "last test to START" not in out.stdout, (
        "claimed an active test when there was no breadcrumb"
    )


def test_the_breadcrumb_hook_records_the_node_id(tmp_path, monkeypatch):
    """The writer half. Without this, the reader tests above would pass against a
    hook that never wrote anything."""
    import tests.conftest as genesis_conftest

    crumb = tmp_path / "active-test.txt"
    monkeypatch.setenv("GENESIS_ACTIVE_TEST_FILE", str(crumb))
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_y", ("x", 1, "y"))
    assert crumb.read_text().strip() == "tests/test_x.py::test_y"

    # INERT without the variable — a local run must pay nothing.
    monkeypatch.delenv("GENESIS_ACTIVE_TEST_FILE", raising=False)
    crumb.unlink()
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_z", ("x", 1, "z"))
    assert not crumb.exists()
