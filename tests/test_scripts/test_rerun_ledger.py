"""tests/_rerun_ledger.py: every collected test is accounted for, retries are named (#5024).

The plugin is driven through real nested pytest runs, the way CI loads it, except
for the dropped-test case: that drop is a worker-crash race that cannot be forced
on demand, so its exit-status rule is checked by calling the hooks directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _rerun_ledger as ledger_mod

REPO = Path(__file__).resolve().parents[2]

_OUTCOMES = """
import pytest

def test_pass():
    pass

def test_fail():
    assert False

@pytest.mark.skip(reason="skipped on purpose")
def test_skip():
    pass

@pytest.mark.xfail(reason="expected")
def test_xfail():
    assert False

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

def test_setup_error(broken):
    pass
"""

_FLAKY = """
from pathlib import Path

def _attempt(name):
    counter = Path(__file__).with_name(name)
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    return n

def test_fails_once():
    assert _attempt("once.count") >= 1

def test_fails_twice():
    assert _attempt("twice.count") >= 2
"""


def _subrun(
    tmp_path: Path,
    files: dict[str, str],
    *extra: str,
    ledger: Path | None,
    owner: str | None = None,
):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    env = {**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}"}
    for key in (
        ledger_mod.LEDGER_ENV,
        ledger_mod.OWNER_ENV,
        "PYTEST_CURRENT_TEST",
        "PYTEST_ADDOPTS",
    ):
        env.pop(key, None)
    if ledger is not None:
        env[ledger_mod.LEDGER_ENV] = str(ledger)
    if owner is not None:
        env[ledger_mod.OWNER_ENV] = owner
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests._rerun_ledger",
            *extra,
            *(str(tmp_path / n) for n in files),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_serial_run_accounts_for_every_outcome(tmp_path):
    ledger = tmp_path / "ledger.json"
    out = _subrun(tmp_path, {"test_outcomes.py": _OUTCOMES}, ledger=ledger)
    assert out.returncode == 1, out.stdout + out.stderr  # test_fail and the setup error
    data = json.loads(ledger.read_text())
    assert data == {
        "collected": 5,
        "with_outcome": 5,
        "missing": 0,
        "missing_ids": [],
        "judged_by": "ids",
        "rerun_passed": [],
        "rerun_failed": [],
    }


def test_an_xdist_run_accounts_for_every_test_on_the_controller(tmp_path):
    pytest.importorskip("xdist")
    ledger = tmp_path / "ledger.json"
    files = {"test_one.py": _OUTCOMES, "test_two.py": _OUTCOMES}
    out = _subrun(
        tmp_path, files, "-n", "2", "--dist=loadfile", "--max-worker-restart=0", ledger=ledger
    )
    assert out.returncode == 1, out.stdout + out.stderr
    data = json.loads(ledger.read_text())
    assert (data["collected"], data["with_outcome"], data["missing"]) == (10, 10, 0)
    assert data["judged_by"] == "ids"  # the workers' collections reached the controller


def test_retries_are_named_by_their_final_outcome(tmp_path):
    pytest.importorskip("pytest_rerunfailures")
    pytest.importorskip("xdist")
    ledger = tmp_path / "ledger.json"
    out = _subrun(
        tmp_path,
        {"test_flaky.py": _FLAKY},
        "-n",
        "2",
        "--dist=loadfile",
        "--max-worker-restart=0",
        "--reruns=1",
        ledger=ledger,
    )
    assert out.returncode == 1, out.stdout + out.stderr  # test_fails_twice fails both attempts
    data = json.loads(ledger.read_text())
    assert data["rerun_passed"] == ["test_flaky.py::test_fails_once"]
    assert data["rerun_failed"] == ["test_flaky.py::test_fails_twice"]
    assert (data["collected"], data["with_outcome"], data["missing"]) == (2, 2, 0)


def test_unset_means_inert(tmp_path):
    out = _subrun(tmp_path, {"test_outcomes.py": _OUTCOMES}, ledger=None)
    assert out.returncode == 1, out.stdout + out.stderr
    assert list(tmp_path.glob("*.json")) == []


def test_a_nested_run_stands_down_for_the_outer_owner(tmp_path):
    """The suite launches nested pytest runs that inherit the environment; only
    the run that claimed the ledger may write it."""
    ledger = tmp_path / "ledger.json"
    out = _subrun(tmp_path, {"test_outcomes.py": _OUTCOMES}, ledger=ledger, owner="1")
    assert out.returncode == 1, out.stdout + out.stderr
    assert not ledger.exists()


@pytest.fixture
def owned_ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv(ledger_mod.LEDGER_ENV, str(path))
    monkeypatch.setenv(ledger_mod.OWNER_ENV, str(os.getpid()))
    monkeypatch.setattr(ledger_mod, "_final", {})
    monkeypatch.setattr(ledger_mod, "_retried", set())
    monkeypatch.setattr(ledger_mod, "_collected", set())
    return path


def _report(nodeid: str, when: str, outcome: str):
    return SimpleNamespace(nodeid=nodeid, when=when, outcome=outcome)


def _session(collected: int, exitstatus: int):
    return SimpleNamespace(
        testscollected=collected, exitstatus=exitstatus, config=SimpleNamespace()
    )


def test_a_collected_test_with_no_outcome_fails_a_green_run(owned_ledger):
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "call", "passed"))
    session = _session(collected=2, exitstatus=0)
    ledger_mod.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    assert json.loads(owned_ledger.read_text())["missing"] == 1


def test_an_outcome_for_an_uncollected_id_cannot_hide_a_missing_one(owned_ledger):
    ledger_mod.pytest_xdist_node_collection_finished(None, ["t.py::a", "t.py::b"])
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "call", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::stray", "call", "passed"))
    session = _session(collected=2, exitstatus=0)
    ledger_mod.pytest_sessionfinish(session, 0)
    data = json.loads(owned_ledger.read_text())
    assert (data["missing"], data["missing_ids"]) == (1, ["t.py::b"])
    assert session.exitstatus == 1


def test_a_retry_that_never_runs_leaves_no_outcome(owned_ledger):
    ledger_mod.pytest_xdist_node_collection_finished(None, ["t.py::a"])
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "setup", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "call", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "teardown", "rerun"))
    session = _session(collected=1, exitstatus=0)
    ledger_mod.pytest_sessionfinish(session, 0)
    assert json.loads(owned_ledger.read_text())["missing_ids"] == ["t.py::a"]
    assert session.exitstatus == 1


def test_a_red_run_keeps_its_own_exit_status(owned_ledger):
    session = _session(collected=1, exitstatus=2)
    ledger_mod.pytest_sessionfinish(session, 2)
    assert session.exitstatus == 2


def test_a_crashed_workers_report_counts_and_a_later_pass_never_undoes_a_failure(owned_ledger):
    ledger_mod.pytest_runtest_logreport(_report("t.py::crash", "???", "failed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::crash", "call", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::tear", "call", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::tear", "teardown", "failed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::tear", "teardown", "passed"))
    session = _session(collected=2, exitstatus=1)
    ledger_mod.pytest_sessionfinish(session, 1)
    data = json.loads(owned_ledger.read_text())
    assert (data["with_outcome"], data["missing"]) == (2, 0)
    assert ledger_mod._final == {"t.py::crash": "failed", "t.py::tear": "failed"}


def test_a_worker_never_writes(owned_ledger):
    session = _session(collected=3, exitstatus=0)
    session.config = SimpleNamespace(workerinput={})
    ledger_mod.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    assert not owned_ledger.exists()


def test_a_new_attempt_discards_what_the_retried_attempt_left(owned_ledger):
    """Round 1 (CodeRabbit): a retried attempt's teardown skip must not become the
    final outcome once the retry passes."""
    ledger_mod.pytest_xdist_node_collection_finished(None, ["t.py::a"])
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "setup", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "call", "rerun"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "teardown", "skipped"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "setup", "passed"))
    ledger_mod.pytest_runtest_logreport(_report("t.py::a", "call", "passed"))
    session = _session(collected=1, exitstatus=0)
    ledger_mod.pytest_sessionfinish(session, 0)
    data = json.loads(owned_ledger.read_text())
    assert data["rerun_passed"] == ["t.py::a"] and data["missing"] == 0
