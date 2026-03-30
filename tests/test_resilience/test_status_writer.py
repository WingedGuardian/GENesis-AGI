"""Tests for StatusFileWriter."""

from __future__ import annotations

import json

import pytest

from genesis.resilience.state import (
    CCStatus,
    EmbeddingStatus,
    ResilienceStateMachine,
)
from genesis.resilience.status_writer import StatusFileWriter


@pytest.fixture
def sm():
    return ResilienceStateMachine()


class TestStatusFileWriter:
    async def test_correct_json_structure(self, sm, tmp_path):
        path = tmp_path / "status.json"
        writer = StatusFileWriter(state_machine=sm, path=str(path))
        await writer.write()

        data = json.loads(path.read_text())
        assert "timestamp" in data
        assert data["resilience_state"]["cloud"] == "NORMAL"
        assert data["resilience_state"]["memory"] == "NORMAL"
        assert data["resilience_state"]["embedding"] == "NORMAL"
        assert data["resilience_state"]["cc"] == "NORMAL"
        assert data["queue_depths"]["deferred_work"] == 0
        assert data["queue_depths"]["dead_letter"] == 0
        assert data["queue_depths"]["pending_embeddings"] == 0
        assert data["last_recovery"] is None
        assert "normal" in data["human_summary"].lower()

    async def test_summary_reflects_worst_axis(self, sm, tmp_path):
        sm.update_embedding(EmbeddingStatus.QUEUED)
        sm.update_cc(CCStatus.THROTTLED)

        path = tmp_path / "status.json"
        writer = StatusFileWriter(state_machine=sm, path=str(path))
        await writer.write()

        data = json.loads(path.read_text())
        assert "embedding" in data["human_summary"].lower()
        assert "throttled" in data["human_summary"].lower()

    async def test_creates_parent_directory(self, sm, tmp_path):
        path = tmp_path / "subdir" / "deep" / "status.json"
        writer = StatusFileWriter(state_machine=sm, path=str(path))
        await writer.write()
        assert path.exists()

    async def test_handles_none_queues(self, sm, tmp_path):
        path = tmp_path / "status.json"
        writer = StatusFileWriter(
            state_machine=sm,
            deferred_queue=None,
            dead_letter=None,
            pending_embeddings_db=None,
            path=str(path),
        )
        await writer.write()
        data = json.loads(path.read_text())
        assert data["queue_depths"]["deferred_work"] == 0
        assert data["queue_depths"]["dead_letter"] == 0
        assert data["queue_depths"]["pending_embeddings"] == 0

    async def test_summary_reflects_failing_jobs(self, sm, tmp_path):
        """Jobs with 2+ consecutive failures appear in human_summary."""
        from unittest.mock import MagicMock

        rt = MagicMock()
        rt.job_health = {
            "calibration": {"consecutive_failures": 3, "last_error": "json parse"},
            "harvest": {"consecutive_failures": 0},
        }
        path = tmp_path / "status.json"
        writer = StatusFileWriter(state_machine=sm, runtime=rt, path=str(path))
        await writer.write()
        data = json.loads(path.read_text())
        assert "1 scheduled job(s) failing" in data["human_summary"]
        assert data["failing_jobs"] == ["calibration"]

    async def test_no_failing_jobs_shows_normal(self, sm, tmp_path):
        from unittest.mock import MagicMock

        rt = MagicMock()
        rt.job_health = {
            "calibration": {"consecutive_failures": 0},
        }
        path = tmp_path / "status.json"
        writer = StatusFileWriter(state_machine=sm, runtime=rt, path=str(path))
        await writer.write()
        data = json.loads(path.read_text())
        assert "normal" in data["human_summary"].lower()
        assert "failing_jobs" not in data

    async def test_no_runtime_is_safe(self, sm, tmp_path):
        """StatusFileWriter works without runtime (backward compat)."""
        path = tmp_path / "status.json"
        writer = StatusFileWriter(state_machine=sm, path=str(path))
        await writer.write()
        data = json.loads(path.read_text())
        assert "normal" in data["human_summary"].lower()

    async def test_includes_queue_depths(self, sm, tmp_path, db):
        from unittest.mock import AsyncMock

        deferred = AsyncMock()
        deferred.count_pending = AsyncMock(return_value=12)
        dead = AsyncMock()
        dead.get_pending_count = AsyncMock(return_value=3)

        path = tmp_path / "status.json"
        writer = StatusFileWriter(
            state_machine=sm,
            deferred_queue=deferred,
            dead_letter=dead,
            pending_embeddings_db=db,
            path=str(path),
        )
        await writer.write()
        data = json.loads(path.read_text())
        assert data["queue_depths"]["deferred_work"] == 12
        assert data["queue_depths"]["dead_letter"] == 3
        # pending_embeddings comes from actual db (should be 0 in empty db)
        assert data["queue_depths"]["pending_embeddings"] == 0
        assert "15 items queued" in data["human_summary"]
