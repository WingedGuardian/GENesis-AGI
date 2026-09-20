#!/usr/bin/env python3
"""Classify one exact pre-update backup run from its durable status."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _abort(reason: str) -> int:
    print(f"abort_db:{reason}")
    return 2


def main() -> int:
    if len(sys.argv) != 3:
        return _abort("gate_usage")
    status_path = Path(sys.argv[1])
    expected_run_id = sys.argv[2]
    try:
        status = json.loads(status_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _abort("status_missing_or_invalid")
    if not isinstance(status, dict) or status.get("run_id") != expected_run_id:
        return _abort("status_run_mismatch")
    if status.get("db_integrity_status") != "healthy":
        stage = status.get("failure_stage") or "integrity_indeterminate"
        return _abort(str(stage))
    if status.get("sqlite_backup_verified") is not True:
        stage = status.get("failure_stage") or "sqlite_not_verified"
        return _abort(str(stage))
    if status.get("failure_class") in {"db_integrity", "db_backup"}:
        stage = status.get("failure_stage") or "database_backup_failed"
        return _abort(str(stage))
    if status.get("success") is not True:
        stage = status.get("failure_stage") or "non_db_failure"
        print(f"continue_degraded:backup:{stage}")
        return 1
    if status.get("tier1_pushed") is not True:
        print("continue_degraded:backup:tier1")
        return 1
    if status.get("tier2_backend") not in {None, "none"} and status.get("tier2_status") != "ok":
        print("continue_degraded:backup:tier2")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
