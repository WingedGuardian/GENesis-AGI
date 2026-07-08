"""Tests for scripts/edit_failure_sensor.py — the Edit/Write outcome sensor.

The sensor was blind to failures for its first 7 weeks (12,737 rows, 0
failures) because it was registered only for PostToolUse, which fires solely
on successful tool calls. These tests pin the failure path (PostToolUseFailure)
and the success path against a real temp SQLite DB.
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

_SCHEMA = """
CREATE TABLE tool_call_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    file_path TEXT,
    success INTEGER NOT NULL,
    error_snippet TEXT,
    timestamp TEXT NOT NULL
);
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
            "SELECT tool_name, file_path, success, error_snippet FROM tool_call_outcomes"
        )
        return cur.fetchall()
    finally:
        conn.close()


def _run_process(monkeypatch, db_path, payload):
    module = _load_module()
    monkeypatch.setattr(module, "_DB_PATH", db_path)
    module._process(payload)


class TestFailurePath:
    def test_post_tool_use_failure_records_failure(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Edit",
                "session_id": "s1",
                "tool_input": {"file_path": "/tmp/x.py"},
                "tool_error": "String to replace not found in file (old_string not found)",
                "tool_error_code": 1,
            },
        )
        rows = _rows(sensor_db)
        assert rows == [
            ("Edit", "/tmp/x.py", 0, "String to replace not found in file (old_string not found)")
        ]

    def test_failure_without_error_text_still_records(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/y.py"},
            },
        )
        rows = _rows(sensor_db)
        assert rows[0][2] == 0
        assert "no error text" in rows[0][3]

    def test_error_snippet_capped_at_200(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/z.py"},
                "tool_error": "x" * 500,
            },
        )
        assert len(_rows(sensor_db)[0][3]) == 200

    def test_non_edit_write_failure_ignored(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "tool_error": "exit 1",
            },
        )
        assert _rows(sensor_db) == []


class TestSuccessPath:
    def test_post_tool_use_records_success(self, monkeypatch, sensor_db):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "session_id": "s2",
                "tool_input": {"file_path": "/tmp/a.py"},
                "tool_output": "The file /tmp/a.py has been updated.",
            },
        )
        assert _rows(sensor_db) == [("Edit", "/tmp/a.py", 1, None)]

    def test_soft_error_marker_in_success_output_records_failure(
        self, monkeypatch, sensor_db
    ):
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/b.py"},
                "tool_output": "Error: old_string not found",
            },
        )
        assert _rows(sensor_db)[0][2] == 0

    def test_missing_event_name_defaults_to_success_path(self, monkeypatch, sensor_db):
        # Older payloads without hook_event_name must keep working.
        _run_process(
            monkeypatch,
            sensor_db,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/c.py"},
                "tool_output": "ok",
            },
        )
        assert _rows(sensor_db)[0][2] == 1


class TestSubprocessEndToEnd:
    def test_stdin_pipe_failure_event(self, sensor_db):
        """Drive the real script as CC does: JSON on stdin, env-pointed DB."""
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Edit",
                "session_id": "e2e",
                "tool_input": {"file_path": "/tmp/e2e.py"},
                "tool_error": "old_string not found",
            }
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env={"GENESIS_DB_PATH": str(sensor_db), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0
        assert _rows(sensor_db) == [("Edit", "/tmp/e2e.py", 0, "old_string not found")]

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
