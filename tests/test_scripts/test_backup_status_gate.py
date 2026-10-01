from __future__ import annotations

import json
import subprocess
from pathlib import Path

_GATE = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "backup_status_gate.py"


def _gate(tmp_path, payload, run_id="run-1"):
    status = tmp_path / "status.json"
    status.write_text(json.dumps(payload))
    return subprocess.run(
        ["python3", str(_GATE), str(status), run_id],
        capture_output=True,
        text=True,
    )


def _base(**overrides):
    payload = {
        "run_id": "run-1",
        "success": True,
        "db_integrity_status": "healthy",
        "sqlite_backup_verified": True,
        "failure_class": "none",
        "failure_stage": "",
        "tier1_pushed": True,
        "tier2_backend": "local",
        "tier2_status": "ok",
    }
    payload.update(overrides)
    return payload


def test_gate_accepts_verified_success(tmp_path):
    result = _gate(tmp_path, _base())
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_gate_aborts_on_stale_or_unverified_status(tmp_path):
    stale = _gate(tmp_path, _base(run_id="older"))
    assert stale.returncode == 2
    assert stale.stdout.strip().startswith("abort_db:")

    unverified = _gate(tmp_path, _base(sqlite_backup_verified=False))
    assert unverified.returncode == 2
    assert unverified.stdout.strip().startswith("abort_db:")


def test_gate_continues_loudly_for_non_db_failure(tmp_path):
    result = _gate(
        tmp_path,
        _base(
            success=False,
            failure_class="non_db",
            failure_stage="qdrant_snapshot",
        ),
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "continue_degraded:backup:qdrant_snapshot"


def test_gate_treats_partial_offsite_as_degraded_even_if_local_success(tmp_path):
    result = _gate(tmp_path, _base(tier2_status="partial"))
    assert result.returncode == 1
    assert result.stdout.strip() == "continue_degraded:backup:tier2"


def test_gate_treats_unconfirmed_tier1_as_degraded(tmp_path):
    result = _gate(tmp_path, _base(tier1_pushed=False))
    assert result.returncode == 1
    assert result.stdout.strip() == "continue_degraded:backup:tier1"


def test_update_surfaces_and_persists_non_aborting_backup_failure():
    update = (_GATE.parents[1] / "update.sh").read_text()
    assert "BACKUP FAILED — UPDATE CONTINUING IN DEGRADED MODE" in update
    assert "scripts/lib/backup_status_gate.py" in update
    assert 'degraded="${degraded:+$degraded,}$PRE_UPDATE_DEGRADED"' in update
    assert "backup.sh\" 2>&1 | tail" not in update
