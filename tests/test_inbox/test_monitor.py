"""Tests for InboxMonitor — dispatch pipeline and lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
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
async def test_empty_output_text_marks_failed_no_response_file(
    monitor, inbox_dir, mock_invoker, db,
):
    """Regression: empty CCOutput.text must not produce a frontmatter-only
    file. The blank files Genesis-4.genesis.md and "My todos &
    musings.genesis.md" on 2026-04-06 / 2026-04-10 were produced exactly
    this way — the upstream router returned success with empty content and
    the monitor passed it straight to the writer."""
    mock_invoker.run.return_value = _success_output(text="")
    (inbox_dir / "blank.md").write_text("content to evaluate")
    result = await monitor.check_once()

    # No response file should exist
    assert list(inbox_dir.glob("*.genesis.md")) == []

    # Item should be marked failed with a clear error message
    assert len(result.errors) == 1
    assert "empty" in result.errors[0].lower()

    from genesis.db.crud import inbox_items
    pending = await inbox_items.query_pending(db)
    assert len(pending) == 0  # not pending — moved to failed


@pytest.mark.asyncio
async def test_whitespace_only_output_text_marks_failed(
    monitor, inbox_dir, mock_invoker,
):
    """Whitespace-only text is also empty for our purposes."""
    mock_invoker.run.return_value = _success_output(text="   \n\n\t  \n")
    (inbox_dir / "ws.md").write_text("something")
    result = await monitor.check_once()

    assert list(inbox_dir.glob("*.genesis.md")) == []
    assert len(result.errors) == 1
    assert "empty" in result.errors[0].lower()


@pytest.mark.asyncio
async def test_empty_output_emits_error_event(monitor, inbox_dir, mock_invoker):
    """Empty-output failures must fire an ERROR event so the dashboard
    and Guardian can see them."""
    from genesis.observability.types import Severity

    event_bus = AsyncMock()
    event_bus.emit = AsyncMock()
    monitor._event_bus = event_bus
    mock_invoker.run.return_value = _success_output(text="")

    (inbox_dir / "silent.md").write_text("trigger silent failure")
    await monitor.check_once()

    # Find the empty_output event among all emitted events
    empty_calls = [
        c for c in event_bus.emit.call_args_list
        if len(c.args) >= 3 and c.args[2] == "evaluation.empty_output"
    ]
    assert len(empty_calls) == 1
    # Severity should be ERROR
    assert empty_calls[0].args[1] == Severity.ERROR


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


# ---------------------------------------------------------------------------
# Autonomous-CLI approval-gate blocked / resume tests
# ---------------------------------------------------------------------------


def _make_dispatcher(decision: AutonomousDispatchDecision):
    """Build a mock autonomous dispatcher that returns a fixed decision."""
    d = SimpleNamespace()
    d.route = AsyncMock(return_value=decision)
    return d


@pytest.mark.asyncio
async def test_blocked_pending_keeps_row_as_processing_with_marker(
    monitor, inbox_dir, mock_invoker, db,
):
    """When the dispatcher returns blocked with a pending approval, the
    row must stay in 'processing' state with an awaiting_approval marker
    — NOT be marked failed (which would cause the scanner to re-detect
    and create duplicate rows on every scan)."""
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-abc-123",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(decision)

    (inbox_dir / "pending.md").write_text("content needing approval")
    result = await monitor.check_once()

    # CC invoker should NOT have been called — we were blocked before CLI
    mock_invoker.run.assert_not_called()
    assert result.batches_dispatched == 0  # dispatch was blocked

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%pending.md'",
    )).fetchall())]
    assert len(rows) == 1
    assert rows[0]["status"] == "processing"
    assert rows[0]["error_message"].startswith("awaiting_approval:")
    assert "req-abc-123" in rows[0]["error_message"]


@pytest.mark.asyncio
async def test_blocked_rejected_marks_row_failed(
    monitor, inbox_dir, mock_invoker, db,
):
    """When the approval was previously rejected, the row must be
    marked failed so it enters the normal permanent-failure flow."""
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="existing rejection found",
        approval_request_id="req-rej-456",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(decision)

    (inbox_dir / "rejected.md").write_text("rejected content")
    await monitor.check_once()

    mock_invoker.run.assert_not_called()
    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%rejected.md'",
    )).fetchall())]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    # Error message is the generic "CLI fallback blocked: ..." not the marker
    assert "rejection" in rows[0]["error_message"].lower()


@pytest.mark.asyncio
async def test_blocked_policy_disabled_marks_row_failed(
    monitor, inbox_dir, mock_invoker, db,
):
    """When CLI fallback is disabled by policy (no approval_request_id),
    mark failed as before."""
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="CLI fallback disabled",
        approval_request_id=None,
    )
    monitor._autonomous_dispatcher = _make_dispatcher(decision)

    (inbox_dir / "disabled.md").write_text("policy disabled")
    await monitor.check_once()

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%disabled.md'",
    )).fetchall())]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_resume_pass_redispatches_awaiting_row(
    monitor, inbox_dir, mock_invoker, db,
):
    """On the NEXT scan after a pending-approval block, the resume pass
    must pick up the awaiting row, re-dispatch it, and — if the approval
    is now approved — run CC and complete the item WITHOUT creating a
    duplicate DB row."""
    # First scan: blocked with pending approval
    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-resume-1",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(pending_decision)

    (inbox_dir / "resume.md").write_text("content awaiting approval")
    await monitor.check_once()

    rows1 = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%resume.md'",
    )).fetchall())]
    assert len(rows1) == 1
    assert rows1[0]["status"] == "processing"

    # Second scan: dispatcher now returns cli_approved — simulates user
    # having approved via Telegram.  Note: in the real dispatcher, an
    # approved mode is "cli_approved" and output stays None so monitor
    # falls through to CLI path.  We mock both the dispatcher and the
    # invoker.run output.
    approved_decision = AutonomousDispatchDecision(
        mode="cli_approved",
        reason="CLI fallback approved",
        approval_request_id="req-resume-1",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(approved_decision)
    mock_invoker.run.return_value = _success_output("resumed evaluation")

    result2 = await monitor.check_once()
    assert result2.batches_dispatched == 1
    mock_invoker.run.assert_called_once()

    # No duplicate rows were created — the same row id was reused
    rows2 = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%resume.md' "
        "ORDER BY created_at",
    )).fetchall())]
    assert len(rows2) == 1
    assert rows2[0]["id"] == rows1[0]["id"]
    assert rows2[0]["status"] == "completed"

    # The response file was written
    assert list(inbox_dir.glob("*resume*.genesis.md"))


@pytest.mark.asyncio
async def test_resume_pass_invalidates_row_when_file_changed(
    monitor, inbox_dir, mock_invoker, db,
):
    """If the user modifies the file while the approval is still pending,
    the original approval is no longer valid for the new content.  The
    resume pass must mark the awaiting row failed so the next scan can
    create a fresh row (and fresh approval request) for the new content."""
    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-change-1",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(pending_decision)

    f = inbox_dir / "changed.md"
    f.write_text("original content")
    await monitor.check_once()

    # Modify the file before the next scan
    f.write_text("modified content that is very different")

    # Second scan — the resume pass should invalidate the old row AND
    # the scanner should create a fresh row for the modified content.
    await monitor.check_once()

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT id, status, error_message, content_hash "
        "FROM inbox_items WHERE file_path LIKE '%changed.md' "
        "ORDER BY created_at",
    )).fetchall())]
    # Must have exactly two rows: the invalidated original and a fresh one
    assert len(rows) == 2, (
        f"expected original (invalidated) + fresh row, got {len(rows)}: "
        f"{rows}"
    )
    # First row: invalidated due to content change — status=failed with
    # the approval_invalidated: prefix
    assert rows[0]["status"] == "failed"
    assert "approval_invalidated:" in (rows[0]["error_message"] or "")
    assert "content changed" in (rows[0]["error_message"] or "")
    # Second row: new row for the modified content, distinct content_hash
    assert rows[1]["content_hash"] != rows[0]["content_hash"]
    # The fresh row hits the dispatcher too → landed in processing state
    # with a new awaiting_approval marker (mocked dispatcher still returns
    # blocked-pending)
    assert rows[1]["status"] == "processing"
    assert (rows[1]["error_message"] or "").startswith(
        "awaiting_approval:",
    )


@pytest.mark.asyncio
async def test_resume_pass_invalidates_row_when_file_vanishes(
    monitor, inbox_dir, mock_invoker, db,
):
    """If the file is deleted while approval is pending, invalidate the
    awaiting row."""
    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-vanish-1",
    )
    monitor._autonomous_dispatcher = _make_dispatcher(pending_decision)

    f = inbox_dir / "vanished.md"
    f.write_text("content")
    await monitor.check_once()

    # Delete the file
    f.unlink()
    await monitor.check_once()

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT status, error_message FROM inbox_items "
        "WHERE file_path LIKE '%vanished.md'",
    )).fetchall())]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "vanished" in (rows[0]["error_message"] or "")


@pytest.mark.asyncio
async def test_dispatch_request_omits_volatile_context(
    monitor, inbox_dir, mock_invoker,
):
    """The dispatch request must pass context=None (or at least NOT
    include batch_id/item_count) so the approval key is content-stable
    across scans.  This regression test guards against reintroducing the
    duplicate-Telegram-prompt bug."""
    captured: list[object] = []

    async def capture_route(request):
        captured.append(request)
        return AutonomousDispatchDecision(
            mode="blocked",
            reason="approval requested",
            approval_request_id="req-ctx-1",
        )

    dispatcher = SimpleNamespace()
    dispatcher.route = capture_route
    monitor._autonomous_dispatcher = dispatcher

    (inbox_dir / "ctx.md").write_text("content")
    await monitor.check_once()

    assert captured, "dispatcher.route was never called"
    request = captured[0]
    # api_call_site_id must be None (no free-SLM fallback)
    assert request.api_call_site_id is None
    # Context must not carry volatile fields that would destabilize
    # the approval key across scans
    if request.context is not None:
        assert "batch_id" not in request.context
        assert "item_count" not in request.context


@pytest.mark.asyncio
async def test_resume_produces_stable_dispatch_across_scans(
    monitor, inbox_dir, mock_invoker,
):
    """Two scans against the same unchanged pending-approval item must
    produce dispatch requests with identical prompts, messages, and
    contexts — the inputs to ``_approval_key``.  This is what guarantees
    ApprovalManager dedup finds the existing pending request on scan 2
    and skips the Telegram resend.  Regression guard for the whole
    fix-it-now motivation: no new Telegram per scan."""
    captured: list[object] = []

    async def capture_route(request):
        captured.append(request)
        return AutonomousDispatchDecision(
            mode="blocked",
            reason="approval requested",
            approval_request_id="req-stable-1",
        )

    dispatcher = SimpleNamespace()
    dispatcher.route = capture_route
    monitor._autonomous_dispatcher = dispatcher

    (inbox_dir / "stable.md").write_text("some content to evaluate")

    # First scan: row created, dispatched, parked
    await monitor.check_once()
    # Second scan: resume pass picks up the same row, re-dispatches
    await monitor.check_once()

    assert len(captured) == 2, (
        f"expected 2 dispatch calls, got {len(captured)}"
    )
    req1, req2 = captured[0], captured[1]

    # The fields that feed _approval_key must be byte-identical between
    # scans.  Any divergence would cause ApprovalManager._find_existing
    # to miss the match and fire a fresh Telegram.
    assert req1.subsystem == req2.subsystem
    assert req1.policy_id == req2.policy_id
    assert req1.action_label == req2.action_label
    assert req1.messages == req2.messages
    assert req1.cli_invocation.prompt == req2.cli_invocation.prompt
    assert req1.cli_invocation.system_prompt == req2.cli_invocation.system_prompt
    assert req1.cli_invocation.model == req2.cli_invocation.model
    assert req1.cli_invocation.effort == req2.cli_invocation.effort
    assert req1.context == req2.context  # both should be None or equal


@pytest.mark.asyncio
async def test_expire_stuck_processing_skips_awaiting_rows(db, inbox_dir):
    """expire_stuck_processing must NOT expire rows that are parked
    awaiting approval, even if they're older than 2h."""
    import uuid as _uuid

    from genesis.db.crud import inbox_items

    # Create two stale processing rows: one with the awaiting marker,
    # one without.
    stale_created_at = "2020-01-01T00:00:00+00:00"
    awaiting_id = str(_uuid.uuid4())
    stuck_id = str(_uuid.uuid4())
    await inbox_items.create(
        db, id=awaiting_id, file_path=str(inbox_dir / "await.md"),
        content_hash="h1", status="processing", created_at=stale_created_at,
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        ("awaiting_approval:req-xyz", awaiting_id),
    )
    await inbox_items.create(
        db, id=stuck_id, file_path=str(inbox_dir / "stuck.md"),
        content_hash="h2", status="processing", created_at=stale_created_at,
    )
    await db.commit()

    expired = await inbox_items.expire_stuck_processing(db)

    # Only the genuinely stuck row should be expired
    assert expired == 1
    awaiting_row = await inbox_items.get_by_id(db, awaiting_id)
    stuck_row = await inbox_items.get_by_id(db, stuck_id)
    assert awaiting_row["status"] == "processing"
    assert stuck_row["status"] == "failed"


@pytest.mark.asyncio
async def test_get_awaiting_approval_returns_only_marked_rows(db, inbox_dir):
    """get_awaiting_approval must return only processing rows with the
    awaiting_approval: marker."""
    import uuid as _uuid

    from genesis.db.crud import inbox_items

    # Row 1: awaiting approval (should appear)
    id1 = str(_uuid.uuid4())
    await inbox_items.create(
        db, id=id1, file_path=str(inbox_dir / "a.md"), content_hash="h1",
        status="processing", created_at="2026-04-10T00:00:00+00:00",
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        ("awaiting_approval:req-1", id1),
    )

    # Row 2: processing, no marker (should NOT appear)
    id2 = str(_uuid.uuid4())
    await inbox_items.create(
        db, id=id2, file_path=str(inbox_dir / "b.md"), content_hash="h2",
        status="processing", created_at="2026-04-10T00:01:00+00:00",
    )

    # Row 3: failed (should NOT appear)
    id3 = str(_uuid.uuid4())
    await inbox_items.create(
        db, id=id3, file_path=str(inbox_dir / "c.md"), content_hash="h3",
        status="failed", created_at="2026-04-10T00:02:00+00:00",
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        ("awaiting_approval:req-3", id3),
    )
    await db.commit()

    rows = await inbox_items.get_awaiting_approval(db)
    assert len(rows) == 1
    assert rows[0]["id"] == id1


# ---------------------------------------------------------------------------
# Wired approval-gate pre-check + state-transition resume tests
# ---------------------------------------------------------------------------


def _make_wired_dispatcher(
    *,
    decision: AutonomousDispatchDecision,
    pending_sites: list[dict] | None = None,
    approval_by_id: dict[str, dict] | None = None,
):
    """Build a mock dispatcher with a fully wired approval_gate.

    Unlike ``_make_dispatcher`` (which only stubs ``.route``), this one
    exposes ``.approval_gate.find_site_pending`` and
    ``._approval_gate._approval_manager.get_by_id`` so the monitor's
    pre-check and state-transition resume logic actually fire.
    """
    pending_sites = pending_sites or []
    approval_by_id = approval_by_id or {}

    async def _find_site_pending(*, subsystem: str, policy_id: str):
        for row in pending_sites:
            ctx = row.get("_context", {})
            if (
                ctx.get("subsystem") == subsystem
                and ctx.get("policy_id") == policy_id
            ):
                return row
        return None

    async def _get_by_id(request_id: str):
        return approval_by_id.get(request_id)

    approval_manager = SimpleNamespace(
        get_by_id=_get_by_id, cancel=AsyncMock(return_value=True),
    )
    # Use the PUBLIC accessor names: the resume pass walks
    # dispatcher.approval_gate.approval_manager via public properties
    # so wrappers/test doubles that only mirror the public API still
    # work without silent fall-through.
    approval_gate = SimpleNamespace(
        find_site_pending=_find_site_pending,
        approval_manager=approval_manager,
    )
    dispatcher = SimpleNamespace()
    dispatcher.route = AsyncMock(return_value=decision)
    dispatcher.approval_gate = approval_gate
    return dispatcher


@pytest.mark.asyncio
async def test_precheck_skips_detection_when_site_blocked_no_new_files(
    monitor, inbox_dir, mock_invoker, db,
):
    """When an inbox_evaluation approval is already pending and no new
    files were added, detection still short-circuits — no dispatch."""
    pending_site_row = {
        "id": "req-already-pending",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-already-pending",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )

    # No new files — just run check
    result = await monitor.check_once()

    # No dispatch happened
    monitor._autonomous_dispatcher.route.assert_not_called()
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_precheck_cancels_stale_approval_and_proceeds(
    monitor, inbox_dir, mock_invoker, db, clock,
):
    """When a pending inbox approval exceeds _MAX_APPROVAL_STALENESS,
    the monitor auto-cancels it and proceeds with normal detection
    instead of blocking indefinitely."""
    from genesis.inbox.monitor import _MAX_APPROVAL_STALENESS

    # Create an approval that's older than the staleness threshold
    stale_created = (clock.now - _MAX_APPROVAL_STALENESS - timedelta(hours=1)).isoformat()
    pending_site_row = {
        "id": "req-stale",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": stale_created,
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="allowed", reason="auto-approved",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )

    # Drop a file — should be detected because the stale approval is cancelled
    (inbox_dir / "notes.md").write_text("content after stale approval")
    result = await monitor.check_once()

    # The stale approval was cancelled and dispatch proceeded
    monitor._autonomous_dispatcher.approval_gate.approval_manager.cancel.assert_called_once_with(
        "req-stale",
    )
    assert result.items_new == 1
    assert result.batches_dispatched == 1


@pytest.mark.asyncio
async def test_precheck_does_not_cancel_fresh_approval(
    monitor, inbox_dir, mock_invoker, db, clock,
):
    """A pending approval younger than _MAX_APPROVAL_STALENESS is NOT
    cancelled — the monitor correctly blocks."""
    from genesis.inbox.monitor import _MAX_APPROVAL_STALENESS

    # Create an approval that's younger than the staleness threshold
    fresh_created = (clock.now - _MAX_APPROVAL_STALENESS + timedelta(hours=1)).isoformat()
    pending_site_row = {
        "id": "req-fresh",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": fresh_created,
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-fresh",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )

    # No new files — should block normally
    result = await monitor.check_once()

    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_precheck_refreshes_when_new_files_added_while_blocked(
    monitor, inbox_dir, mock_invoker, db,
):
    """When an inbox_evaluation approval is pending but new files arrive,
    the stale approval is cancelled and files are detected so a fresh
    approval reflecting the current inbox state can be created."""
    pending_site_row = {
        "id": "req-already-pending",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    # After the stale approval is cancelled, the dispatch will create
    # a new approval request (blocked again with a new request id).
    decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-fresh",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )

    # Drop a NEW file while approval is pending
    (inbox_dir / "new-while-blocked.md").write_text("fresh content")
    await monitor.check_once()

    # The new file WAS detected and a row was created
    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%new-while-blocked%'",
    )).fetchall())]
    assert len(rows) == 1, (
        f"new file should have been detected after stale approval cancel, "
        f"got: {rows}"
    )
    # Dispatch was attempted (creating a fresh approval)
    monitor._autonomous_dispatcher.route.assert_called_once()


@pytest.mark.asyncio
async def test_resume_pass_dispatches_on_pending_to_approved_transition(
    monitor, inbox_dir, mock_invoker, db,
):
    """With a wired approval manager, the resume pass dispatches an
    awaiting row ONLY when the approval transitions from pending to
    approved.  While pending, it must not dispatch."""
    # Scan 1: wired dispatcher returns blocked-pending → row parked
    pending_decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-transition-1",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,
        approval_by_id={
            "req-transition-1": {"id": "req-transition-1", "status": "pending"},
        },
    )
    (inbox_dir / "transition.md").write_text("content to approve")
    await monitor.check_once()

    rows1 = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md'",
    )).fetchall())]
    assert len(rows1) == 1
    assert rows1[0]["status"] == "processing"
    assert (rows1[0]["error_message"] or "").startswith("awaiting_approval:")

    # Scan 2: approval still pending.  Wire dispatcher to have a pending
    # site (pre-check will skip new detection) and approval_by_id to
    # return pending (resume pass will skip dispatch).  The test asserts
    # that NO dispatch happens this scan.
    pending_site_row = {
        "id": "req-transition-1", "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "_context": {
            "subsystem": "inbox", "policy_id": "inbox_evaluation",
        },
    }
    still_pending_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,
        pending_sites=[pending_site_row],
        approval_by_id={
            "req-transition-1": {"id": "req-transition-1", "status": "pending"},
        },
    )
    monitor._autonomous_dispatcher = still_pending_dispatcher
    await monitor.check_once()
    still_pending_dispatcher.route.assert_not_called()
    mock_invoker.run.assert_not_called()

    # Row still parked, not churned
    rows2 = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md'",
    )).fetchall())]
    assert len(rows2) == 1
    assert rows2[0]["id"] == rows1[0]["id"]
    assert rows2[0]["status"] == "processing"

    # Scan 3: approval now approved.  Resume pass should detect the
    # transition, dispatch the item, and CC should run to completion.
    approved_decision = AutonomousDispatchDecision(
        mode="cli_approved", reason="CLI fallback approved",
        approval_request_id="req-transition-1",
    )
    transition_dispatcher = _make_wired_dispatcher(
        decision=approved_decision,
        # No pending_sites — approval has resolved, find_site_pending
        # returns None, so pre-check allows detection/dispatch.
        approval_by_id={
            "req-transition-1": {"id": "req-transition-1", "status": "approved"},
        },
    )
    monitor._autonomous_dispatcher = transition_dispatcher
    mock_invoker.run.return_value = _success_output("resumed eval")

    result3 = await monitor.check_once()
    assert result3.batches_dispatched == 1
    mock_invoker.run.assert_called_once()

    # Row completed with no duplicates
    rows3 = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md' "
        "ORDER BY created_at",
    )).fetchall())]
    assert len(rows3) == 1
    assert rows3[0]["id"] == rows1[0]["id"]
    assert rows3[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_pass_marks_row_failed_on_rejected_transition(
    monitor, inbox_dir, mock_invoker, db,
):
    """When the wired approval manager reports the approval as rejected,
    the resume pass must mark the inbox row failed (with a rejection
    message) and not dispatch anything."""
    pending_decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-rejected-1",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,
        approval_by_id={
            "req-rejected-1": {"id": "req-rejected-1", "status": "pending"},
        },
    )
    (inbox_dir / "will-be-rejected.md").write_text("bad content")
    await monitor.check_once()

    # Second scan: approval now rejected
    rejected_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,  # shouldn't be called
        approval_by_id={
            "req-rejected-1": {"id": "req-rejected-1", "status": "rejected"},
        },
    )
    monitor._autonomous_dispatcher = rejected_dispatcher
    await monitor.check_once()
    rejected_dispatcher.route.assert_not_called()
    mock_invoker.run.assert_not_called()

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%will-be-rejected%'",
    )).fetchall())]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "reject" in (rows[0]["error_message"] or "").lower()


@pytest.mark.asyncio
async def test_resume_pass_invalidates_row_on_missing_approval(
    monitor, inbox_dir, mock_invoker, db,
):
    """When the approval row is missing entirely (approval_manager
    returns None), the inbox row must be invalidated with the
    APPROVAL_INVALIDATED_PREFIX marker so the next scan re-detects the
    file as new."""
    from genesis.db.crud import inbox_items

    pending_decision = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested",
        approval_request_id="req-gone-1",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,
        approval_by_id={
            "req-gone-1": {"id": "req-gone-1", "status": "pending"},
        },
    )
    (inbox_dir / "gone.md").write_text("content")
    await monitor.check_once()

    # Second scan: approval_by_id is empty → get_by_id returns None
    gone_dispatcher = _make_wired_dispatcher(
        decision=pending_decision,
        approval_by_id={},  # approval row vanished
    )
    monitor._autonomous_dispatcher = gone_dispatcher
    await monitor.check_once()

    rows = [dict(r) for r in (await (await db.execute(
        "SELECT * FROM inbox_items WHERE file_path LIKE '%gone.md'",
    )).fetchall())]
    # At least one row should be failed+invalidated.  The next scan may
    # also have created a fresh row for the re-detected file.
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) >= 1
    assert any(
        (r["error_message"] or "").startswith(inbox_items.APPROVAL_INVALIDATED_PREFIX)
        for r in failed
    )


def test_passes_coherence_check_valid():
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "# Inbox Evaluation\n\n**Classification:** Technology\n\nThis article from example.com " + "x" * 300
    source = "Check out https://example.com/article"
    assert _passes_coherence_check(evaluation, source) is True


def test_coherence_check_rejects_empty():
    from genesis.inbox.monitor import _passes_coherence_check
    assert _passes_coherence_check("", "some source") is False


def test_coherence_check_rejects_short():
    from genesis.inbox.monitor import _passes_coherence_check
    assert _passes_coherence_check("# Inbox Evaluation\nShort.", "src") is False


def test_coherence_check_rejects_missing_heading():
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "Some evaluation text " * 30  # >300 chars, no heading
    assert _passes_coherence_check(evaluation, "src") is False


def test_coherence_check_rejects_no_url_mentions():
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "# Inbox Evaluation\n\n" + "No URLs mentioned here " * 20
    source = "Check https://github.com/some/repo"
    assert _passes_coherence_check(evaluation, source) is False


def test_coherence_check_passes_with_url_domain():
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "# Inbox Evaluation\n\nThis article from github.com " + "x" * 300
    source = "Check https://github.com/some/repo"
    assert _passes_coherence_check(evaluation, source) is True


def test_coherence_check_no_urls_in_source():
    from genesis.inbox.monitor import _passes_coherence_check
    # No URLs in source content — URL check is skipped, other checks pass
    evaluation = "# Inbox Evaluation\n\n" + "Analysis of the plain text content " * 15
    source = "Just some plain text with no links"
    assert _passes_coherence_check(evaluation, source) is True


def test_coherence_check_passes_with_platform_name():
    """Evaluations use platform names ('LinkedIn') not raw domains ('www.linkedin.com')."""
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "# Inbox Evaluation\n\n**Classification:** Technology\n\nA LinkedIn post by Hao Hoang " + "x" * 300
    source = "Check out https://www.linkedin.com/posts/some-post"
    assert _passes_coherence_check(evaluation, source) is True


def test_coherence_check_passes_with_domain_stem_fallback():
    """Unknown domains still match via stem extraction (e.g. 'langchain' from 'langchain.com')."""
    from genesis.inbox.monitor import _passes_coherence_check
    evaluation = "# Inbox Evaluation\n\n**Classification:** Technology\n\nLangChain's new feature " + "x" * 300
    source = "https://www.langchain.com/blog/something"
    assert _passes_coherence_check(evaluation, source) is True
