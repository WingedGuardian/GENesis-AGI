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
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "ci" / "failure_summary.py"

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
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_FILE_ENV, str(crumb))
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, str(os.getpid()))
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_y", ("x", 1, "y"))
    assert crumb.read_text().strip() == "tests/test_x.py::test_y"

    # INERT without the variable — a local run must pay nothing.
    monkeypatch.delenv(genesis_conftest.ACTIVE_TEST_FILE_ENV, raising=False)
    crumb.unlink()
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_z", ("x", 1, "z"))
    assert not crumb.exists()


def test_a_process_that_does_not_own_the_breadcrumb_does_not_write_it(
    tmp_path, monkeypatch
):
    """The predicate, in-process and both ways round.

    A nested pytest run inherits the breadcrumb PATH, so the path alone cannot
    decide who may write. Ownership is the pid comparison, and this asserts the
    hook consults it — the subprocess test below proves it in a real nested run,
    but this one localises a regression to the predicate itself.
    """
    import tests.conftest as genesis_conftest

    crumb = tmp_path / "active-test.txt"
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_FILE_ENV, str(crumb))
    # An owner that is NOT this process: exactly what a child inherits.
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, str(os.getpid() + 1))
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_nested", ("x", 1, "y"))
    assert not crumb.exists(), "a non-owner wrote the breadcrumb"

    # CONTROL that moves: same call, ownership taken.
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, str(os.getpid()))
    genesis_conftest.pytest_runtest_logstart("tests/test_x.py::test_owned", ("x", 1, "y"))
    assert crumb.read_text().strip() == "tests/test_x.py::test_owned"


def test_the_claim_stands_down_when_a_parent_pytest_already_owns_it(tmp_path, monkeypatch):
    """The claim half: the mechanism that makes a child a non-owner in the first
    place. Presence of an inherited owner must not be overwritten."""
    import tests.conftest as genesis_conftest

    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_FILE_ENV, str(tmp_path / "c.txt"))
    monkeypatch.delenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, raising=False)

    # Unclaimed -> this process takes it.
    genesis_conftest._claim_active_test_breadcrumb()
    assert os.environ[genesis_conftest.ACTIVE_TEST_OWNER_ENV] == str(os.getpid())

    # Already claimed by someone else -> left alone, so we stay a non-owner.
    monkeypatch.setenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, "424242")
    genesis_conftest._claim_active_test_breadcrumb()
    assert os.environ[genesis_conftest.ACTIVE_TEST_OWNER_ENV] == "424242"
    assert not genesis_conftest._owns_active_test_breadcrumb()

    # And no breadcrumb path at all -> nothing is claimed.
    monkeypatch.delenv(genesis_conftest.ACTIVE_TEST_FILE_ENV, raising=False)
    monkeypatch.delenv(genesis_conftest.ACTIVE_TEST_OWNER_ENV, raising=False)
    genesis_conftest._claim_active_test_breadcrumb()
    assert genesis_conftest.ACTIVE_TEST_OWNER_ENV not in os.environ


#: A small, fast module for the nested-run tests to aim a child pytest at. It
#: only has to make at least one test START, which is what fires the hook.
_CHILD_TARGET = ["tests/test_env.py", "-k", "timezone or user_timezone"]


def _child_pytest(crumb: Path, *, owner: str | None) -> subprocess.CompletedProcess[str]:
    """A real nested pytest run, exactly as this suite's own tests spawn one.

    GENESIS_PYTEST_LOCK=0 because the box-wide test lock is already held by the
    run executing this test — the same reason test_pytest_lock gives.
    """
    import tests.conftest as genesis_conftest

    env = {
        **os.environ,
        genesis_conftest.ACTIVE_TEST_FILE_ENV: str(crumb),
        "GENESIS_PYTEST_LOCK": "0",
    }
    if owner is None:
        env.pop(genesis_conftest.ACTIVE_TEST_OWNER_ENV, None)
    else:
        env[genesis_conftest.ACTIVE_TEST_OWNER_ENV] = owner
    return subprocess.run(
        [sys.executable, "-m", "pytest", *_CHILD_TARGET, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=600,
    )


def test_a_nested_pytest_run_does_not_replace_the_outer_breadcrumb(tmp_path):
    """The defect, replayed. This suite launches child pytest runs with
    ``{**os.environ, ...}`` (test_pytest_lock, test_proactive_hook_bounded_output),
    so before ownership the child rewrote the outer session's breadcrumb with its
    OWN node ids — and a hard crash in the still-running outer test was then
    reported under the child's last test. A confident wrong name, which is worse
    than no name, because the notice around it reads identically either way.
    """
    crumb = tmp_path / "active-test.txt"
    outer = "tests/test_outer.py::test_the_one_that_actually_crashed"
    crumb.write_text(outer + "\n")

    proc = _child_pytest(crumb, owner=str(os.getpid()))
    # Guard-the-guard: a child that never ran would satisfy the assertion below
    # vacuously, which is the whole failure mode this test exists to avoid.
    combined = proc.stdout + proc.stderr
    assert re.search(r"\d+ (passed|failed|error)", combined), (
        f"the child pytest produced no result line, so it never ran:\n{combined[-800:]}"
    )

    assert crumb.read_text().strip() == outer, (
        "a nested pytest run overwrote the outer session's breadcrumb — a crash "
        f"in {outer} would now be reported under the child's last test"
    )


def test_an_unclaimed_breadcrumb_is_still_written_by_the_run_that_owns_it(tmp_path):
    """The control that moves, and the acceptance bar for the feature itself.

    Without this, an implementation that simply never wrote would pass the test
    above. Same child, same file, one variable different: with no inherited
    owner the child claims the breadcrumb and records its own node ids.
    """
    crumb = tmp_path / "active-test.txt"
    crumb.write_text("tests/test_outer.py::test_stale\n")

    proc = _child_pytest(crumb, owner=None)
    combined = proc.stdout + proc.stderr
    assert re.search(r"\d+ (passed|failed|error)", combined), (
        f"the child pytest produced no result line, so it never ran:\n{combined[-800:]}"
    )

    written = crumb.read_text().strip()
    assert written.startswith("tests/test_env.py::"), (
        f"the owning run did not record its own node id — breadcrumb says {written!r}"
    )


def test_a_long_node_id_is_named_whole(tmp_path, monkeypatch):
    """The 200-char bound cut 54 real ids (0.2% of 26,798) across 18 files.

    MEASURED 2026-09-16 on this suite: p50 91, p99 173, p99.9 215. A cut lands
    in the SUFFIX, which is precisely the part that distinguishes one
    parametrised case from its siblings — so the summary named a case that was
    not the case that crashed, and read as though it were whole.
    """
    node_id = "tests/test_hooks/test_guard_ansic_fail_closed.py::test_x[" + "p" * 220 + "]"
    assert len(node_id) > 200, "fixture no longer exercises the old bound"
    crumb = tmp_path / "active-test.txt"
    crumb.write_text(node_id + "\n")
    monkeypatch.setenv("GENESIS_ACTIVE_TEST_FILE", str(crumb))

    out = _run(str(tmp_path / "does-not-exist.xml"))
    assert out.returncode == 0
    assert node_id in out.stdout, "the node id was cut — the summary names the wrong case"
    assert "TRUNCATED" not in out.stdout


def test_a_breadcrumb_beyond_the_ceiling_declares_its_cut(tmp_path, monkeypatch):
    """The ceiling is a RESOURCE guard, and the one case where cutting is right:
    the longest id this suite collects is 200,105 characters — a parametrised
    case whose parameter is a 200 KB string, which is a payload, not a name.
    GitHub renders NOTHING when a step summary passes 1 MiB, so an unbounded
    paste is a way to lose the whole summary. What must never happen is a SILENT
    cut, so the notice states the true length.
    """
    node_id = "tests/test_hooks/test_hook_output.py::test_big[" + "z" * 9000 + "]"
    crumb = tmp_path / "active-test.txt"
    crumb.write_text(node_id + "\n")
    monkeypatch.setenv("GENESIS_ACTIVE_TEST_FILE", str(crumb))

    out = _run(str(tmp_path / "does-not-exist.xml"))
    assert out.returncode == 0
    # The BRACKETED clause, not the bare word: a node id is attacker-adjacent
    # text here (it is whatever a parametrised case is named), so an id merely
    # containing "TRUNCATED" must not be able to satisfy this.
    assert f"[TRUNCATED — the node id is {len(node_id)} characters" in out.stdout, (
        f"an oversized id was cut with no usable declaration: {out.stdout[-300:]!r}"
    )
    assert len(out.stdout) < 20000, "the ceiling did not bound the notice"


def test_the_narrator_fires_only_when_the_TEST_step_failed():
    """Bare ``failure()`` is true after ANY earlier step in the job fails.

    Checkout, setup-python and the dependency install all run before the test
    step. Unscoped, a broken install makes this narrator announce that pytest
    died before writing a report about a pytest that never started — and a
    broken CHECKOUT leaves no script and no interpreter, so the narrator itself
    goes red: a second failure invented by the one step whose entire contract is
    that it never adds one.
    """
    import yaml

    workflow = yaml.safe_load((_REPO / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]

    by_name = {s.get("name"): s for s in steps if s.get("name")}
    narrator = by_name.get("Name the failures where truncation cannot reach")
    assert narrator is not None, "the narrator step was renamed — re-aim this test"

    tests_step = next((s for s in steps if s.get("id") == "tests"), None)
    assert tests_step is not None, (
        "no step carries id: tests, so the narrator cannot be scoped to it"
    )
    assert "pytest" in tests_step.get("run", ""), "id: tests is not on the pytest step"

    condition = narrator["if"]
    assert "steps.tests.outcome" in condition, (
        f"the narrator is not scoped to the test step's outcome: {condition!r} — it "
        "will fire on a dependency-install or checkout failure too"
    )
    # An `if` with NO status-check function gets an implicit success() AND-ed
    # onto it, so an outcome test on its own would never run after a failure --
    # the step would go permanently silent in exactly the case it exists for.
    assert any(fn in condition for fn in ("cancelled()", "failure()", "always()")), (
        f"the narrator condition carries no status-check function: {condition!r} — "
        "GitHub will AND an implicit success() onto it and the step will never run "
        "after a failure"
    )
