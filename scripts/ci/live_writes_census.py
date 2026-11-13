#!/usr/bin/env python3
"""Read a scoped audit receipt: 0 clean, 1 nonempty, 2 incomplete.

CI must also retain this exact artifact and check raw preparation/test/reader/
upload outcomes. A green advisory step or parseable empty TSV is insufficient.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _active(state, run_id):
    return (isinstance(state, dict) and state.get("run_id") == run_id
            and state.get("installed") is True and state.get("disarmed") == ""
            and type(state.get("faults")) is int and state["faults"] == 0
            and type(state.get("exitstatus")) is int and state["exitstatus"] == 0)


def _ids(value):
    return (isinstance(value, list) and all(isinstance(item, str) and item for item in value)
            and len(value) == len(set(value)))


def _coverage(state, run_id, workers):
    expected, merged, records = (state.get(key) for key in ("expected_workers", "merged_workers", "workers"))
    return (_ids(expected) and _ids(merged) and isinstance(records, dict)
            and len(expected) == workers and set(expected) == set(merged) == set(records)
            and all(_active(record, run_id) for record in records.values()))


def _incomplete(reason, rows=None):
    return {"verdict": "incomplete", "rows": rows, "reason": reason}


def assess(report, *, run_id, workers, tests_outcome):
    from tests._live_writes_report import decode

    report = Path(report)
    try:
        payload = report.read_bytes()
        rows = decode(payload)
        state = json.loads(Path(str(report) + ".status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _incomplete("artifact")
    count = len(rows)
    if not run_id or type(workers) is not int or workers < 0 or tests_outcome != "success":
        return _incomplete("run-outcome", count)
    if not isinstance(state, dict):
        return _incomplete("metadata", count)
    if (type(state.get("schema")) is not int or state["schema"] != 1
            or state.get("format") != "escaped-tsv-v1"
            or not _active(state, run_id) or state.get("unreported") != []):
        return _incomplete("observation-state", count)
    if (type(state.get("row_count")) is not int or state["row_count"] != count
            or state.get("sha256") != hashlib.sha256(payload).hexdigest()):
        return _incomplete("data-binding", count)
    if not _coverage(state, run_id, workers):
        return _incomplete("worker-coverage", count)
    return {"verdict": "nonempty" if rows else "clean", "rows": count, "workers": workers}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--tests-outcome", choices=("success", "failure", "skipped", "cancelled"), required=True)
    args = parser.parse_args(argv)
    result = assess(args.report, run_id=args.run_id, workers=args.workers, tests_outcome=args.tests_outcome)
    print(json.dumps(result, sort_keys=True))  # counts/reason codes only, never report rows
    return {"clean": 0, "nonempty": 1, "incomplete": 2}[result["verdict"]]


if __name__ == "__main__":
    # A script starts with scripts/ci on sys.path; load this checkout's pure
    # codec rather than a similarly named installed tests package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
