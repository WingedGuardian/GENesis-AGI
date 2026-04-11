"""Tests for genesis.sentinel.shared — shared filesystem writes."""

from __future__ import annotations

import json
from pathlib import Path

from genesis.sentinel.shared import append_log, write_last_run, write_state_for_guardian


class TestWriteLastRun:
    def test_writes_json_file(self, tmp_path: Path):
        write_last_run(
            trigger_source="test",
            tier=2,
            diagnosis="test diagnosis",
            actions_taken=["restarted qdrant"],
            resolved=True,
            duration_s=15.3,
            session_id="s-123",
            shared_dir=tmp_path,
        )
        last_run = json.loads((tmp_path / "last_run.json").read_text())
        assert last_run["trigger_source"] == "test"
        assert last_run["tier"] == 2
        assert last_run["resolved"] is True
        assert last_run["duration_s"] == 15.3


class TestAppendLog:
    def test_appends_entries(self, tmp_path: Path):
        append_log({"event": "first"}, shared_dir=tmp_path)
        append_log({"event": "second"}, shared_dir=tmp_path)

        log_path = tmp_path / "sentinel_log.jsonl"
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "first"
        assert json.loads(lines[1])["event"] == "second"
        # Each line has a timestamp
        assert "timestamp" in json.loads(lines[0])

    def test_rotates_large_log(self, tmp_path: Path):
        log_path = tmp_path / "sentinel_log.jsonl"
        # Write a large initial file
        log_path.write_text("x" * 1_100_000)

        append_log({"event": "after_rotation"}, shared_dir=tmp_path)

        # Original should be rotated
        rotated = tmp_path / "sentinel_log.jsonl.1"
        assert rotated.exists()
        # New log should have just the new entry
        new_content = log_path.read_text().strip()
        assert json.loads(new_content)["event"] == "after_rotation"


class TestWriteStateForGuardian:
    def test_writes_state(self, tmp_path: Path):
        write_state_for_guardian(
            {"current_state": "investigating", "tier": 2},
            shared_dir=tmp_path,
        )
        state = json.loads((tmp_path / "sentinel_state.json").read_text())
        assert state["current_state"] == "investigating"
