"""Account for every collected test when CI retries failures (#5024).

CI runs the suite with ``--reruns=1`` (pytest-rerunfailures), so a test that fails
once and then passes no longer fails the run. Two things then need a record that
the junit report cannot give:

- WHICH tests passed only on their retry. In junit a retried test is two
  ``<testcase>`` elements, and the first attempt is an EMPTY element that reads
  as a pass, so a reader cannot tell a flake from two passes.
- Whether every collected test reached a result at all. Under xdist with
  ``--max-worker-restart=0`` and retries on, one probe run ended green with a
  crashed worker's test absent from the junit report. MEASURED 2026-11-24 with
  pytest 9.2.0, xdist 3.8.0 and pytest-rerunfailures 16.7: observed once, then 0
  of 15 attempts. The mechanism is not established (the plugin's source reports a
  crash under loadfile as a failure), so this check is kept for the observed
  outcome, not for a known cause.

This plugin runs on the controller only (xdist re-fires ``pytest_runtest_logreport``
there for every worker's report, including the ``when="???"`` report of a crashed
worker's test). It records the final outcome of every node id and the ids that had
a retry. At session end it writes a JSON ledger, and when fewer node ids reached an
outcome than were collected it turns a passing exit status into a failure: a
dropped test must never leave the run green.

INERT unless ``GENESIS_RERUN_LEDGER`` names the output file, and inert in a NESTED
pytest run that inherited the variable (the suite launches some), by the same
owner-pid idiom as the active-test breadcrumb in ``tests/conftest.py``.
``scripts/ci/flaky_reruns.py`` reads the ledger.

"Collected" is the set of node ids that reached the runner. A module that fails
to import contributes none, so it is not counted as missing here; the run still
fails on the collection error itself.
"""

from __future__ import annotations

import contextlib
import json
import os

import pytest

LEDGER_ENV = "GENESIS_RERUN_LEDGER"
OWNER_ENV = "GENESIS_RERUN_LEDGER_OWNER"

_final: dict[str, str] = {}
_retried: set[str] = set()
# Node ids that should get an outcome: session.items on a serial run, the
# workers' collections under xdist (the controller collects nothing itself).
_collected: set[str] = set()


def _claim() -> None:
    if not os.environ.get(LEDGER_ENV) or os.environ.get(OWNER_ENV):
        return  # off, or inherited from an outer pytest that owns the ledger
    os.environ[OWNER_ENV] = str(os.getpid())


def _owns() -> bool:
    return bool(os.environ.get(LEDGER_ENV)) and os.environ.get(OWNER_ENV) == str(os.getpid())


with contextlib.suppress(Exception):  # import time: a raise here would be a collection error
    _claim()


def pytest_collection_finish(session):
    if _owns() and not hasattr(session.config, "workerinput"):
        _collected.update(item.nodeid for item in session.items)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    if _owns():
        _collected.update(ids)


def pytest_runtest_logreport(report):
    if not _owns():
        return
    if report.outcome == "rerun":
        # This attempt does not count: a retry that is queued but never runs
        # must leave the test with no outcome, whatever an earlier phase said.
        _retried.add(report.nodeid)
        _final.pop(report.nodeid, None)
        return
    # A setup or teardown failure, a crashed worker's "???" report, and the call
    # phase each decide the outcome; a later passing phase never undoes a failure.
    if report.when == "setup":
        # A new attempt begins: whatever an earlier, retried attempt left (a
        # teardown skip after its failed call, say) no longer counts.
        _final.pop(report.nodeid, None)
    decides = report.outcome != "passed" or report.when == "call"
    if decides and _final.get(report.nodeid, "passed") == "passed":
        _final[report.nodeid] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    if not _owns() or hasattr(session.config, "workerinput"):
        return
    collected = session.testscollected
    if _collected:
        # Judged by id, so an outcome for an id nobody collected cannot hide a
        # collected id that got none.
        missing_ids = sorted(_collected - _final.keys())
    else:
        missing_ids = ["<ids unavailable: count only>"] * max(collected - len(_final), 0)
    missing = len(missing_ids)
    ledger = {
        "collected": collected,
        "with_outcome": len(_final),
        "missing": missing,
        "missing_ids": missing_ids,
        "judged_by": "ids" if _collected else "count",
        "rerun_passed": sorted(n for n in _retried if _final.get(n) == "passed"),
        "rerun_failed": sorted(n for n in _retried if _final.get(n) not in (None, "passed")),
    }
    try:
        with open(os.environ[LEDGER_ENV], "w", encoding="utf-8", errors="surrogateescape") as fh:
            json.dump(ledger, fh, indent=1, sort_keys=True)
    except OSError:
        # The reader treats a missing ledger as a failed check, so a write
        # failure is still loud there; it must not crash the session here.
        pass
    if missing and session.exitstatus == 0:
        session.exitstatus = 1
