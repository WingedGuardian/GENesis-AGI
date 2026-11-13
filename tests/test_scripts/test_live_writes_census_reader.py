"""The census receipt boundary rejects incomplete or stale evidence."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_writes_report as codec

RUN_ID = "owned-current-attempt-and-source"
ROW = ("test::owned", "~/.genesis", "write", "/owned/raw-\udcff")


def _observer(run_id=RUN_ID):
    return {"run_id": run_id, "installed": True, "disarmed": "", "faults": 0, "exitstatus": 0}


def _receipt(tmp_path, *, workers=0, rows=()):
    path = tmp_path / "rows.tsv"
    payload = codec.encode(rows)
    path.write_bytes(payload)
    ids = [f"gw{index}" for index in range(workers)]
    state = {**_observer(), "schema": 1, "format": "escaped-tsv-v1", "row_count": len(rows),
             "sha256": hashlib.sha256(payload).hexdigest(), "unreported": [],
             "expected_workers": ids, "merged_workers": ids,
             "workers": {worker: _observer() for worker in ids}}
    _write_state(path, state)
    return path, state


def _write_state(path, state):
    Path(str(path) + ".status.json").write_text(json.dumps(state))


def _assess(path, *, workers=0, outcome="success", run_id=RUN_ID):
    from scripts.ci.live_writes_census import assess

    return assess(path, run_id=run_id, workers=workers, tests_outcome=outcome)


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("rows", [(), (ROW,)])
def test_complete_receipts_distinguish_zero_and_nonempty(tmp_path, workers, rows):
    path, _ = _receipt(tmp_path, workers=workers, rows=rows)
    result = _assess(path, workers=workers)
    assert result["verdict"] == ("nonempty" if rows else "clean")
    assert result["rows"] == len(rows)


@pytest.mark.parametrize("field,value", [
    ("run_id", "earlier"), ("run_id", None), ("installed", False), ("installed", 1),
    ("disarmed", "unknown home"), ("disarmed", None), ("faults", 1), ("faults", False),
    ("faults", None), ("exitstatus", 1), ("exitstatus", False), ("exitstatus", None),
    ("schema", 0), ("schema", True), ("format", "legacy"), ("row_count", False),
    ("row_count", 1), ("sha256", "mismatch"), ("unreported", ["gw-missing"]),
    ("unreported", None), ("expected_workers", None), ("merged_workers", None),
    ("workers", None), ("workers", {"unexpected": None}),
])
def test_incomplete_controller_or_pair_cannot_qualify(tmp_path, field, value):
    path, state = _receipt(tmp_path)
    state[field] = value
    _write_state(path, state)
    assert _assess(path)["verdict"] == "incomplete"


@pytest.mark.parametrize("field", ["faults", "exitstatus", "installed", "disarmed", "workers", "expected_workers", "merged_workers"])
def test_missing_fields_do_not_default_to_clean(tmp_path, field):
    path, state = _receipt(tmp_path)
    del state[field]
    _write_state(path, state)
    assert _assess(path)["verdict"] == "incomplete"


@pytest.mark.parametrize("field,value", [
    ("run_id", "earlier"), ("installed", False), ("installed", 1), ("disarmed", "off"),
    ("faults", 1), ("faults", False), ("exitstatus", 1), ("exitstatus", False),
])
def test_every_worker_must_supply_current_active_clean_state(tmp_path, field, value):
    path, state = _receipt(tmp_path, workers=1)
    state["workers"]["gw0"][field] = value
    _write_state(path, state)
    assert _assess(path, workers=1)["verdict"] == "incomplete"


@pytest.mark.parametrize("case", ["missing", "legacy", "extra", "duplicate", "wrong-denominator", "empty-id"])
def test_worker_participation_is_a_denominator(tmp_path, case):
    path, state = _receipt(tmp_path, workers=1)
    denominator = 1
    if case == "missing":
        state["merged_workers"] = []
        state["workers"] = {}
    elif case == "legacy":
        state["workers"]["gw0"] = None
    elif case == "extra":
        state["workers"]["gw-extra"] = _observer()
    elif case == "duplicate":
        state["expected_workers"] = ["gw0", "gw0"]
    elif case == "empty-id":
        state["expected_workers"] = [""]
    else:
        denominator = 2
    _write_state(path, state)
    assert _assess(path, workers=denominator)["verdict"] == "incomplete"


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled"])
def test_actual_failed_test_step_overrules_an_earlier_exit_zero_receipt(tmp_path, outcome):
    path, _ = _receipt(tmp_path)
    assert _assess(path, outcome=outcome)["verdict"] == "incomplete"


@pytest.mark.parametrize("case", ["data-missing", "status-missing", "status-partial", "status-list", "malformed-data", "changed-data", "absent-id"])
def test_absent_or_partial_publication_is_not_empty_clean(tmp_path, case):
    path, _ = _receipt(tmp_path)
    status = Path(str(path) + ".status.json")
    if case == "data-missing":
        path.unlink()
    elif case == "status-missing":
        status.unlink()
    elif case == "status-partial":
        status.write_bytes(b'{"schema":')
    elif case == "status-list":
        status.write_text("[]")
    elif case == "malformed-data":
        path.write_bytes(b"bad\trow\n")
    elif case == "changed-data":
        path.write_bytes(codec.encode([ROW]))
    assert _assess(path, run_id="" if case == "absent-id" else RUN_ID)["verdict"] == "incomplete"


@pytest.mark.parametrize("case,expected", [("clean", 0), ("nonempty", 1), ("off", 2), ("missing", 2)])
def test_real_cli_returns_counts_without_echoing_rows_or_state(tmp_path, case, expected):
    path, state = _receipt(tmp_path, rows=[ROW] if case == "nonempty" else [])
    if case == "off":
        state["disarmed"] = "synthetic-private-marker"
        _write_state(path, state)
    elif case == "missing":
        Path(str(path) + ".status.json").unlink()
    script = Path(__file__).resolve().parents[2] / "scripts/ci/live_writes_census.py"
    run = subprocess.run([sys.executable, str(script), str(path), "--run-id", RUN_ID,
                          "--workers", "0", "--tests-outcome", "success"], capture_output=True)
    assert run.returncode == expected, run.stderr
    result = json.loads(run.stdout)
    assert result["verdict"] == ("clean" if case == "clean" else "nonempty" if case == "nonempty" else "incomplete")
    assert b"synthetic-private-marker" not in run.stdout + run.stderr
    assert b"/owned/raw" not in run.stdout + run.stderr
