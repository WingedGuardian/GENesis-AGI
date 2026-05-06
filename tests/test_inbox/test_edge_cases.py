"""Edge case tests for inbox system — invoker stdin, scanner resilience, retry logic.

These tests cover scenarios that caused production failures:
- File deletion between scan and read
- Stuck "processing" entries blocking forever
- Deleted response files preventing re-evaluation
- Dropbox/sync temp file exclusion
- Invoker stdin edge cases (empty, long, unicode prompts)
- Retry cap (items permanently fail after max_retries)
- Concurrency guard on check_once
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCOutput
from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.scanner import (
    compute_hash,
    detect_changes,
    read_content,
    scan_folder,
)
from genesis.inbox.types import InboxConfig
from genesis.inbox.writer import ResponseWriter

# ── Scanner edge cases ─────────────────────────────────────────────────


@pytest.fixture
def inbox_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def test_scan_excludes_tilde_prefixed(inbox_dir: Path):
    """Dropbox/editors create ~file temp files."""
    (inbox_dir / "~Untitled.md").write_text("temp")
    (inbox_dir / "real.md").write_text("content")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "real.md"


def test_scan_excludes_sync_conflict(inbox_dir: Path):
    """Dropbox sync conflict files should be ignored."""
    (inbox_dir / "notes.sync-conflict-20260311-123456.md").write_text("conflict")
    (inbox_dir / "notes.md").write_text("real")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "notes.md"


def test_scan_excludes_tmp_files(inbox_dir: Path):
    """Temp files from atomic writes or sync should be ignored."""
    (inbox_dir / "response.tmp").write_text("partial")
    (inbox_dir / "real.md").write_text("content")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "real.md"


def test_detect_changes_file_vanishes(inbox_dir: Path):
    """File deleted between scan_folder and compute_hash should not crash."""
    f = inbox_dir / "ephemeral.md"
    f.write_text("here now")

    # Patch scan_folder to return the file, then delete it before hash
    original_compute = compute_hash

    call_count = 0

    def _vanishing_hash(fp: Path) -> str:
        nonlocal call_count
        call_count += 1
        if fp.name == "ephemeral.md":
            fp.unlink()  # File vanishes mid-operation
            raise FileNotFoundError(f"No such file: {fp}")
        return original_compute(fp)

    with patch("genesis.inbox.scanner.compute_hash", side_effect=_vanishing_hash):
        new, modified = detect_changes(inbox_dir, {})

    # Should not crash, should return empty
    assert new == []
    assert modified == []


def test_detect_changes_permission_error(inbox_dir: Path):
    """Unreadable file should be skipped, not crash."""
    f = inbox_dir / "locked.md"
    f.write_text("content")

    def _perm_error(fp: Path) -> str:
        raise PermissionError(f"Permission denied: {fp}")

    with patch("genesis.inbox.scanner.compute_hash", side_effect=_perm_error):
        new, modified = detect_changes(inbox_dir, {})

    assert new == []
    assert modified == []


def test_read_content_empty_file(inbox_dir: Path):
    """Empty file should return empty string, not crash."""
    f = inbox_dir / "empty.md"
    f.write_text("")
    assert read_content(f) == ""


def test_compute_hash_empty_file(inbox_dir: Path):
    """Empty file should have a valid, consistent hash."""
    f = inbox_dir / "empty.md"
    f.write_text("")
    h = compute_hash(f)
    assert len(h) == 64
    # SHA-256 of empty bytes is deterministic
    assert h == compute_hash(f)


# ── CRUD: get_all_known edge cases ─────────────────────────────────────


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_get_all_known_excludes_failed(db):
    """Failed items should not block reprocessing."""
    await inbox_items.create(
        db, id="fail1", file_path="/inbox/test.md",
        content_hash="abc123", status="failed",
        created_at="2026-03-11T01:00:00+00:00",
    )
    known = await inbox_items.get_all_known(db)
    assert "/inbox/test.md" not in known


@pytest.mark.asyncio
async def test_get_all_known_includes_completed(db, tmp_path):
    """Completed items with existing response file should block reprocessing."""
    response = tmp_path / "test.genesis.md"
    response.write_text("evaluation")
    await inbox_items.create(
        db, id="ok1", file_path="/inbox/test.md",
        content_hash="abc123", status="completed",
        created_at="2026-03-11T01:00:00+00:00",
    )
    await inbox_items.set_response_path(
        db, "ok1", response_path=str(response),
        processed_at="2026-03-11T01:05:00+00:00",
    )
    known = await inbox_items.get_all_known(db)
    assert "/inbox/test.md" in known
    assert known["/inbox/test.md"] == "abc123"


@pytest.mark.asyncio
async def test_get_all_known_deleted_response_allows_reprocessing(db, tmp_path):
    """If user deletes the response file, item becomes reprocessable."""
    response = tmp_path / "test.genesis.md"
    response.write_text("evaluation")
    await inbox_items.create(
        db, id="ok2", file_path="/inbox/test.md",
        content_hash="abc123", status="completed",
        created_at="2026-03-11T01:00:00+00:00",
    )
    await inbox_items.set_response_path(
        db, "ok2", response_path=str(response),
        processed_at="2026-03-11T01:05:00+00:00",
    )
    # Simulate user deleting the response file
    response.unlink()
    known = await inbox_items.get_all_known(db)
    assert "/inbox/test.md" not in known


@pytest.mark.asyncio
async def test_get_all_known_completed_without_response_path_stays_known(db):
    """Completed items with no response_path (older data) stay known."""
    await inbox_items.create(
        db, id="old1", file_path="/inbox/test.md",
        content_hash="abc123", status="completed",
        created_at="2026-03-11T01:00:00+00:00",
    )
    # No set_response_path call — response_path is NULL
    known = await inbox_items.get_all_known(db)
    assert "/inbox/test.md" in known


@pytest.mark.asyncio
async def test_expire_stuck_processing(db):
    """Items stuck in 'processing' for >2h should be expired to 'failed'."""
    old_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    await inbox_items.create(
        db, id="stuck1", file_path="/inbox/stuck.md",
        content_hash="def456", status="processing",
        created_at=old_time,
    )
    expired = await inbox_items.expire_stuck_processing(db)
    assert expired == 1
    # Should not be in known set (retry_count=0 < max_retries=3, so retriable)
    known = await inbox_items.get_all_known(db)
    assert "/inbox/stuck.md" not in known
    # Verify it was marked as failed
    item = await inbox_items.get_by_id(db, "stuck1")
    assert item["status"] == "failed"
    assert item["error_message"] == "processing_timeout_expired"


@pytest.mark.asyncio
async def test_get_all_known_recent_processing_not_expired(db):
    """Recently created 'processing' items should NOT be expired."""
    recent_time = datetime.now(UTC).isoformat()
    await inbox_items.create(
        db, id="active1", file_path="/inbox/active.md",
        content_hash="ghi789", status="processing",
        created_at=recent_time,
    )
    known = await inbox_items.get_all_known(db)
    # Should still be known (blocking, as intended)
    assert "/inbox/active.md" in known


# ── Monitor edge cases ─────────────────────────────────────────────────


def _success_output(text: str = "evaluation result") -> CCOutput:
    return CCOutput(
        session_id="cc-sess-1", text=text, model_used="sonnet",
        cost_usd=0.05, input_tokens=100, output_tokens=200,
        duration_ms=5000, exit_code=0,
    )


@pytest.fixture
def config(inbox_dir: Path) -> InboxConfig:
    return InboxConfig(watch_path=inbox_dir, batch_size=5, evaluation_cooldown_seconds=0)


@pytest.fixture
def mock_invoker():
    inv = AsyncMock()
    inv.run = AsyncMock(return_value=_success_output())
    return inv


@pytest.fixture
def mock_session_manager():
    sm = AsyncMock()
    sm.create_background = AsyncMock(return_value={"id": "sess-bg-1"})
    sm.complete = AsyncMock()
    sm.fail = AsyncMock()
    return sm


@pytest.fixture
def writer(inbox_dir: Path) -> ResponseWriter:
    return ResponseWriter(watch_path=inbox_dir, timezone="UTC")


@pytest.fixture
def monitor(db, mock_invoker, mock_session_manager, config, writer, tmp_path):
    return InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer,
        clock=lambda: datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC),
        prompt_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_file_vanishes_before_read(monitor, inbox_dir, mock_invoker):
    """File deleted after scan but before read should not crash the monitor."""
    f = inbox_dir / "vanishing.md"
    f.write_text("here now, gone soon")

    # Run check — the file exists during scan_folder and detect_changes
    # but simulate it vanishing during read_content
    original_read = read_content

    def _vanishing_read(fp: Path, max_bytes: int = 50_000) -> str:
        if fp.name == "vanishing.md":
            fp.unlink()
            raise FileNotFoundError(f"File gone: {fp}")
        return original_read(fp, max_bytes)

    with patch("genesis.inbox.monitor.read_content", side_effect=_vanishing_read):
        result = await monitor.check_once()

    # Should not crash, CC should not be called for the vanished file
    assert result.errors == []
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_empty_file_skipped(monitor, inbox_dir, mock_invoker):
    """Empty file should be recorded but not dispatched to CC."""
    (inbox_dir / "empty.md").write_text("")
    result = await monitor.check_once()
    assert result.items_new == 1
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_only_file_skipped(monitor, inbox_dir, mock_invoker):
    """File with only whitespace should be treated as empty."""
    (inbox_dir / "blank.md").write_text("   \n\n  \t  \n")
    result = await monitor.check_once()
    assert result.items_new == 1
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_cleared_file_not_reprocessed(monitor, inbox_dir, mock_invoker, db):
    """If user clears file content, the modified file should not dispatch to CC."""
    (inbox_dir / "note.md").write_text("some real content")
    result1 = await monitor.check_once()
    assert result1.batches_dispatched == 1

    mock_invoker.run.reset_mock()

    # User clears the file
    (inbox_dir / "note.md").write_text("")
    result2 = await monitor.check_once()
    assert result2.items_modified == 1
    assert result2.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_response_triggers_reprocessing(
    monitor, inbox_dir, mock_invoker, db,
):
    """If user deletes response file, same content gets reprocessed on next tick."""
    (inbox_dir / "links.md").write_text("https://example.com")
    result1 = await monitor.check_once()
    assert result1.batches_dispatched == 1

    # Response file should exist
    responses = list(inbox_dir.glob("*.genesis.md"))
    assert len(responses) >= 1

    # User deletes the response file
    for r in responses:
        r.unlink()

    # Reset mock to track new calls
    mock_invoker.run.reset_mock()

    # Next tick should reprocess (same content, but response missing)
    result2 = await monitor.check_once()
    # The file should now be detected as "new" since get_all_known
    # excludes completed items whose response was deleted
    assert result2.batches_dispatched == 1
    mock_invoker.run.assert_called_once()


@pytest.mark.asyncio
async def test_failed_item_retried_on_content_change(
    monitor, inbox_dir, mock_invoker,
):
    """After a failure, modifying the source file should trigger retry."""
    mock_invoker.run.return_value = CCOutput(
        session_id="", text="", model_used="sonnet", cost_usd=0.0,
        input_tokens=0, output_tokens=0, duration_ms=1000, exit_code=1,
        is_error=True, error_message="CC error: timeout",
    )
    f = inbox_dir / "retry.md"
    f.write_text("version 1 — will fail")
    result1 = await monitor.check_once()
    assert len(result1.errors) == 1

    # Fix the invoker, change the content
    mock_invoker.run.return_value = _success_output("success!")
    f.write_text("version 2 — should succeed")
    result2 = await monitor.check_once()
    assert result2.batches_dispatched == 1
    assert result2.errors == []


@pytest.mark.asyncio
async def test_failed_item_retried_same_content(
    monitor, inbox_dir, mock_invoker, db,
):
    """After a failure, the same content should be retried on next tick.
    The existing failed row is reused (not duplicated) to maintain proper
    retry_count tracking."""
    mock_invoker.run.return_value = CCOutput(
        session_id="", text="", model_used="sonnet", cost_usd=0.0,
        input_tokens=0, output_tokens=0, duration_ms=1000, exit_code=1,
        is_error=True, error_message="CC error: temporary failure",
    )
    (inbox_dir / "flaky.md").write_text("same content both times")
    result1 = await monitor.check_once()
    assert len(result1.errors) == 1

    # Fix the invoker — same content should be retried
    mock_invoker.run.return_value = _success_output("now it works")
    result2 = await monitor.check_once()
    assert result2.items_new == 1  # Detected as new since failed is excluded
    assert result2.batches_dispatched == 1

    # Verify dedup: only one DB row for this file (reused, not duplicated)
    rows = [dict(r) for r in (await (await db.execute(
        "SELECT id, status, retry_count FROM inbox_items "
        "WHERE file_path LIKE '%flaky.md'",
    )).fetchall())]
    assert len(rows) == 1, f"Expected 1 row (dedup), got {len(rows)}: {rows}"
    assert rows[0]["status"] == "completed"


# ── Retry cap tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_count_increments_on_failure(db):
    """Each update_status(status='failed') should increment retry_count."""
    await inbox_items.create(
        db, id="r1", file_path="/inbox/retry.md",
        content_hash="abc", created_at="2026-03-11T00:00:00+00:00",
    )
    await inbox_items.update_status(db, "r1", status="failed", error_message="err1")
    item = await inbox_items.get_by_id(db, "r1")
    assert item["retry_count"] == 1

    await inbox_items.update_status(db, "r1", status="failed", error_message="err2")
    item = await inbox_items.get_by_id(db, "r1")
    assert item["retry_count"] == 2


@pytest.mark.asyncio
async def test_permanently_failed_blocks_reprocessing(db):
    """Items with retry_count >= max_retries should block reprocessing."""
    await inbox_items.create(
        db, id="pf1", file_path="/inbox/perm.md",
        content_hash="abc", created_at="2026-03-11T00:00:00+00:00",
    )
    # Fail 3 times (max_retries=3)
    for _ in range(3):
        await inbox_items.update_status(db, "pf1", status="failed", error_message="err")

    known = await inbox_items.get_all_known(db, max_retries=3)
    # Should be in known set (permanently failed, blocks reprocessing)
    assert "/inbox/perm.md" in known


@pytest.mark.asyncio
async def test_retriable_failure_allows_reprocessing(db):
    """Items with retry_count < max_retries should allow reprocessing."""
    await inbox_items.create(
        db, id="rf1", file_path="/inbox/retriable.md",
        content_hash="abc", created_at="2026-03-11T00:00:00+00:00",
    )
    # Fail only once (max_retries=3, so still retriable)
    await inbox_items.update_status(db, "rf1", status="failed", error_message="err")

    known = await inbox_items.get_all_known(db, max_retries=3)
    assert "/inbox/retriable.md" not in known


# ── Retry dedup tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_reuses_existing_failed_item(monitor, inbox_dir, mock_invoker, db):
    """When a failed item is retried, the monitor should reuse the existing
    DB row rather than creating a duplicate with retry_count=0."""
    mock_invoker.run.return_value = CCOutput(
        session_id="", text="", model_used="sonnet", cost_usd=0.0,
        input_tokens=0, output_tokens=0, duration_ms=1000, exit_code=1,
        is_error=True, error_message="CC error: Timeout after 600s",
    )
    (inbox_dir / "timeout.md").write_text("content that times out")
    result1 = await monitor.check_once()
    assert len(result1.errors) == 1

    # Get the failed item's ID
    rows_after_fail = [dict(r) for r in (await (await db.execute(
        "SELECT id, status, retry_count FROM inbox_items "
        "WHERE file_path LIKE '%timeout.md'",
    )).fetchall())]
    assert len(rows_after_fail) == 1
    assert rows_after_fail[0]["status"] == "failed"
    assert rows_after_fail[0]["retry_count"] == 1
    original_id = rows_after_fail[0]["id"]

    # Fix the invoker — retry should reuse the existing row
    mock_invoker.run.return_value = _success_output("success on retry")
    result2 = await monitor.check_once()
    assert result2.items_new == 1  # Scanner still sees it as "new"
    assert result2.batches_dispatched == 1

    # Verify: still only ONE row for this file, same ID reused
    rows_after_retry = [dict(r) for r in (await (await db.execute(
        "SELECT id, status, retry_count FROM inbox_items "
        "WHERE file_path LIKE '%timeout.md'",
    )).fetchall())]
    assert len(rows_after_retry) == 1, (
        f"Expected 1 row (reused), got {len(rows_after_retry)}: {rows_after_retry}"
    )
    assert rows_after_retry[0]["id"] == original_id
    assert rows_after_retry[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_retry_dedup_skips_invalidated_items(db):
    """get_retriable_failed must NOT return approval-invalidated items."""
    await inbox_items.create(
        db, id="inv1", file_path="/inbox/changed.md",
        content_hash="old_hash", created_at="2026-03-11T00:00:00+00:00",
    )
    # Mark as failed with approval_invalidated prefix
    await inbox_items.update_status(
        db, "inv1", status="failed",
        error_message="approval_invalidated:content changed",
    )

    # Should NOT find the invalidated item
    result = await inbox_items.get_retriable_failed(
        db, "/inbox/changed.md", max_retries=3,
    )
    assert result is None


@pytest.mark.asyncio
async def test_retry_dedup_finds_transient_failures(db):
    """get_retriable_failed should find items that failed due to transient errors."""
    await inbox_items.create(
        db, id="trans1", file_path="/inbox/flaky.md",
        content_hash="abc", created_at="2026-03-11T00:00:00+00:00",
    )
    await inbox_items.update_status(
        db, "trans1", status="failed",
        error_message="CC invocation failed: Timeout after 600s",
    )

    result = await inbox_items.get_retriable_failed(
        db, "/inbox/flaky.md", max_retries=3,
    )
    assert result is not None
    assert result["id"] == "trans1"
    assert result["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_dedup_respects_max_retries(db):
    """get_retriable_failed should NOT find items that exhausted retries."""
    await inbox_items.create(
        db, id="exhausted1", file_path="/inbox/perm.md",
        content_hash="abc", created_at="2026-03-11T00:00:00+00:00",
    )
    # Fail 3 times (max_retries=3)
    for _ in range(3):
        await inbox_items.update_status(
            db, "exhausted1", status="failed", error_message="err",
        )

    result = await inbox_items.get_retriable_failed(
        db, "/inbox/perm.md", max_retries=3,
    )
    assert result is None


# ── Concurrency guard tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_once_rejects_concurrent_call(
    monitor, inbox_dir, mock_invoker,
):
    """If check_once is already running, a second call returns an error."""
    (inbox_dir / "test.md").write_text("content")

    # Acquire the lock manually to simulate an in-progress check
    await monitor._check_lock.acquire()
    try:
        result = await monitor.check_once()
        assert result.errors == ["Check already in progress"]
        assert result.batches_dispatched == 0
    finally:
        monitor._check_lock.release()


# ── Delta content computation ─────────────────────────────────────────


def test_compute_new_content_appended_lines():
    """Appending new lines to a file should return only the new lines."""
    from genesis.inbox.monitor import _compute_new_content

    old = "https://example.com/article1\n\nGemini embeddings 2\n"
    new = "https://example.com/article1\n\nGemini embeddings 2\n\naws agent core\naws strands\n"
    delta = _compute_new_content(old, new)
    assert "aws agent core" in delta
    assert "aws strands" in delta
    assert "example.com/article1" not in delta
    assert "Gemini embeddings 2" not in delta


def test_compute_new_content_identical():
    """Identical content should return empty string."""
    from genesis.inbox.monitor import _compute_new_content

    content = "line1\nline2\n"
    assert _compute_new_content(content, content).strip() == ""


def test_compute_new_content_all_new():
    """Completely new content (no overlap) returns everything."""
    from genesis.inbox.monitor import _compute_new_content

    old = "old stuff\n"
    new = "brand new\nanother new\n"
    delta = _compute_new_content(old, new)
    assert "brand new" in delta
    assert "another new" in delta


def test_compute_new_content_blank_lines_preserved():
    """Blank lines between new items should be preserved."""
    from genesis.inbox.monitor import _compute_new_content

    old = "existing\n"
    new = "existing\n\nnew item 1\n\nnew item 2\n"
    delta = _compute_new_content(old, new)
    assert "new item 1" in delta
    assert "new item 2" in delta


@pytest.mark.asyncio
async def test_modified_file_only_sends_delta(
    monitor, inbox_dir, mock_invoker, db,
):
    """When a file is modified, only new content should be sent to CC."""
    f = inbox_dir / "links.md"
    f.write_text("https://example.com/first\n")
    result1 = await monitor.check_once()
    assert result1.batches_dispatched == 1

    # Reset and add new content
    mock_invoker.run.reset_mock()
    mock_invoker.run.return_value = _success_output("eval of second")
    f.write_text("https://example.com/first\n\nhttps://example.com/second\n")
    result2 = await monitor.check_once()
    assert result2.batches_dispatched == 1

    # The prompt sent to CC should contain the new URL but not the old one
    call_args = mock_invoker.run.call_args
    prompt = call_args.args[0].prompt if call_args.args else call_args.kwargs.get("prompt", "")
    # InboxItem.content is the delta, which gets embedded in the prompt
    assert "example.com/second" in prompt
    assert "example.com/first" not in prompt


@pytest.mark.asyncio
async def test_modified_file_no_new_content_skipped(
    monitor, inbox_dir, mock_invoker, db,
):
    """If file is modified but content hasn't actually changed, skip dispatch."""
    f = inbox_dir / "note.md"
    f.write_text("some content\n")
    result1 = await monitor.check_once()
    assert result1.batches_dispatched == 1

    mock_invoker.run.reset_mock()
    # Touch the file (add trailing whitespace) — no real new content
    f.write_text("some content\n   \n")
    result2 = await monitor.check_once()
    assert result2.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


# ── Triage pipeline integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_pipeline_fired_after_evaluation(
    db, mock_invoker, mock_session_manager, config, writer, tmp_path, inbox_dir,
):
    """Triage pipeline should be called with CCOutput after successful evaluation."""
    mock_triage = AsyncMock()
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer,
        clock=lambda: datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC),
        prompt_dir=tmp_path,
        triage_pipeline=mock_triage,
    )
    (inbox_dir / "test.md").write_text("https://example.com")
    result = await mon.check_once()
    assert result.batches_dispatched == 1

    # Give the fire-and-forget task a chance to run
    await asyncio.sleep(0.05)

    mock_triage.assert_called_once()
    args = mock_triage.call_args
    # First arg is the CCOutput, second is user_text, third is "inbox"
    assert args[0][2] == "inbox"


@pytest.mark.asyncio
async def test_triage_failure_does_not_crash_monitor(
    db, mock_invoker, mock_session_manager, config, writer, tmp_path, inbox_dir,
):
    """Triage pipeline failure must not affect inbox processing."""
    mock_triage = AsyncMock(side_effect=RuntimeError("triage boom"))
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer,
        clock=lambda: datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC),
        prompt_dir=tmp_path,
        triage_pipeline=mock_triage,
    )
    (inbox_dir / "test.md").write_text("some content")
    result = await mon.check_once()
    # Monitor completes successfully even though triage exploded
    assert result.batches_dispatched == 1
    assert result.errors == []

    await asyncio.sleep(0.05)
    mock_triage.assert_called_once()


@pytest.mark.asyncio
async def test_no_triage_pipeline_still_works(
    monitor, inbox_dir, mock_invoker,
):
    """Monitor without triage pipeline should work as before."""
    (inbox_dir / "test.md").write_text("content")
    result = await monitor.check_once()
    assert result.batches_dispatched == 1
    assert result.errors == []


# ── Invoker stdin edge cases ──────────────────────────────────────────


@pytest.fixture
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def test_build_args_never_contains_prompt(invoker):
    """Prompt must NEVER appear in args — it goes via stdin only."""
    cases = [
        CCInvocation(prompt="simple"),
        CCInvocation(prompt="with spaces and\nnewlines"),
        CCInvocation(prompt="a" * 10000),  # long prompt
        CCInvocation(prompt=""),  # empty
        CCInvocation(prompt="hello 世界 🎉"),  # unicode
        CCInvocation(prompt='{"json": true}'),  # looks like JSON
        CCInvocation(prompt="--model opus"),  # looks like a flag
        CCInvocation(prompt="-- some text"),  # starts with --
        CCInvocation(
            prompt="eval this",
            allowed_tools=["WebFetch", "WebSearch", "Read"],
            disallowed_tools=["Bash"],
            skip_permissions=True,
        ),
    ]
    for inv in cases:
        args = invoker._build_args(inv)
        assert inv.prompt not in args, f"Prompt leaked into args for: {inv.prompt[:50]}"
        assert "--" not in args, f"Separator '--' found in args for: {inv.prompt[:50]}"


def test_build_args_allowed_tools_comma_separated(invoker):
    """allowedTools should be a single comma-separated string, not variadic."""
    inv = CCInvocation(
        prompt="test",
        allowed_tools=["WebFetch", "WebSearch", "Read"],
    )
    args = invoker._build_args(inv)
    idx = args.index("--allowedTools")
    # The next arg should be the SINGLE comma-separated string
    tools_arg = args[idx + 1]
    assert tools_arg == "WebFetch,WebSearch,Read"
    # And nothing else after it (no variadic leak)
    remaining = args[idx + 2:]
    assert "WebFetch" not in remaining
    assert "WebSearch" not in remaining
    assert "Read" not in remaining


def test_build_args_disallowed_tools_comma_separated(invoker):
    """disallowedTools should be a single comma-separated string."""
    inv = CCInvocation(
        prompt="test",
        disallowed_tools=["Bash", "Edit", "Write"],
    )
    args = invoker._build_args(inv)
    idx = args.index("--disallowedTools")
    assert args[idx + 1] == "Bash,Edit,Write"


def test_build_args_both_allowed_and_disallowed(invoker):
    """Both tool lists can coexist."""
    inv = CCInvocation(
        prompt="test",
        allowed_tools=["WebFetch"],
        disallowed_tools=["Bash"],
    )
    args = invoker._build_args(inv)
    assert "--allowedTools" in args
    assert "--disallowedTools" in args
    # Prompt not in args
    assert "test" not in args


@pytest.mark.asyncio
async def test_run_sends_prompt_via_stdin(invoker):
    """The prompt must be sent via stdin, not as a CLI argument."""
    import json

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "got it", "session_id": "s1",
        "total_cost_usd": 0.01, "duration_ms": 100,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "modelUsage": {"claude-sonnet-4-6": {}},
    })

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        inv = CCInvocation(prompt="hello world")
        output = await invoker.run(inv)

    # Verify stdin=PIPE was requested
    call_kwargs = mock_exec.call_args.kwargs
    assert call_kwargs.get("stdin") is not None

    # Verify prompt was sent via communicate(input=...)
    comm_kwargs = mock_proc.communicate.call_args
    assert comm_kwargs.kwargs.get("input") == b"hello world" or \
           comm_kwargs.args[0] == b"hello world" if comm_kwargs.args else \
           comm_kwargs.kwargs.get("input") == b"hello world"

    assert output.text == "got it"
    assert not output.is_error


@pytest.mark.asyncio
async def test_run_unicode_prompt(invoker):
    """Unicode prompts should be encoded as UTF-8 via stdin."""
    import json

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1",
        "total_cost_usd": 0.01, "duration_ms": 100,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "modelUsage": {},
    })

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    prompt = "分析这个 URL: https://example.com/研究"
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(CCInvocation(prompt=prompt))

    # Verify UTF-8 encoding
    sent_input = mock_proc.communicate.call_args.kwargs.get("input")
    assert sent_input == prompt.encode("utf-8")
    assert not output.is_error


@pytest.mark.asyncio
async def test_run_streaming_sends_prompt_via_stdin(invoker):
    """Streaming mode also sends prompt via stdin."""
    import json

    result_event = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "streamed", "session_id": "s1",
        "total_cost_usd": 0.02, "duration_ms": 200,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "modelUsage": {},
    })

    class _AsyncStdout:
        def __init__(self):
            self._lines = iter([result_event.encode() + b"\n"])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._lines)
            except StopIteration:
                raise StopAsyncIteration from None

    mock_proc = AsyncMock()
    mock_proc.stdout = _AsyncStdout()
    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_stdin.close = MagicMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stderr = AsyncMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(CCInvocation(prompt="stream me"))

    # Verify stdin was written and closed
    mock_stdin.write.assert_called_once_with(b"stream me")
    mock_stdin.drain.assert_called_once()
    mock_stdin.close.assert_called_once()
    assert output.text == "streamed"
