"""Tests for InboxMonitor — dispatch pipeline and lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.cc.types import CCOutput
from genesis.db.schema import create_all_tables
from genesis.inbox.monitor import InboxMonitor, _extract_urls, _is_acknowledged
from genesis.inbox.types import InboxConfig
from genesis.inbox.writer import ResponseWriter


@dataclass
class _FakeClock:
    """Controllable clock for tests."""
    now: datetime = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def _success_output(text: str = "evaluation result") -> CCOutput:
    return CCOutput(
        session_id="cc-sess-1",
        text=text,
        model_used="sonnet",
        cost_usd=0.05,
        input_tokens=100,
        output_tokens=200,
        duration_ms=5000,
        exit_code=0,
    )


def _error_output(msg: str = "something broke") -> CCOutput:
    return CCOutput(
        session_id="",
        text="",
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        duration_ms=1000,
        exit_code=1,
        is_error=True,
        error_message=msg,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def inbox_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def config(inbox_dir: Path) -> InboxConfig:
    return InboxConfig(watch_path=inbox_dir, batch_size=5)


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
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def monitor(db, mock_invoker, mock_session_manager, config, writer, clock, tmp_path):
    return InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
        prompt_dir=tmp_path,  # no INBOX_EVALUATE.md → uses fallback
    )


@pytest.mark.asyncio
async def test_empty_folder(monitor):
    result = await monitor.check_once()
    assert result.items_new == 0
    assert result.batches_dispatched == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_new_files_dispatched(monitor, inbox_dir, mock_invoker):
    (inbox_dir / "links.md").write_text("https://example.com")
    result = await monitor.check_once()
    assert result.items_new == 1
    assert result.batches_dispatched == 1
    mock_invoker.run.assert_called_once()


@pytest.mark.asyncio
async def test_modified_file_reprocessed(monitor, inbox_dir, mock_invoker, clock):
    f = inbox_dir / "doc.md"
    f.write_text("version 1")
    await monitor.check_once()
    mock_invoker.run.reset_mock()
    # Advance clock past the evaluation cooldown (default 3600s)
    clock.now = clock.now + timedelta(hours=2)
    f.write_text("version 2")
    result = await monitor.check_once()
    assert result.items_modified == 1
    assert result.batches_dispatched == 1
    mock_invoker.run.assert_called_once()


@pytest.mark.asyncio
async def test_already_processed_skipped(monitor, inbox_dir, mock_invoker):
    (inbox_dir / "doc.md").write_text("stable content")
    await monitor.check_once()
    mock_invoker.run.reset_mock()
    # Second check — no changes
    result = await monitor.check_once()
    assert result.items_new == 0
    assert result.items_modified == 0
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_batching_multiple_batches(monitor, inbox_dir, mock_invoker, config):
    # Create 12 files → should create 3 batches (batch_size=5)
    for i in range(12):
        (inbox_dir / f"item-{i:02d}.md").write_text(f"content {i}")
    result = await monitor.check_once()
    assert result.items_new == 12
    assert result.batches_dispatched == 3
    assert mock_invoker.run.call_count == 3


@pytest.mark.asyncio
async def test_cc_error_marks_items_failed(monitor, inbox_dir, mock_invoker, db):
    mock_invoker.run.return_value = _error_output("timeout")
    (inbox_dir / "fail.md").write_text("will fail")
    result = await monitor.check_once()
    assert len(result.errors) == 1
    assert "timeout" in result.errors[0]
    # Item should be marked failed in DB
    from genesis.db.crud import inbox_items
    pending = await inbox_items.query_pending(db)
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_cc_exception_marks_items_failed(monitor, inbox_dir, mock_invoker):
    mock_invoker.run.side_effect = RuntimeError("connection refused")
    (inbox_dir / "err.md").write_text("error")
    result = await monitor.check_once()
    assert len(result.errors) == 1
    assert "connection refused" in result.errors[0]


@pytest.mark.asyncio
async def test_response_file_written(monitor, inbox_dir, writer):
    (inbox_dir / "research.md").write_text("https://example.com/paper")
    await monitor.check_once()
    responses = list(inbox_dir.glob("*.genesis.md"))
    assert len(responses) == 1
    content = responses[0].read_text()
    assert "evaluation result" in content


@pytest.mark.asyncio
async def test_message_queue_entry_created(monitor, inbox_dir, db):
    (inbox_dir / "item.md").write_text("some content")
    await monitor.check_once()
    from genesis.db.crud import message_queue
    msgs = await message_queue.query_pending(db, target="cc_foreground")
    assert len(msgs) == 1
    assert "item.md" in msgs[0]["content"]


@pytest.mark.asyncio
async def test_system_prompt_loaded(monitor, tmp_path):
    prompt_file = tmp_path / "INBOX_EVALUATE.md"
    prompt_file.write_text("Custom system prompt here")
    monitor._system_prompt = None  # reset cache
    prompt = monitor._load_system_prompt()
    assert prompt == "Custom system prompt here"


@pytest.mark.asyncio
async def test_system_prompt_fallback(monitor):
    prompt = monitor._load_system_prompt()
    assert "inbox evaluation" in prompt.lower()  # fallback mentions inbox evaluation


@pytest.mark.asyncio
async def test_missing_watch_path(db, mock_invoker, mock_session_manager, tmp_path):
    config = InboxConfig(watch_path=tmp_path / "nonexistent")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config,
    )
    result = await mon.check_once()
    assert len(result.errors) == 1
    assert "does not exist" in result.errors[0]


@pytest.mark.asyncio
async def test_start_stop_lifecycle(monitor):
    await monitor.start()
    assert monitor._scheduler.running
    jobs = monitor._scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "inbox_monitor_check"
    # stop() should not raise
    await monitor.stop()


@pytest.mark.asyncio
async def test_event_bus_emits(monitor, inbox_dir):
    event_bus = AsyncMock()
    event_bus.emit = AsyncMock()
    monitor._event_bus = event_bus
    (inbox_dir / "ev.md").write_text("trigger event")
    await monitor.check_once()
    event_bus.emit.assert_called()


@pytest.mark.asyncio
async def test_session_creation_failure(monitor, inbox_dir, mock_session_manager):
    mock_session_manager.create_background.side_effect = RuntimeError("no sessions")
    (inbox_dir / "x.md").write_text("content")
    result = await monitor.check_once()
    assert len(result.errors) == 1
    assert result.batches_dispatched == 0


@pytest.mark.asyncio
async def test_cooldown_skips_recently_evaluated(
    db, mock_invoker, mock_session_manager, inbox_dir, tmp_path,
):
    """Modified file within cooldown period should not be re-evaluated."""
    clock = _FakeClock()
    config = InboxConfig(
        watch_path=inbox_dir, batch_size=5, evaluation_cooldown_seconds=3600,
    )
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer, clock=clock, prompt_dir=tmp_path,
    )
    f = inbox_dir / "doc.md"
    f.write_text("version 1")
    result1 = await mon.check_once()
    assert result1.batches_dispatched == 1

    mock_invoker.run.reset_mock()

    # Modify file but stay within cooldown (advance only 10 min)
    clock.now = clock.now + timedelta(minutes=10)
    f.write_text("version 2")
    result2 = await mon.check_once()
    assert result2.items_modified == 1
    assert result2.batches_dispatched == 0
    mock_invoker.run.assert_not_called()

    # Advance past cooldown
    clock.now = clock.now + timedelta(hours=2)
    f.write_text("version 3")
    result3 = await mon.check_once()
    assert result3.items_modified == 1
    assert result3.batches_dispatched == 1


# --- URL extraction tests ---


def test_extract_urls_standard():
    text = "Check out https://example.com/path?q=1 and http://foo.bar/baz"
    urls = _extract_urls(text)
    assert urls == ["https://example.com/path?q=1", "http://foo.bar/baz"]


def test_extract_urls_bare_domain():
    text = "See search.app/STsCw for details"
    urls = _extract_urls(text)
    assert urls == ["search.app/STsCw"]


def test_extract_urls_deduplication():
    text = "Visit https://example.com twice: https://example.com"
    urls = _extract_urls(text)
    assert urls == ["https://example.com"]


def test_extract_urls_strips_trailing_punctuation():
    text = "Link: https://foo.com/bar). Also (https://baz.com/qux)."
    urls = _extract_urls(text)
    assert "https://foo.com/bar" in urls
    assert "https://baz.com/qux" in urls


def test_extract_urls_empty_content():
    assert _extract_urls("no urls here, just text") == []
    assert _extract_urls("") == []


def test_extract_urls_google_search():
    """Google search URLs with embedded query strings are captured."""
    text = (
        "https://www.google.com/search?client=firefox-b-1-m"
        "&q=Selling%20AI%20Agents"
    )
    urls = _extract_urls(text)
    assert len(urls) == 1
    assert "google.com/search" in urls[0]


@pytest.mark.asyncio
async def test_build_prompt_enumerates_urls(monitor, inbox_dir):
    """Prompt includes numbered URL list when content contains URLs."""
    from genesis.inbox.types import InboxItem

    item = InboxItem(
        id="url-test",
        file_path=str(inbox_dir / "links.md"),
        content="Check https://example.com and search.app/abc",
        content_hash="abc",
        detected_at="2026-03-16",
    )
    prompt = monitor._build_prompt([item])
    assert "### URLs found" in prompt
    assert "1. https://example.com" in prompt
    assert "2. search.app/abc" in prompt
    assert "### Content:" in prompt


@pytest.mark.asyncio
async def test_build_prompt_no_urls_section_when_none(monitor, inbox_dir):
    """Prompt omits URL section when content has no URLs."""
    from genesis.inbox.types import InboxItem

    item = InboxItem(
        id="no-url",
        file_path=str(inbox_dir / "notes.md"),
        content="Just some plain text thoughts",
        content_hash="def",
        detected_at="2026-03-16",
    )
    prompt = monitor._build_prompt([item])
    assert "### URLs found" not in prompt
    assert "### Content:" in prompt


# --- Acknowledged classification tests ---


def test_is_acknowledged_positive():
    """Detects standard Acknowledged classification."""
    text = "**Classification:** Acknowledged\nNoted: this is context."
    assert _is_acknowledged(text) is True


def test_is_acknowledged_case_insensitive():
    """Detects case variations."""
    assert _is_acknowledged("**Classification:** acknowledged") is True
    assert _is_acknowledged("**Classification:**  ACKNOWLEDGED") is True


def test_is_acknowledged_negative():
    """Does not false-positive on other classifications."""
    assert _is_acknowledged("**Classification:** Genesis-relevant") is False
    assert _is_acknowledged("**Classification:** Personal note") is False


def test_is_acknowledged_body_mention():
    """Does not false-positive on 'acknowledged' in body text."""
    text = (
        "**Classification:** Genesis-relevant\n"
        "The query titles suggest (acknowledged, not evaluated)."
    )
    assert _is_acknowledged(text) is False


@pytest.mark.asyncio
async def test_acknowledged_no_file_written(
    db, mock_invoker, mock_session_manager, inbox_dir, tmp_path,
):
    """When CC classifies as Acknowledged, no response file is written."""
    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer, clock=clock, prompt_dir=tmp_path,
    )
    mock_invoker.run.return_value = _success_output(
        "**Classification:** Acknowledged\n"
        "Noted: this file is user-specific context."
    )
    (inbox_dir / "meta.md").write_text(
        "[This note is USER specific, generally not for researching]"
    )
    result = await mon.check_once()
    assert result.batches_dispatched == 1
    assert result.items_new == 1
    # No response file should exist
    responses = list(inbox_dir.glob("*.genesis.md"))
    assert len(responses) == 0


@pytest.mark.asyncio
async def test_acknowledged_stores_evaluated_content(
    db, mock_invoker, mock_session_manager, inbox_dir, tmp_path,
):
    """Acknowledged items store evaluated_content for delta computation."""
    from genesis.db.crud import inbox_items

    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer, clock=clock, prompt_dir=tmp_path,
    )
    mock_invoker.run.return_value = _success_output(
        "**Classification:** Acknowledged\nNoted: context absorbed."
    )
    source_text = "[Context for Genesis — not for evaluation]"
    (inbox_dir / "ctx.md").write_text(source_text)
    await mon.check_once()

    # Verify DB has evaluated_content stored
    row = await inbox_items.get_by_file_path(db, str(inbox_dir / "ctx.md"))
    assert row is not None
    assert row["status"] == "completed"
    assert row["evaluated_content"] is not None
    assert source_text in row["evaluated_content"]


@pytest.mark.asyncio
async def test_ambiguous_note_gets_response(
    db, mock_invoker, mock_session_manager, inbox_dir, tmp_path,
):
    """Non-Acknowledged items still get a response file written."""
    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer, clock=clock, prompt_dir=tmp_path,
    )
    mock_invoker.run.return_value = _success_output(
        "**Classification:** Question\n"
        "I noticed this note. Did you want me to research something here?"
    )
    (inbox_dir / "ambiguous.md").write_text("Something unclear")
    await mon.check_once()

    # Response file should be written
    responses = list(inbox_dir.glob("*.genesis.md"))
    assert len(responses) == 1


# --- No hard eval limit — cooldown + retry storm prevention are sufficient ---


@pytest.mark.asyncio
async def test_no_hard_eval_limit(
    db, mock_invoker, mock_session_manager, inbox_dir, tmp_path,
):
    """Files with many prior evaluations are still processed when content changes."""
    import uuid

    from genesis.db.crud import inbox_items

    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=config, writer=writer, clock=clock, prompt_dir=tmp_path,
    )
    f = inbox_dir / "notepad.md"

    # Seed 20 prior evaluations (well above any former hard cap)
    for i in range(20):
        await inbox_items.create(
            db,
            id=str(uuid.uuid4()),
            file_path=str(f),
            content_hash=f"old-hash-{i}",
            status="completed",
            created_at=f"2026-03-0{min(i + 1, 9)}T00:00:00+00:00",
        )

    f.write_text("brand new content that was never evaluated")
    result = await mon.check_once()
    # File has prior DB entries so it's detected as modified, not new
    assert result.items_modified == 1
    assert result.batches_dispatched == 1
    mock_invoker.run.assert_called_once()
