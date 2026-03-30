"""Tests for BookmarkEnrichmentExecutor."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.bookmark.enrichment import BookmarkEnrichmentExecutor, _read_transcript
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


@pytest.fixture
def mock_bookmark_mgr():
    mgr = AsyncMock()
    mgr.enrich = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def mock_router():
    router = AsyncMock()
    response = MagicMock()
    response.text = "Key decisions: Use REST API. Next steps: Implement auth."
    router.route = AsyncMock(return_value=response)
    return router


@pytest.fixture
def executor(mock_bookmark_mgr, mock_router):
    db = AsyncMock()
    return BookmarkEnrichmentExecutor(
        bookmark_manager=mock_bookmark_mgr,
        db=db,
        router=mock_router,
    )


def _make_task(payload: str | None = None) -> SurplusTask:
    return SurplusTask(
        id="task-1",
        task_type=TaskType.BOOKMARK_ENRICHMENT,
        compute_tier=ComputeTier.FREE_API,
        priority=0.4,
        drive_alignment="curiosity",
        status=TaskStatus.RUNNING,
        created_at="2026-03-22T10:00:00",
        payload=payload,
    )


def test_read_transcript_nonexistent():
    assert _read_transcript("/nonexistent/path.jsonl") == ""


def test_read_transcript_with_content():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "human", "message": {"content": "Hello"}}) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hi there!"}]},
        }) + "\n")
        f.flush()
        result = _read_transcript(f.name)
    assert "Hello" in result
    assert "Hi there!" in result
    Path(f.name).unlink()


@pytest.mark.asyncio
async def test_execute_no_payload(executor):
    result = await executor.execute(_make_task(payload=None))
    assert not result.success
    assert "No payload" in result.error


@pytest.mark.asyncio
async def test_execute_no_bookmark_id(executor):
    result = await executor.execute(_make_task(payload="{}"))
    assert not result.success


@pytest.mark.asyncio
async def test_execute_bookmark_not_found(executor):
    # Mock DB returns None
    from genesis.db.crud import session_bookmarks as crud

    executor._db = AsyncMock()

    # Patch get_by_id to return None
    original = crud.get_by_id

    async def mock_get(db, bid):
        return None

    crud.get_by_id = mock_get
    try:
        payload = json.dumps({"bookmark_id": "bm-1", "transcript_path": "/tmp/t.jsonl"})
        result = await executor.execute(_make_task(payload=payload))
        assert not result.success
        assert "not found" in result.error
    finally:
        crud.get_by_id = original


@pytest.mark.asyncio
async def test_execute_success(executor, mock_bookmark_mgr, mock_router):
    """Full success path with transcript."""
    import aiosqlite

    from genesis.db.crud import session_bookmarks as crud
    from genesis.db.schema import create_all_tables, seed_data

    # Set up real in-memory DB
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()

    # Create a bookmark
    import uuid
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-1",
        bookmark_type="micro", topic="Test",
        created_at="2026-03-22T10:00:00",
    )

    executor._db = db

    # Create a transcript file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "human", "message": {"content": "Build bookmarks"}}) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Let's design it."}]},
        }) + "\n")
        f.flush()
        transcript_path = f.name

    try:
        payload = json.dumps({"bookmark_id": bid, "transcript_path": transcript_path})
        result = await executor.execute(_make_task(payload=payload))
        assert result.success
        assert result.content
        mock_bookmark_mgr.enrich.assert_called_once()
        mock_router.route.assert_called_once()
    finally:
        Path(transcript_path).unlink()
        await db.close()
