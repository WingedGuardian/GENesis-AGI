"""Tests for the ingestion worker bridge (ingest_upload.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.db.crud import knowledge_uploads
from genesis.db.schema import create_all_tables


@dataclass
class FakeIngestResult:
    source: str = ""
    source_type: str = "text"
    units_created: int = 2
    unit_ids: list[str] = field(default_factory=lambda: ["u1", "u2"])
    quality_flags: list[str] = field(default_factory=list)
    error: str | None = None


@pytest.fixture
async def db():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = None
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def upload_id(db):
    uid = await knowledge_uploads.insert(
        db, filename="test.txt", file_path="/tmp/test.txt", file_size=100,
    )
    await knowledge_uploads.update_status(db, uid, status="processing")
    return uid


async def test_run_ingest_success(db, upload_id):
    from genesis.knowledge.ingest_upload import run_ingest

    mock_orchestrator = MagicMock()
    mock_orchestrator.ingest_source = AsyncMock(return_value=FakeIngestResult())

    mock_rt = MagicMock()
    mock_rt.db = db

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=mock_rt),
        patch("genesis.mcp.memory.knowledge._get_orchestrator", return_value=mock_orchestrator),
        patch("genesis.knowledge.ingest_upload._move_to_completed"),
    ):
        await run_ingest(upload_id, project_type="pro", domain="test")

    row = await knowledge_uploads.get(db, upload_id)
    assert row["status"] == "completed"
    assert "u1" in row["unit_ids"]


async def test_run_ingest_failure(db, upload_id):
    from genesis.knowledge.ingest_upload import run_ingest

    mock_orchestrator = MagicMock()
    mock_orchestrator.ingest_source = AsyncMock(return_value=FakeIngestResult(error="Bad file"))

    mock_rt = MagicMock()
    mock_rt.db = db

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=mock_rt),
        patch("genesis.mcp.memory.knowledge._get_orchestrator", return_value=mock_orchestrator),
    ):
        await run_ingest(upload_id, project_type="pro", domain="test")

    row = await knowledge_uploads.get(db, upload_id)
    assert row["status"] == "failed"
    assert row["error_message"] == "Bad file"


async def test_run_ingest_exception(db, upload_id):
    from genesis.knowledge.ingest_upload import run_ingest

    mock_orchestrator = MagicMock()
    mock_orchestrator.ingest_source = AsyncMock(side_effect=RuntimeError("Boom"))

    mock_rt = MagicMock()
    mock_rt.db = db

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=mock_rt),
        patch("genesis.mcp.memory.knowledge._get_orchestrator", return_value=mock_orchestrator),
    ):
        await run_ingest(upload_id, project_type="pro", domain="test")

    row = await knowledge_uploads.get(db, upload_id)
    assert row["status"] == "failed"
    assert "Internal error" in row["error_message"]


async def test_run_ingest_missing_upload(db):
    from genesis.knowledge.ingest_upload import run_ingest

    mock_rt = MagicMock()
    mock_rt.db = db

    with patch("genesis.runtime.GenesisRuntime.instance", return_value=mock_rt):
        # Should not raise — just logs and returns
        await run_ingest("nonexistent", project_type="pro")
