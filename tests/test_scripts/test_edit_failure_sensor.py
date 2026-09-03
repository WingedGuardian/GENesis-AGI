"""Tests for scripts/edit_failure_sensor.py — the Edit/Write outcome sensor.

The sensor recorded 0 failures in 25k rows over ~8 weeks (#1597): on current
Claude Code a failed Edit fires NO PostToolUse/PostToolUseFailure hook. Both
successes and failures ARE in the session transcript, so the sensor now scans it
on the Stop event and records EVERY Edit/Write outcome (success=0 on is_error,
else success=1) — one population, deduped by tool_use_id. These tests pin that
path against a real temp SQLite DB, using byte-real transcript-record shapes.

Fixture ids are synthetic, low-entropy ``toolu_`` values on purpose — these
fixtures may reach the public repo; a real high-entropy id both leaks an
identifier and can trip the detect-secrets floor.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "edit_failure_sensor.py"

# Mirror of the live schema (incl. the #1597 dedup column + unique index) so the
# INSERT OR IGNORE dedup path is exercised exactly as in production.
_SCHEMA = """
CREATE TABLE tool_call_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    file_path TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error_snippet TEXT,
    timestamp TEXT NOT NULL,
    tool_use_id TEXT
);
CREATE UNIQUE INDEX idx_tco_tool_use_id ON tool_call_outcomes(tool_use_id);
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("edit_failure_sensor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sensor_db(tmp_path):
    db_path = tmp_path / "genesis.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT tool_name, file_path, success, error_snippet, tool_use_id "
            "FROM tool_call_outcomes ORDER BY id"
        )
        return cur.fetchall()
    finally:
        conn.close()


def _run_process(monkeypatch, db_path, payload):
    module = _load_module()
    monkeypatch.setattr(module, "_DB_PATH", db_path)
    module._process(payload)


# --- Transcript-fixture builders (byte-real shapes captured 2026-09-02) --------

_MISSING = object()


def _tool_use(tid, name="Edit", file_path="/tmp/x.py", sidechain=False):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "session_id": "s1",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": name,
                    "input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
                }
            ]
        },
    }


def _tool_result(tid, is_error=_MISSING, text="String to replace not found in file.", sidechain=False):
    """is_error=_MISSING → a SUCCESS result (real successes omit the key entirely).
    Pass True/1/"true" for a failure, or False/"false" for an explicit success."""
    result = {"type": "tool_result", "tool_use_id": tid}
    if is_error is not _MISSING:
        result["is_error"] = is_error
    failure = is_error is True or is_error == 1 or (isinstance(is_error, str) and is_error.lower() == "true")
    result["content"] = (
        f"<tool_use_error>{text}</tool_use_error>" if failure else "The file has been updated."
    )
    rec = {
        "type": "user",
        "isSidechain": sidechain,
        "session_id": "s1",
        "timestamp": "2026-09-02T22:00:00+00:00",
        "message": {"content": [result]},
    }
    if failure:
        rec["toolUseResult"] = f"Error: {text}"
    return rec


def _write_transcript(tmp_path, records, name="transcript.jsonl"):
    path = tmp_path / name
    with path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _stop_payload(transcript_path):
    return {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(transcript_path),
    }


# --- Stop outcome-scan path ---------------------------------------------------


class TestStopScan:
    def test_records_edit_failure(self, monkeypatch, sensor_db, tmp_path):
        """The key #1597 case: a real failed-Edit transcript pair → one success=0 row."""
        tp = _write_transcript(
            tmp_path,
            [_tool_use("toolu_fail01", file_path="/tmp/x.py"), _tool_result("toolu_fail01", is_error=True)],
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        rows = _rows(sensor_db)
        assert len(rows) == 1
        tool_name, file_path, success, snippet, tid = rows[0]
        assert (tool_name, file_path, success, tid) == ("Edit", "/tmp/x.py", 0, "toolu_fail01")
        assert "String to replace not found" in snippet

    def test_records_edit_success(self, monkeypatch, sensor_db, tmp_path):
        """Success (is_error omitted) → success=1 row, tool_use_id set, no snippet."""
        tp = _write_transcript(
            tmp_path, [_tool_use("toolu_ok01", file_path="/tmp/ok.py"), _tool_result("toolu_ok01")]
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert _rows(sensor_db) == [("Edit", "/tmp/ok.py", 1, None, "toolu_ok01")]

    def test_mixed_success_and_failure(self, monkeypatch, sensor_db, tmp_path):
        tp = _write_transcript(
            tmp_path,
            [
                _tool_use("toolu_s", name="Write", file_path="/tmp/s.py"),
                _tool_result("toolu_s"),
                _tool_use("toolu_f", file_path="/tmp/f.py"),
                _tool_result("toolu_f", is_error=True),
            ],
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        rows = {r[4]: r[2] for r in _rows(sensor_db)}
        assert rows == {"toolu_s": 1, "toolu_f": 0}

    @pytest.mark.parametrize(
        "is_error,expected_success",
        [
            (True, 0),      # JSON bool
            (1, 0),         # numeric drift
            ("true", 0),    # string drift
            (False, 1),     # explicit success
            ("false", 1),   # string false must NOT read as failure
            (_MISSING, 1),  # real successes omit the key
        ],
    )
    def test_is_error_classification(self, monkeypatch, sensor_db, tmp_path, is_error, expected_success):
        tp = _write_transcript(
            tmp_path, [_tool_use("toolu_v"), _tool_result("toolu_v", is_error=is_error)]
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert _rows(sensor_db)[0][2] == expected_success

    def test_idempotent_rescan(self, monkeypatch, sensor_db, tmp_path):
        """Stop re-fires every turn; re-scanning the same transcript stays stable."""
        tp = _write_transcript(
            tmp_path,
            [
                _tool_use("toolu_ok"), _tool_result("toolu_ok"),
                _tool_use("toolu_bad"), _tool_result("toolu_bad", is_error=True),
            ],
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert len(_rows(sensor_db)) == 2

    def test_sidechain_skipped(self, monkeypatch, sensor_db, tmp_path):
        """v1 is main-session only — sub-agent (isSidechain) outcomes are skipped, both
        success and failure, so the base-rate population stays symmetric."""
        tp = _write_transcript(
            tmp_path,
            [
                _tool_use("toolu_ss", sidechain=True), _tool_result("toolu_ss", sidechain=True),
                _tool_use("toolu_sf", sidechain=True), _tool_result("toolu_sf", is_error=True, sidechain=True),
            ],
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert _rows(sensor_db) == []

    def test_non_edit_write_ignored(self, monkeypatch, sensor_db, tmp_path):
        tp = _write_transcript(
            tmp_path,
            [
                _tool_use("toolu_b", name="Bash"), _tool_result("toolu_b"),
                _tool_use("toolu_bf", name="Bash"), _tool_result("toolu_bf", is_error=True),
            ],
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert _rows(sensor_db) == []

    def test_far_apart_tool_use_and_result(self, monkeypatch, sensor_db, tmp_path):
        filler = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}]
        records = (
            [_tool_use("toolu_far", file_path="/tmp/f.py")]
            + filler * 50
            + [_tool_result("toolu_far", is_error=True)]
        )
        tp = _write_transcript(tmp_path, records)
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        rows = _rows(sensor_db)
        assert len(rows) == 1 and rows[0][4] == "toolu_far"

    def test_malformed_lines_skipped(self, monkeypatch, sensor_db, tmp_path):
        tp = tmp_path / "t.jsonl"
        with tp.open("w") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps(_tool_use("toolu_ok")) + "\n")
            fh.write("\n")  # blank line
            fh.write(json.dumps(_tool_result("toolu_ok", is_error=True)) + "\n")
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        rows = _rows(sensor_db)
        assert len(rows) == 1 and rows[0][4] == "toolu_ok"

    def test_error_snippet_capped_at_200(self, monkeypatch, sensor_db, tmp_path):
        tp = _write_transcript(
            tmp_path, [_tool_use("toolu_long"), _tool_result("toolu_long", is_error=True, text="x" * 500)]
        )
        _run_process(monkeypatch, sensor_db, _stop_payload(tp))
        assert len(_rows(sensor_db)[0][3]) == 200

    def test_missing_transcript_path_no_rows(self, monkeypatch, sensor_db):
        _run_process(monkeypatch, sensor_db, {"hook_event_name": "Stop", "session_id": "s1"})
        assert _rows(sensor_db) == []

    def test_nonexistent_transcript_no_crash(self, monkeypatch, sensor_db, tmp_path):
        _run_process(monkeypatch, sensor_db, _stop_payload(tmp_path / "nope.jsonl"))
        assert _rows(sensor_db) == []


class TestNonScanEventsIgnored:
    """The sensor is Stop-only now; a stray PostToolUse payload must record nothing."""

    def test_post_tool_use_payload_ignored(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "session_id": "s2",
                "tool_input": {"file_path": "/tmp/a.py"},
                "tool_response": {"filePath": "/tmp/a.py"},
            },
        )
        assert _rows(sensor_db) == []

    def test_session_end_also_scans(self, monkeypatch, sensor_db, tmp_path):
        """SessionEnd is an accepted fallback trigger (also carries transcript_path)."""
        tp = _write_transcript(
            tmp_path, [_tool_use("toolu_se"), _tool_result("toolu_se", is_error=True)]
        )
        _run_process(
            monkeypatch,
            sensor_db,
            {"hook_event_name": "SessionEnd", "session_id": "s1", "transcript_path": str(tp)},
        )
        assert len(_rows(sensor_db)) == 1


# --- Subprocess E2E (drive the real script over stdin, as CC does) ------------


class TestSubprocessEndToEnd:
    def test_stop_scan_via_stdin(self, sensor_db, tmp_path):
        tp = _write_transcript(
            tmp_path,
            [
                _tool_use("toolu_e2eok", file_path="/tmp/ok.py"), _tool_result("toolu_e2eok"),
                _tool_use("toolu_e2ebad", file_path="/tmp/bad.py"),
                _tool_result("toolu_e2ebad", is_error=True),
            ],
        )
        payload = json.dumps(_stop_payload(tp))
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env={"GENESIS_DB_PATH": str(sensor_db), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0
        rows = {r[4]: r[2] for r in _rows(sensor_db)}
        assert rows == {"toolu_e2eok": 1, "toolu_e2ebad": 0}

    def test_malformed_stdin_never_raises(self, sensor_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="{not json",
            capture_output=True,
            text=True,
            timeout=30,
            env={"GENESIS_DB_PATH": str(sensor_db), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0
        assert _rows(sensor_db) == []
