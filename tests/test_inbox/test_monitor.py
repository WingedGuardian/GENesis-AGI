"""Tests for InboxMonitor — dispatch pipeline and lifecycle."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
from genesis.cc.types import CCOutput
from genesis.db.crud.prompt_versions import compute_prompt_hash
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


def _success_output(
    # Default fake response echoes this file's fixture URLs the way the
    # evaluation prompt now requires (**Source:** lines), so dispatch and
    # lifecycle tests pass the per-URL coverage gate. A platform name alone
    # is deliberately NOT coverage for a path-bearing URL — that rule has its
    # own dedicated tests in test_url_failures.py.
    text: str = (
        "evaluation result\n"
        "**Source:** https://example.com/article\n"
        "**Source:** https://example.com/article-1\n"
        "**Source:** https://example.com/article-2\n"
        "**Source:** https://example.com/article-3\n"
        "**Source:** https://example.com/first\n"
        "**Source:** https://example.com/second\n"
        "**Source:** https://www.linkedin.com/posts/foo-share-123-1G81/\n"
    ),
) -> CCOutput:
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
def monitor(db, mock_invoker, mock_session_manager, config, writer, clock):
    return InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_empty_folder(monitor):
    result = await monitor.check_once()
    assert result.items_new == 0
    assert result.batches_dispatched == 0
    assert result.errors == []


def test_build_lane_defaults_none(monitor):
    # Hook is inert until the build lane is late-wired.
    assert monitor._build_lane is None


def test_set_build_lane_wires_hook(monitor):
    sentinel = object()
    monitor.set_build_lane(sentinel)
    assert monitor._build_lane is sentinel


@pytest.mark.asyncio
async def test_silently_omitted_url_requeues_not_baselines(monitor, inbox_dir, db, mock_invoker):
    """An evaluation that never MENTIONS one of its URLs must not complete.

    Silent omission emits no give-up language, so _has_url_failures cannot
    see it; before the coverage gate, the batch was marked completed and the
    omitted URL's line was permanently absorbed into the baseline —
    unrecoverable, invisible loss.
    """
    # This test pins ENFORCEMENT; the shipped default is shadow
    # (see test_shadow_mode_logs_but_does_not_requeue). InboxConfig is frozen,
    # so swap the whole config rather than mutating a field.
    from dataclasses import replace as _replace

    monitor._config = _replace(monitor._config, url_coverage_mode="enforce")
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text(
        "https://alice:password@example.com/one-thing?token=secret-one "
        "https://other.org/two-thing?sig=secret-two"
    )
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n\n## one-thing\n"
        "Thorough discussion of the example.com piece and nothing else.\n" + "x" * 300
    )

    await monitor.check_once()

    row = await inbox_items.get_by_file_path(db, str(f))
    assert row["status"] == "failed"
    assert row["error_message"].startswith("partial_url_failure")
    assert "url#" in row["error_message"]
    assert "password" not in row["error_message"]
    assert "secret-one" not in row["error_message"]
    assert "secret-two" not in row["error_message"]
    assert row["evaluated_content"] is None
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_shadow_mode_logs_but_does_not_requeue(monitor, inbox_dir, db, mock_invoker, caplog):
    """The SHIPPED default, and the contrast with the test above.

    The coverage gate is NEW — `main` carries no such check — and a replay over
    the completed-evaluation corpus says enforcing it would flag roughly half of
    legacy-shaped responses on day one, into a retry path that parks a whole
    file after max_retries with no user notification. That would trade a
    silent-loss bug for a silent-stall one. So the gate computes its verdict,
    says so in the log, and acts on nothing until compliance has been measured.
    """
    import logging

    from genesis.db.crud import inbox_items

    assert monitor._config.url_coverage_mode == "shadow", "the shipped default must be shadow"

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one-thing?token=shadow-secret")
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n\n## one-thing\n"
        "Thorough discussion of the example.com piece and nothing else.\n" + "x" * 300
    )

    with caplog.at_level(logging.WARNING):
        await monitor.check_once()

    assert any("url-coverage SHADOW" in r.getMessage() for r in caplog.records), (
        "shadow mode must say what it WOULD have done"
    )
    assert "shadow-secret" not in caplog.text
    assert "url#" in caplog.text
    row = await inbox_items.get_by_file_path(db, str(f))
    assert row["status"] == "completed"  # acted on nothing
    assert row["evaluated_content"] is not None


@pytest.mark.asyncio
async def test_acknowledged_url_item_falls_through_to_coverage_gate(
    monitor, inbox_dir, db, mock_invoker
):
    """A URL-bearing item classified Acknowledged must NOT baseline its URLs
    unexamined — the Acknowledged fast-path is honored only for URL-free meta
    notes; here the un-covering response re-queues instead."""
    # This test pins ENFORCEMENT; the shipped default is shadow
    # (see test_shadow_mode_logs_but_does_not_requeue). InboxConfig is frozen,
    # so swap the whole config rather than mutating a field.
    from dataclasses import replace as _replace

    monitor._config = _replace(monitor._config, url_coverage_mode="enforce")
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one-thing")
    mock_invoker.run.return_value = _success_output(
        "**Classification:** Acknowledged\nNoted, nothing to do."
    )

    await monitor.check_once()

    row = await inbox_items.get_by_file_path(db, str(f))
    assert row["status"] == "failed"
    assert row["evaluated_content"] is None


@pytest.mark.asyncio
async def test_no_follow_ups_from_coverage_failed_eval(monitor, inbox_dir, db, mock_invoker):
    """Follow-ups/build cards only fire for COMPLETED evals — acting on an
    eval we just declared unevaluated would let dedup block the retry's
    corrected verdict."""
    # This test pins ENFORCEMENT; the shipped default is shadow
    # (see test_shadow_mode_logs_but_does_not_requeue). InboxConfig is frozen,
    # so swap the whole config rather than mutating a field.
    from dataclasses import replace as _replace

    monitor._config = _replace(monitor._config, url_coverage_mode="enforce")
    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one-thing https://other.org/two-thing")
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n\n## one-thing\nOnly this one.\n\n"
        "### Recommendation\n```yaml\naction: ADOPT\n"
        'next_step: "Try it"\nscope: V4\n```\n' + "x" * 300
    )

    await monitor.check_once()

    rows = await db.execute_fetchall("SELECT id FROM follow_ups")
    assert rows == []


@pytest.mark.asyncio
async def test_fully_covered_urls_complete_normally(monitor, inbox_dir, db, mock_invoker):
    """Regression guard: full coverage behaves exactly as before the gate."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one-thing https://other.org/two-thing")
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n\n## 1. one-thing\nGood piece.\n\n"
        "## 2. two-thing\nAlso solid.\n" + "x" * 300
    )

    await monitor.check_once()

    row = await inbox_items.get_by_file_path(db, str(f))
    assert row["status"] == "completed"
    assert row["evaluated_content"] is not None
    assert "https://example.com/one-thing" in row["evaluated_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_sink", ["follow_ups", "build_lane"])
async def test_cancellation_during_synchronous_side_effects_does_not_baseline(
    monitor,
    inbox_dir,
    db,
    mock_invoker,
    mock_session_manager,
    monkeypatch,
    cancelled_sink,
):
    """A process exit during durable side effects must leave the item retriable."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one-thing")
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n**Source:** https://example.com/one-thing\n" + "x" * 300
    )
    cancelled = AsyncMock(side_effect=asyncio.CancelledError)
    if cancelled_sink == "follow_ups":
        monkeypatch.setattr(monitor, "_create_follow_ups_from_eval", cancelled)
    else:
        monitor._build_lane = SimpleNamespace(handle_eval=cancelled)

    with pytest.raises(asyncio.CancelledError):
        await monitor.check_once()

    row = await inbox_items.get_by_file_path(db, str(f))
    assert row["status"] != "completed"
    assert row["evaluated_content"] is None
    mock_session_manager.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersede_does_not_burn_a_retry(monitor, inbox_dir, db):
    """A superseded pending row keeps its retry_count.

    Supersession ("the user edited the file before the old snapshot
    dispatched") is not a failure of the item — burning a retry on it walked
    repeatedly-edited files toward the max_retries exclusion for no reason,
    and (because the superseded row then exceeded the reuse threshold)
    accumulated duplicate rows instead of recycling one.
    """
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/article")
    # A stale pending row from an earlier scan of a previous file version.
    await inbox_items.create(
        db,
        id="stale-pending",
        file_path=str(f),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
    )

    await monitor.check_once()

    rows = await db.execute_fetchall(
        "SELECT id, status, retry_count FROM inbox_items WHERE file_path = ?",
        (str(f),),
    )
    # The superseded row is recycled for the fresh delta (reuse_as_pending),
    # evaluated, and completed — ONE row, retry budget untouched.
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["status"] == "completed"
    assert rows[0]["retry_count"] == 0


@pytest.mark.asyncio
async def test_supersede_near_retry_cap_still_recycles(monitor, inbox_dir, db):
    """At max_retries-1, a supersede must not push the row over the cap.

    Before the fix, supersession incremented retry_count to the cap, the row
    stopped being reusable, and a duplicate row was created for the fresh
    delta while the old one sat as a permanent phantom 'failure'.
    """
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/article")
    await inbox_items.create(
        db,
        id="worn-pending",
        file_path=str(f),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
    )
    # Two genuine failures already on the clock (max_retries default is 3).
    await inbox_items.update_status(db, "worn-pending", status="pending", retry_count=2)

    await monitor.check_once()

    rows = await db.execute_fetchall(
        "SELECT id, status, retry_count FROM inbox_items WHERE file_path = ?",
        (str(f),),
    )
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["status"] == "completed"
    assert rows[0]["retry_count"] == 2


@pytest.mark.asyncio
async def test_modified_file_supersedes_every_stale_pending_batch(
    monitor, inbox_dir, db
):
    """A multi-item interrupted drop must not leave invisible pending siblings."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/article")
    for index in range(2):
        await inbox_items.create(
            db,
            id=f"stale-{index}",
            file_path=str(f),
            content_hash="0" * 64,
            status="pending",
            created_at=f"2026-01-01T00:00:0{index}+00:00",
            drop_id="old-drop",
        )

    await monitor.check_once()

    rows = await db.execute_fetchall(
        "SELECT status FROM inbox_items WHERE file_path = ?",
        (str(f),),
    )
    assert rows
    assert all(row["status"] != "pending" for row in rows)


@pytest.mark.asyncio
async def test_modified_vanished_file_retires_stale_pending_batches(
    monitor, inbox_dir, db, monkeypatch
):
    """A vanished replacement must not strand its old batches."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/new")
    await inbox_items.create(
        db,
        id="stale",
        file_path=str(f),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(
        "genesis.inbox.monitor.read_content",
        Mock(side_effect=FileNotFoundError),
    )

    await monitor.check_once()

    row = await inbox_items.get_by_id(db, "stale")
    assert row["status"] == "failed"
    assert row["error_message"] == "pending_restart_requeue"


@pytest.mark.asyncio
async def test_temporarily_unreadable_pending_batch_recovers_when_readable(
    monitor, inbox_dir, db, monkeypatch, mock_invoker
):
    from genesis.db.crud import inbox_items
    from genesis.inbox import monitor as monitor_module
    from genesis.inbox.scanner import compute_hash

    f = inbox_dir / "links.md"
    content = "https://example.com/article"
    f.write_text(content)
    await inbox_items.create(
        db,
        id="pending",
        file_path=str(f),
        content_hash=compute_hash(f),
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
        drop_id="drop",
        batch_items=content,
    )
    original_read = monitor_module.read_content
    monkeypatch.setattr(
        "genesis.inbox.monitor.read_content",
        Mock(side_effect=PermissionError),
    )

    await monitor.check_once()
    assert (await inbox_items.get_by_id(db, "pending"))["status"] == "failed"
    mock_invoker.run.assert_not_awaited()

    monkeypatch.setattr("genesis.inbox.monitor.read_content", original_read)
    await monitor.check_once()

    assert (await inbox_items.get_by_id(db, "pending"))["status"] == "completed"
    mock_invoker.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_unmarked_processing_batch_is_rederived_immediately_after_restart(
    monitor, inbox_dir, db, mock_invoker,
):
    """A crash after the pre-dispatch claim must not wait for the 2h expiry."""
    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    f = inbox_dir / "links.md"
    content = "https://example.com/article"
    f.write_text(content)
    await inbox_items.create(
        db,
        id="claimed",
        file_path=str(f),
        content_hash=compute_hash(f),
        status="processing",
        # Must be younger than expire_stuck_processing's two-hour fallback;
        # otherwise the fixture never exercises immediate restart recovery.
        created_at=datetime.now(UTC).isoformat(),
        drop_id="drop",
        batch_items=content,
    )

    result = await monitor.check_once()

    assert result.batches_dispatched == 1
    assert (await inbox_items.get_by_id(db, "claimed"))["status"] == "completed"
    mock_invoker.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_batch_is_marked_dispatching_before_cc_invocation(
    monitor, inbox_dir, db, mock_invoker,
):
    """The durable phase marker must precede the external CC side effect."""
    from genesis.db.crud import inbox_items

    observed_markers = []

    async def _observe_marker(_invocation):
        cursor = await db.execute(
            "SELECT status, error_message FROM inbox_items ORDER BY rowid DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        observed_markers.append((row["status"], row["error_message"]))
        return _success_output()

    mock_invoker.run.side_effect = _observe_marker
    (inbox_dir / "links.md").write_text("https://example.com/article")

    assert (await monitor.check_once()).batches_dispatched == 1
    assert len(observed_markers) == 1
    status, marker = observed_markers[0]
    assert status == "processing"
    assert marker.startswith(inbox_items.DISPATCHING_PREFIX)


@pytest.mark.asyncio
async def test_unrelated_modification_does_not_reset_exhausted_item_retry_budget(
    monitor, inbox_dir, db, mock_invoker,
):
    """An exhausted URL stays terminal while genuinely new content is evaluated."""
    from dataclasses import replace

    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    monitor._config = replace(
        monitor._config,
        evaluation_cooldown_seconds=0,
        items_per_eval=1,
    )
    f = inbox_dir / "links.md"
    evaluated = "https://example.com/already-done"
    exhausted = "https://example.com/permanently-dead"
    new = "https://example.com/new-item"
    f.write_text(f"{evaluated}\n{exhausted}\n")
    old_hash = compute_hash(f)

    await inbox_items.create(
        db,
        id="completed",
        file_path=str(f),
        content_hash=old_hash,
        status="completed",
        created_at="2026-03-10T11:00:00+00:00",
    )
    await inbox_items.update_status(
        db,
        "completed",
        status="completed",
        processed_at="2026-03-10T11:00:00+00:00",
        evaluated_content=evaluated,
    )
    # Three old exhausted siblings used to trip the file-wide storm guard and
    # suppress even genuinely new work. Item-local terminal handling must keep
    # all three out of the new drop without imposing a time window.
    exhausted_ids = []
    for index in range(monitor._config.max_retries):
        row_id = f"exhausted-{index}"
        exhausted_ids.append(row_id)
        await inbox_items.create(
            db,
            id=row_id,
            file_path=str(f),
            content_hash=old_hash,
            status="failed",
            created_at=f"2020-01-0{index + 1}T00:00:00+00:00",
            batch_items=exhausted,
        )
        await inbox_items.update_status(
            db,
            row_id,
            status="failed",
            processed_at=f"2020-01-0{index + 1}T00:01:00+00:00",
            error_message="partial_url_failure",
            retry_count=monitor._config.max_retries,
        )

    mock_invoker.run.return_value = _success_output(
        f"# Inbox Evaluation\n**Source:** {new}\n" + "x" * 300
    )
    f.write_text(f"{evaluated}\n{exhausted}\n{new}\n")

    result = await monitor.check_once()

    assert result.batches_dispatched == 1
    invocation = mock_invoker.run.await_args.args[0]
    assert new in invocation.prompt
    assert exhausted not in invocation.prompt
    rows = await db.execute_fetchall(
        "SELECT id, status, retry_count FROM inbox_items WHERE file_path = ?",
        (str(f),),
    )
    exhausted_rows = [row for row in rows if row["retry_count"] >= monitor._config.max_retries]
    assert [row["id"] for row in exhausted_rows] == exhausted_ids


@pytest.mark.asyncio
async def test_modern_terminal_content_does_not_hide_opaque_legacy_storm(
    monitor, inbox_dir, db, mock_invoker,
):
    """Mixed rollout history keeps the legacy guard for unidentifiable rows."""
    from dataclasses import replace

    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    monitor._config = replace(monitor._config, evaluation_cooldown_seconds=0)
    f = inbox_dir / "links.md"
    modern_terminal = "https://example.com/known-dead"
    f.write_text(modern_terminal)
    old_hash = compute_hash(f)
    now_iso = datetime.now(UTC).isoformat()
    await inbox_items.create(
        db, id="modern", file_path=str(f), content_hash=old_hash,
        status="failed", created_at=now_iso, batch_items=modern_terminal,
        error_message="partial_url_failure", retry_count=monitor._config.max_retries,
    )
    for index in range(monitor._config.max_retries):
        await inbox_items.create(
            db, id=f"legacy-{index}", file_path=str(f), content_hash=old_hash,
            status="failed", created_at=now_iso,
            error_message="partial_url_failure", retry_count=monitor._config.max_retries,
        )

    f.write_text(f"{modern_terminal}\nhttps://example.com/new")
    result = await monitor.check_once()

    assert result.items_modified == 1
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_modern_terminal_siblings_do_not_block_distinct_retriable_batch(
    monitor, inbox_dir, db, mock_invoker,
):
    """Item-local terminal state must also apply in the auto-retry lane."""
    from dataclasses import replace

    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    monitor._config = replace(monitor._config, items_per_eval=1)
    f = inbox_dir / "links.md"
    terminal_urls = [f"https://example.com/dead-{index}" for index in range(3)]
    retriable = "https://example.com/retry-me"
    f.write_text("\n".join([*terminal_urls, retriable]))
    content_hash = compute_hash(f)
    now_iso = datetime.now(UTC).isoformat()
    for index, url in enumerate(terminal_urls):
        await inbox_items.create(
            db, id=f"terminal-{index}", file_path=str(f),
            content_hash=content_hash, status="failed", created_at=now_iso,
            batch_items=inbox_items.serialize_batch_items([url]),
            error_message="partial_url_failure",
            retry_count=monitor._config.max_retries,
        )
    await inbox_items.create(
        db, id="retriable", file_path=str(f), content_hash=content_hash,
        status="failed", created_at=now_iso,
        batch_items=inbox_items.serialize_batch_items([retriable]),
        error_message="partial_url_failure", retry_count=1,
    )
    mock_invoker.run.return_value = _success_output(
        f"# Inbox Evaluation\n**Source:** <{retriable}>\n" + "x" * 300
    )

    result = await monitor.check_once()

    assert result.items_retried == 1
    assert result.batches_dispatched == 1
    invocation = mock_invoker.run.await_args.args[0]
    assert retriable in invocation.prompt
    assert all(url not in invocation.prompt for url in terminal_urls)
    row = await inbox_items.get_by_id(db, "retriable")
    assert row["status"] == "completed"
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_pending_recovery_abandons_path_replaced_by_directory(
    monitor, inbox_dir, db, mock_invoker
):
    from genesis.db.crud import inbox_items

    replacement = inbox_dir / "was-a-file.md"
    replacement.mkdir()
    await inbox_items.create(
        db,
        id="pending",
        file_path=str(replacement),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
        drop_id="drop",
        batch_items="https://example.com/article",
    )

    await monitor.check_once()
    row = await inbox_items.get_by_id(db, "pending")

    assert row["status"] == "failed"
    assert row["error_message"] == (
        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}source path is not a file"
    )
    await monitor.check_once()
    mock_invoker.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_modified_file_retires_stale_pending_batches(monitor, inbox_dir, db):
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("")
    await inbox_items.create(
        db,
        id="stale",
        file_path=str(f),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
    )

    await monitor.check_once()

    assert (await inbox_items.get_by_id(db, "stale"))["status"] == "failed"


@pytest.mark.asyncio
async def test_cooldown_deferred_modification_retires_stale_pending_batches(
    monitor, inbox_dir, db, mock_invoker
):
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/new")
    await inbox_items.create(
        db,
        id="completed",
        file_path=str(f),
        content_hash="1" * 64,
        status="completed",
        created_at="2026-03-10T11:59:00+00:00",
    )
    await inbox_items.update_status(
        db,
        "completed",
        status="completed",
        processed_at="2026-03-10T12:00:00+00:00",
        evaluated_content="https://example.com/old",
    )
    await inbox_items.create(
        db,
        id="stale",
        file_path=str(f),
        content_hash="0" * 64,
        status="pending",
        created_at="2026-03-10T12:01:00+00:00",
    )

    await monitor.check_once()

    assert (await inbox_items.get_by_id(db, "stale"))["status"] == "failed"
    mock_invoker.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_batches_resume_after_crash_before_dispatch(
    monitor, inbox_dir, db, monkeypatch, mock_invoker
):
    """Durable batch payload is the startup authority after a phase-boundary crash."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/article")
    dispatch = monitor._phase_dispatch_batches
    monkeypatch.setattr(
        monitor,
        "_phase_dispatch_batches",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await monitor.check_once()
    pending = await inbox_items.query_pending(db)
    assert len(pending) == 1

    monkeypatch.setattr(monitor, "_phase_dispatch_batches", dispatch)
    await monitor.check_once()

    assert (await inbox_items.get_by_id(db, pending[0]["id"]))["status"] == "completed"
    mock_invoker.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_drop_creation_crash_rebuilds_every_item(
    monitor, inbox_dir, db, monkeypatch, mock_invoker
):
    """A durable prefix must never masquerade as a complete multi-batch drop."""
    from genesis.db.crud import inbox_items

    f = inbox_dir / "links.md"
    urls = [f"https://example.com/item-{index}" for index in range(3)]
    f.write_text("\n".join(urls))
    original_create = inbox_items.create
    pending_creates = 0

    async def crash_after_first_pending(*args, **kwargs):
        nonlocal pending_creates
        result = await original_create(*args, **kwargs)
        if kwargs.get("status") == "pending":
            pending_creates += 1
            if pending_creates == 1:
                raise asyncio.CancelledError
        return result

    monkeypatch.setattr(inbox_items, "create", crash_after_first_pending)
    with pytest.raises(asyncio.CancelledError):
        await monitor.check_once()
    assert len(await inbox_items.query_pending(db)) == 1

    monkeypatch.setattr(inbox_items, "create", original_create)
    result = await monitor.check_once()

    assert result.batches_dispatched == 3
    assert mock_invoker.run.await_count == 3
    baseline = await inbox_items.get_evaluated_content(db, str(f))
    assert baseline is not None
    assert all(url in baseline for url in urls)


@pytest.mark.asyncio
async def test_pending_recovery_restores_every_batch_in_a_drop(
    monitor, inbox_dir, db, mock_invoker
):
    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/one\nhttps://example.com/two")
    content_hash = compute_hash(f)
    for index, url in enumerate(
        ("https://example.com/one", "https://example.com/two")
    ):
        await inbox_items.create(
            db,
            id=f"pending-{index}",
            file_path=str(f),
            content_hash=content_hash,
            status="pending",
            created_at=f"2026-01-01T00:00:0{index}+00:00",
            drop_id="drop",
            batch_items=url,
        )

    result = await monitor.check_once()

    assert result.batches_dispatched == 2
    assert mock_invoker.run.await_count == 2
    statuses = [
        (await inbox_items.get_by_id(db, f"pending-{index}"))["status"]
        for index in range(2)
    ]
    assert statuses == ["completed", "completed"]


@pytest.mark.asyncio
async def test_pending_recovery_rebuilds_a_missing_durable_payload(
    monitor, inbox_dir, db, mock_invoker
):
    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    f = inbox_dir / "links.md"
    f.write_text("https://example.com/current")
    await inbox_items.create(
        db,
        id="malformed",
        file_path=str(f),
        content_hash=compute_hash(f),
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
        drop_id="drop",
        batch_items="",
        retry_count=2,
    )

    result = await monitor.check_once()

    rebuilt = await inbox_items.get_by_id(db, "malformed")
    assert rebuilt["status"] == "completed"
    assert inbox_items.batch_items_for_dispatch(rebuilt["batch_items"]) == (
        "https://example.com/current"
    )
    assert rebuilt["drop_id"] != "drop"
    assert rebuilt["retry_count"] == 2
    assert result.items_new == 1
    assert result.batches_dispatched == 1
    mock_invoker.run.assert_awaited_once()


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
    # 12 separate files → 12 drops (a drop = ONE file's delta; there is no
    # cross-file batching). Each single-line file segments to one item → one
    # batch → one eval. (Within-file batching of ~items_per_eval is covered by
    # test_drop_dispatch.py.)
    for i in range(12):
        (inbox_dir / f"item-{i:02d}.md").write_text(f"content {i}")
    result = await monitor.check_once()
    assert result.items_new == 12
    assert result.batches_dispatched == 12
    assert mock_invoker.run.call_count == 12


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
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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
    monitor,
    inbox_dir,
    mock_invoker,
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
        c
        for c in event_bus.emit.call_args_list
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
async def test_missing_watch_path(db, mock_invoker, mock_session_manager, tmp_path):
    config = InboxConfig(watch_path=tmp_path / "nonexistent")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
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
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """Modified file within cooldown period should not be re-evaluated."""
    clock = _FakeClock()
    config = InboxConfig(
        watch_path=inbox_dir,
        batch_size=5,
        evaluation_cooldown_seconds=3600,
    )
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
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


@pytest.mark.asyncio
async def test_cooldown_defers_without_dropping_modification(
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """A modification made within cooldown must still be evaluated once the
    cooldown elapses, even if the file is never edited again.

    Regression (cooldown-consume): the cooldown branch used to write a
    'completed' row carrying the new content hash with no evaluation, which
    advanced the known hash so the change was never re-detected — stranding
    the new content until the next edit.
    """
    clock = _FakeClock()
    config = InboxConfig(
        watch_path=inbox_dir,
        batch_size=5,
        evaluation_cooldown_seconds=3600,
    )
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )
    f = inbox_dir / "doc.md"

    f.write_text("alpha")
    assert (await mon.check_once()).batches_dispatched == 1

    # Edit within cooldown -> deferred, not dispatched.
    clock.now = clock.now + timedelta(minutes=10)
    f.write_text("alpha\nbeta")
    r2 = await mon.check_once()
    assert r2.items_modified == 1
    assert r2.batches_dispatched == 0

    mock_invoker.run.reset_mock()

    # Cooldown elapses; the file is NOT touched again. The deferred change
    # must now be picked up and evaluated (not stranded with the hash advanced).
    clock.now = clock.now + timedelta(hours=2)
    r3 = await mon.check_once()
    assert r3.batches_dispatched == 1, "modification deferred during cooldown was never evaluated"
    mock_invoker.run.assert_called_once()


@pytest.mark.asyncio
async def test_e2e_url_repaste_different_tracking_not_reevaluated(
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """End-to-end via check_once: the same article re-pasted with different
    share/tracking params is detected as a file change but produces no new
    content, so it is not re-evaluated (no duplicate dispatch)."""
    clock = _FakeClock()
    # cooldown=0 isolates the URL-dedup path from the cooldown defer.
    config = InboxConfig(
        watch_path=inbox_dir,
        batch_size=5,
        evaluation_cooldown_seconds=0,
    )
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )
    f = inbox_dir / "Genesis.md"
    base = "https://www.linkedin.com/posts/foo-share-123-1G81/"

    # First paste (android share) -> evaluated.
    f.write_text(base + "?utm_source=share&utm_medium=member_android&rcm=AAA")
    assert (await mon.check_once()).batches_dispatched == 1

    mock_invoker.run.reset_mock()

    # Re-paste of the SAME post from desktop (different tracking params).
    clock.now = clock.now + timedelta(minutes=1)
    f.write_text(base + "?utm_source=share&utm_medium=member_desktop&rcm=BBB")
    r2 = await mon.check_once()
    assert r2.items_modified == 1  # hash changed -> detected as modified
    assert r2.batches_dispatched == 0  # ...but no NEW content -> no re-eval
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_phantom_modified_within_cooldown_advances_hash(
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """A file modified within cooldown but with NO new content (empty delta)
    must write a completing row that ADVANCES the known hash, so it is not
    re-detected as modified on every subsequent scan.

    Regression (detection storm): the cooldown branch used to `continue`
    before the delta check, so an empty-delta phantom-modification never
    advanced the hash and was re-detected every scan for hours.
    """
    clock = _FakeClock()
    config = InboxConfig(
        watch_path=inbox_dir,
        batch_size=5,
        evaluation_cooldown_seconds=3600,
    )
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )
    f = inbox_dir / "Genesis.md"
    base = "https://www.linkedin.com/posts/foo-share-123-1G81/"

    # First paste -> evaluated (baseline set).
    f.write_text(base + "?utm_source=share&utm_medium=member_android")
    assert (await mon.check_once()).batches_dispatched == 1
    mock_invoker.run.reset_mock()

    # WITHIN cooldown, re-paste the SAME post with different tracking params:
    # hash changes (detected modified) but the delta is empty. Must NOT
    # dispatch, but MUST advance the known hash.
    clock.now = clock.now + timedelta(minutes=10)
    f.write_text(base + "?utm_source=share&utm_medium=member_desktop")
    r2 = await mon.check_once()
    assert r2.items_modified == 1
    assert r2.batches_dispatched == 0
    mock_invoker.run.assert_not_called()

    # Next scan, file UNCHANGED: must NOT be re-detected as modified — the
    # empty-delta write advanced the known hash even though we were in cooldown.
    clock.now = clock.now + timedelta(minutes=10)
    r3 = await mon.check_once()
    assert r3.items_modified == 0, (
        "phantom-modified re-detected: the empty-delta write did not advance "
        "the known hash within cooldown (storm)"
    )
    assert r3.batches_dispatched == 0


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
    text = "https://www.google.com/search?client=firefox-b-1-m&q=Selling%20AI%20Agents"
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
@pytest.mark.parametrize(
    ("content", "listed"),
    [
        # Balanced delimiters are URL-borne and must survive display trimming.
        (
            "https://en.wikipedia.org/wiki/Foo_(bar)",
            "https://en.wikipedia.org/wiki/Foo_(bar)",
        ),
        # An IPv6 authority keeps its bracket -- the `[` is still open.
        ("https://[::1]:8443/p", "https://[::1]:8443/p"),
        # A markdown wrapper is NOT part of the URL and must not be requested.
        ("[docs](https://example.com/foo)", "https://example.com/foo"),
        # Nor is a surrounding quote, with or without trailing prose comma.
        ('see "https://example.com/x", later', "https://example.com/x"),
        # A code span around a URL is likewise presentation.
        ("`https://example.com/y`.", "https://example.com/y"),
        # A URL-shaped markdown LABEL is its own token and must not absorb
        # `](https://...` -- this case reaches the bare-domain `]` stop, which
        # the `[docs](...)` cases above never do.
        ("[example.com/a](https://example.com/a)", "example.com/a"),
    ],
)
async def test_build_prompt_lists_the_url_the_writer_meant(
    monitor,
    inbox_dir,
    content,
    listed,
):
    """The prompt asks for a fetchable URL, not the gate's lossless token.

    The coverage grammar keeps every terminal character so the GATE can fail
    closed on ambiguous punctuation. Handing that token to the evaluator asks
    it to fetch a resource that does not exist -- MEASURED at 340 of 18,119
    tokens (1.88%) ending in wrapper punctuation on this install's corpus,
    falling to 11 (0.06%) once unmatched delimiters are trimmed.

    Display trimming is structural: a paired delimiter is removed only when
    the remainder leaves it unmatched. The gate accepts either rendering of
    the SAME token, and no more -- see
    ``test_a_truncated_sibling_still_cannot_vouch_for_another_url``.
    """
    from genesis.inbox.types import InboxItem

    item = InboxItem(
        id="display-url",
        file_path=str(inbox_dir / "links.md"),
        content=content,
        content_hash="abc",
        detected_at="2026-09-14",
    )

    prompt = monitor._build_prompt([item])
    # Assert the WHOLE enumerated line. A bare ``in`` check is vacuous here:
    # "1. https://example.com/foo" is a substring of the untrimmed
    # "1. https://example.com/foo)", so it passes on the failure path too.
    enumerated = [
        re.sub(r"^\d+\. ", "", line)
        for line in prompt.splitlines()
        if re.fullmatch(r"\d+\. \S+", line)
    ]
    assert listed in enumerated, f"{listed!r} not enumerated; got {enumerated!r}"


def test_citing_exactly_what_the_prompt_asked_for_covers_the_url():
    """A compliant answer must be able to satisfy the gate.

    The prompt shows ``_display_url(token)`` while the gate discovered
    ``token``. If only the untrimmed form counted, every URL whose two
    renderings differ would be permanently uncoverable -- a floor under the
    shadow flag rate, which is the signal the enforce decision reads.
    """
    from genesis.inbox.monitor import _display_url, _uncovered_urls

    source = '"https://example.com/q?x=1",'
    assert _display_url('https://example.com/q?x=1",') == "https://example.com/q?x=1"

    # What the prompt actually asked for.
    assert _uncovered_urls("**Source:** <https://example.com/q?x=1>", source) == []
    # The untrimmed token the gate discovered still counts too.
    assert _uncovered_urls('**Source:** <https://example.com/q?x=1",>', source) == []


def test_ambiguous_sentence_punctuation_is_never_trimmed_for_display():
    """Round 3's fail-closed rule survives the display layer.

    ``/path;`` and ``/q?x=1!`` are legal URLs. Nothing structurally proves the
    terminal character is prose, so trimming it would ask the evaluator for a
    DIFFERENT resource than the user saved -- and the gate would then accept
    that answer, which is the silent loss this whole gate exists to prevent.
    A trim only stands when it removed a paired delimiter.
    """
    from genesis.inbox.monitor import _display_url, _uncovered_urls

    for ambiguous in ("https://example.com/path;", "https://example.com/q?x=1!"):
        assert _display_url(ambiguous) == ambiguous
        truncated = ambiguous[:-1]
        assert _uncovered_urls(f"**Source:** {truncated}", ambiguous) == [ambiguous]


def test_a_truncated_sibling_still_cannot_vouch_for_another_url():
    """Accepting two renderings of ONE token must not widen to siblings.

    This is the round-3 guarantee. ``/foo`` and ``/foo:bar`` are distinct
    resources; neither one's display form is the other's identity, so citing
    one must leave the other uncovered.
    """
    from genesis.inbox.monitor import _uncovered_urls

    source = "https://example.com/foo\nhttps://example.com/foo:bar"

    cited_prefix = "**Source:** <https://example.com/foo>"
    assert _uncovered_urls(cited_prefix, source) == ["https://example.com/foo:bar"]

    cited_longer = "**Source:** <https://example.com/foo:bar>"
    assert _uncovered_urls(cited_longer, source) == ["https://example.com/foo"]


def test_bare_domain_label_does_not_swallow_the_link_target():
    """DISCOVERY must end a bare-domain token at `]`.

    A URL-shaped markdown LABEL is its own token. Without the stop it absorbs
    `](https://...` into one string that is neither URL and that no response
    can ever cite. Asserted on the extractor directly: the display layer
    trims the trailing `)` and would mask a regression here.
    """
    from genesis.inbox.monitor import _extract_coverage_input_urls

    assert _extract_coverage_input_urls("[example.com/a](https://example.com/a)") == [
        "example.com/a",
        "https://example.com/a)",
    ]


def test_evidence_validation_keeps_bracketed_query_params():
    """VALIDATION must NOT inherit discovery's `]` stop.

    A Source field is already delimited, so there is no prose to end at.
    Narrowing it would reject a legitimate schemeless citation carrying a
    bracketed query parameter, and the URL would read as uncovered even
    though the evaluator cited it exactly.
    """
    from genesis.inbox.monitor import _extract_coverage_input_urls, _extract_source_urls

    cited = "example.com/s?f[0]=x"
    assert _extract_source_urls(f"**Source:** {cited}") == [cited]

    # DISCOVERY is the narrowed one, and deliberately so: scanning prose it
    # stops the bare-domain form at `]`. A schemeless URL carrying a bracketed
    # query parameter is therefore discovered truncated -- a known limit, not
    # a regression from sharing one pattern. The scheme'd form is unaffected,
    # and in practice such params are written with a scheme.
    assert _extract_coverage_input_urls(cited) == ["example.com/s?f[0"]
    assert _extract_coverage_input_urls(f"https://{cited}") == [f"https://{cited}"]


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


@pytest.mark.asyncio
async def test_build_prompt_includes_delta_instruction(monitor, inbox_dir):
    """Prompt includes delta evaluation instruction to prevent re-evaluation."""
    from genesis.inbox.types import InboxItem

    item = InboxItem(
        id="delta-test",
        file_path=str(inbox_dir / "Genesis.md"),
        content="https://example.com/new-article",
        content_hash="ghi",
        detected_at="2026-05-21",
    )
    prompt = monitor._build_prompt([item])
    assert "DELTA EVALUATION" in prompt
    assert "Do NOT use the Read tool" in prompt
    assert "ONLY" in prompt


# --- Standing bracketed lines (bracket re-injection) ---


def test_extract_bracket_directives_shapes():
    """Whole-line [ ... ] directives extracted; mid-line / link lines rejected."""
    from genesis.inbox.monitor import _extract_bracket_directives

    text = (
        "[build everything here]\n"
        "  [ leading and trailing space ]  \n"
        "regular line\n"
        "see [the docs](https://example.com)\n"  # markdown link — not a directive
        "prefix [not a directive] suffix\n"  # bracket mid-line — rejected
        "[build everything here]\n"  # exact duplicate — deduped
    )
    directives = _extract_bracket_directives(text)
    assert directives == [
        "[build everything here]",
        "[ leading and trailing space ]",
    ]


def test_extract_bracket_directives_empty():
    from genesis.inbox.monitor import _extract_bracket_directives

    assert _extract_bracket_directives("") == []
    assert _extract_bracket_directives("no brackets at all\njust text") == []


@pytest.mark.asyncio
async def test_build_prompt_injects_standing_directives(monitor, inbox_dir):
    """A standing [ ... ] directive in the source file renders on a delta eval
    even when the delta content does NOT contain the bracket line (the bug: the
    directive is baselined away after the first eval)."""
    from genesis.inbox.types import InboxItem

    src = inbox_dir / "New Genesis Capabilities.md"
    src.write_text(
        "[If it's in here, default to building it]\n"
        "https://example.com/old-item\n"
        "https://example.com/new-item\n"
    )
    item = InboxItem(
        id="d1",
        file_path=str(src),
        content="https://example.com/new-item",
        content_hash="h",
        detected_at="2026-07-27",
    )
    prompt = monitor._build_prompt([item])
    assert "Standing bracketed lines" in prompt
    assert "default to building it" in prompt
    assert "context" in prompt.lower()


@pytest.mark.asyncio
async def test_build_prompt_no_directive_section_without_brackets(monitor, inbox_dir):
    from genesis.inbox.types import InboxItem

    src = inbox_dir / "plain.md"
    src.write_text("just plain notes\nhttps://example.com/x\n")
    item = InboxItem(
        id="p1",
        file_path=str(src),
        content="https://example.com/x",
        content_hash="h",
        detected_at="2026-07-27",
    )
    prompt = monitor._build_prompt([item])
    assert "Standing bracketed lines" not in prompt


@pytest.mark.asyncio
async def test_build_prompt_directives_missing_file_graceful(monitor, inbox_dir):
    """A deleted/renamed source file yields no directive section and no raise."""
    from genesis.inbox.types import InboxItem

    item = InboxItem(
        id="g1",
        file_path=str(inbox_dir / "does-not-exist.md"),
        content="https://example.com/x",
        content_hash="h",
        detected_at="2026-07-27",
    )
    prompt = monitor._build_prompt([item])  # must not raise
    assert "Standing bracketed lines" not in prompt
    assert "### Content:" in prompt


@pytest.mark.asyncio
async def test_build_prompt_directives_are_sanitized(monitor, inbox_dir):
    """Directive text is wrapped by the perimeter sanitizer (external-untrusted)."""
    from genesis.inbox.types import InboxItem

    src = inbox_dir / "inject.md"
    # Any inbox content is wrapped in perimeter boundary markers regardless of
    # whether it trips an injection pattern — a benign directive proves wrapping.
    src.write_text("[build everything in here by default]\nhttps://example.com/x\n")
    item = InboxItem(
        id="s1",
        file_path=str(src),
        content="https://example.com/x",
        content_hash="h",
        detected_at="2026-07-27",
    )
    prompt = monitor._build_prompt([item])
    assert "Standing bracketed lines" in prompt
    assert '<external-content source="inbox"' in prompt


@pytest.mark.asyncio
async def test_build_prompt_multi_item_directives_per_file(monitor, inbox_dir):
    """Two items from different files each surface their own file's directives."""
    from genesis.inbox.types import InboxItem

    a = inbox_dir / "A.md"
    a.write_text("[directive alpha]\nhttps://example.com/a\n")
    b = inbox_dir / "B.md"
    b.write_text("[directive beta]\nhttps://example.com/b\n")
    items = [
        InboxItem(
            id="a",
            file_path=str(a),
            content="https://example.com/a",
            content_hash="ha",
            detected_at="2026-07-27",
        ),
        InboxItem(
            id="b",
            file_path=str(b),
            content="https://example.com/b",
            content_hash="hb",
            detected_at="2026-07-27",
        ),
    ]
    prompt = monitor._build_prompt(items)
    assert "directive alpha" in prompt
    assert "directive beta" in prompt


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
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """When CC classifies as Acknowledged, no response file is written."""
    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )
    mock_invoker.run.return_value = _success_output(
        "**Classification:** Acknowledged\nNoted: this file is user-specific context."
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
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """Acknowledged items store evaluated_content for delta computation."""
    from genesis.db.crud import inbox_items

    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
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
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """Non-Acknowledged items still get a response file written."""
    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
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
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """Files with many prior evaluations are still processed when content changes."""
    import uuid

    from genesis.db.crud import inbox_items

    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
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
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%pending.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "processing"
    assert rows[0]["error_message"].startswith("awaiting_approval:")
    assert "req-abc-123" in rows[0]["error_message"]


@pytest.mark.asyncio
async def test_blocked_rejected_marks_row_failed(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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
    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%rejected.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    # Error message is the generic "CLI fallback blocked: ..." not the marker
    assert "rejection" in rows[0]["error_message"].lower()


@pytest.mark.asyncio
async def test_blocked_policy_disabled_marks_row_failed(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%disabled.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_resume_pass_redispatches_awaiting_row(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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

    rows1 = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%resume.md'",
                )
            ).fetchall()
        )
    ]
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
    rows2 = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%resume.md' "
                    "ORDER BY created_at",
                )
            ).fetchall()
        )
    ]
    assert len(rows2) == 1
    assert rows2[0]["id"] == rows1[0]["id"]
    assert rows2[0]["status"] == "completed"

    # The response file was written
    assert list(inbox_dir.glob("*resume*.genesis.md"))


@pytest.mark.asyncio
async def test_resume_pass_invalidates_row_when_file_changed(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT id, status, error_message, content_hash "
                    "FROM inbox_items WHERE file_path LIKE '%changed.md' "
                    "ORDER BY created_at",
                )
            ).fetchall()
        )
    ]
    # Must have exactly two rows: the invalidated original and a fresh one
    assert len(rows) == 2, f"expected original (invalidated) + fresh row, got {len(rows)}: {rows}"
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
    monitor,
    inbox_dir,
    mock_invoker,
    db,
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT status, error_message FROM inbox_items "
                    "WHERE file_path LIKE '%vanished.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "vanished" in (rows[0]["error_message"] or "")


@pytest.mark.asyncio
async def test_dispatch_request_omits_volatile_context(
    monitor,
    inbox_dir,
    mock_invoker,
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
async def test_resume_does_not_reroute_across_scans(
    monitor,
    inbox_dir,
    mock_invoker,
):
    """A parked drop is dispatched DIRECTLY on resume — it does not re-enter
    ``route()`` — so re-scanning a pending item never fires a duplicate
    approval/Telegram. Regression guard for the whole fix-it-now motivation:
    exactly ONE route() call across scans for the same content (the old design
    re-routed every scan and depended on stable-key dedup to suppress the
    duplicate; the new design simply doesn't re-route)."""
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

    # First scan: row created, one approval requested, drop parked.
    await monitor.check_once()
    # Second scan: resume pass dispatches the parked drop directly (no route()).
    await monitor.check_once()

    assert len(captured) == 1, (
        f"expected 1 route() call (resume dispatches directly), got {len(captured)}"
    )
    req = captured[0]
    # The single request carries the stable-key inputs + null context that keep
    # the approval content-agnostic and dedupable.
    assert req.subsystem == "inbox"
    assert req.policy_id == "inbox_evaluation"
    assert req.action_label == "inbox evaluation"
    assert req.approval_key_stable is True
    assert req.api_call_site_id is None
    assert req.context is None
    system_message = req.messages[0]["content"]
    assert system_message == req.cli_invocation.system_prompt
    assert monitor._prompt_hash == compute_prompt_hash(system_message)


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
        db,
        id=awaiting_id,
        file_path=str(inbox_dir / "await.md"),
        content_hash="h1",
        status="processing",
        created_at=stale_created_at,
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        ("awaiting_approval:req-xyz", awaiting_id),
    )
    await inbox_items.create(
        db,
        id=stuck_id,
        file_path=str(inbox_dir / "stuck.md"),
        content_hash="h2",
        status="processing",
        created_at=stale_created_at,
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
        db,
        id=id1,
        file_path=str(inbox_dir / "a.md"),
        content_hash="h1",
        status="processing",
        created_at="2026-04-10T00:00:00+00:00",
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        ("awaiting_approval:req-1", id1),
    )

    # Row 2: processing, no marker (should NOT appear)
    id2 = str(_uuid.uuid4())
    await inbox_items.create(
        db,
        id=id2,
        file_path=str(inbox_dir / "b.md"),
        content_hash="h2",
        status="processing",
        created_at="2026-04-10T00:01:00+00:00",
    )

    # Row 3: failed (should NOT appear)
    id3 = str(_uuid.uuid4())
    await inbox_items.create(
        db,
        id=id3,
        file_path=str(inbox_dir / "c.md"),
        content_hash="h3",
        status="failed",
        created_at="2026-04-10T00:02:00+00:00",
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
            if ctx.get("subsystem") == subsystem and ctx.get("policy_id") == policy_id:
                return row
        return None

    async def _get_by_id(request_id: str):
        return approval_by_id.get(request_id)

    approval_manager = SimpleNamespace(
        get_by_id=_get_by_id,
        cancel=AsyncMock(return_value=True),
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


async def _seed_parked_row(
    db,
    inbox_dir,
    *,
    request_id: str,
    filename: str = "parked.md",
    content: str = "parked content",
):
    """Create a real inbox file + a LIVE awaiting_approval row referencing
    ``request_id``, so the monitor's orphan-guard sees the approval as still
    bound to live work (and the resume pass leaves the row parked).

    Returns ``(path, content_hash)``.
    """
    from genesis.db.crud import inbox_items
    from genesis.inbox.scanner import compute_hash

    f = inbox_dir / filename
    f.write_text(content)
    h = compute_hash(f)
    row_id = f"row-{request_id}"
    await inbox_items.create(
        db,
        id=row_id,
        file_path=str(f),
        content_hash=h,
        status="processing",
        created_at="2026-03-10T11:00:00+00:00",
    )
    await db.execute(
        "UPDATE inbox_items SET error_message = ? WHERE id = ?",
        (f"{inbox_items.AWAITING_APPROVAL_PREFIX}{request_id}", row_id),
    )
    await db.commit()
    return f, h


@pytest.mark.asyncio
async def test_precheck_skips_detection_when_site_blocked_no_new_files(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """When an inbox_evaluation approval is already pending (with a live
    parked row) and no new files were added, detection short-circuits —
    the approval HOLDS, no dispatch and no cancel."""
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
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-already-pending",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
        approval_by_id={
            "req-already-pending": {
                "id": "req-already-pending",
                "status": "pending",
            }
        },
    )
    # A live parked row keeps the approval bound to real work (not orphaned).
    await _seed_parked_row(db, inbox_dir, request_id="req-already-pending")

    # No NEW files — just run check
    result = await monitor.check_once()

    # No dispatch happened, and the held approval was NOT cancelled.
    monitor._autonomous_dispatcher.route.assert_not_called()
    monitor._autonomous_dispatcher.approval_gate.approval_manager.cancel.assert_not_called()
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_precheck_cancels_orphaned_approval(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    clock,
):
    """A pending inbox approval that NO live inbox row references is
    orphaned — it can never be dispatched, so the monitor cancels it for
    recovery and proceeds with normal detection. This holds regardless of
    the approval's age (no age threshold)."""
    # A *young* approval, deliberately, to prove the trigger is orphaning and
    # NOT age. No _seed_parked_row call => zero live rows => orphaned.
    fresh_created = (clock.now - timedelta(minutes=5)).isoformat()
    pending_site_row = {
        "id": "req-orphan",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": fresh_created,
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-orphan",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )

    # NO new file: the ONLY reason a cancel can happen is the orphan-guard
    # (a young approval is not stale, and with no new content the legacy
    # refresh path never fires) — so this cleanly isolates orphan recovery.
    result = await monitor.check_once()

    # The orphaned approval was cancelled for recovery.
    monitor._autonomous_dispatcher.approval_gate.approval_manager.cancel.assert_called_once_with(
        "req-orphan",
    )
    assert result.batches_dispatched == 0


@pytest.mark.asyncio
async def test_orphan_cancel_lost_race_holds_without_routing(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    clock,
):
    """TOCTOU guard (Codex P1): if an orphaned approval is APPROVED by the user
    between find_site_pending() and cancel(), cancel() returns False (the row is
    no longer pending). The monitor must then HOLD this cycle — it must NOT clear
    the hold and route new content onto the just-approved, content-agnostic
    stable-key approval. Hold now; the next scan re-reads a fresh state."""
    pending_site_row = {
        "id": "req-race",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": (clock.now - timedelta(minutes=5)).isoformat(),
        "_context": {"subsystem": "inbox", "policy_id": "inbox_evaluation"},
    }
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-race",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
    )
    # Simulate the race: by the time cancel() runs, the row is already resolved
    # (user approved it) -> resolve() matches 0 pending rows -> returns False.
    monitor._autonomous_dispatcher.approval_gate.approval_manager.cancel = AsyncMock(
        return_value=False,
    )
    # New content arrives — it must NOT be routed onto the raced approval.
    (inbox_dir / "raced.md").write_text("new content during the race")

    result = await monitor.check_once()

    disp = monitor._autonomous_dispatcher
    disp.approval_gate.approval_manager.cancel.assert_called_once_with("req-race")
    disp.route.assert_not_called()
    assert result.batches_dispatched == 0
    # The new file was NOT recorded/dispatched this cycle (the monitor held).
    row = await (
        await db.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE file_path LIKE '%raced.md'",
        )
    ).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_precheck_holds_approval_with_live_rows(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    clock,
):
    """A pending approval that a live inbox row still references is HELD —
    never cancelled — no matter how old it is. This is 'block until
    approved, no re-ask' for healthy approvals."""
    # Make the approval ancient to prove age does NOT trigger a cancel.
    ancient_created = (clock.now - timedelta(days=30)).isoformat()
    pending_site_row = {
        "id": "req-held",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": ancient_created,
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-held",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
        approval_by_id={"req-held": {"id": "req-held", "status": "pending"}},
    )
    # A live parked row referencing the approval => not orphaned => hold.
    await _seed_parked_row(db, inbox_dir, request_id="req-held")

    # No new files — should hold, not cancel, not dispatch
    result = await monitor.check_once()

    monitor._autonomous_dispatcher.approval_gate.approval_manager.cancel.assert_not_called()
    monitor._autonomous_dispatcher.route.assert_not_called()
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_called()


@pytest.mark.asyncio
async def test_pending_inbox_approval_blocks_without_reask(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    clock,
):
    """The user's guarantee: a held inbox approval is re-asked ZERO times.
    Across many scans, with the clock advanced well past any historical
    re-ask window, the monitor never cancels and never re-routes (which
    would mint a fresh approval), and the parked row stays live."""
    pending_site_row = {
        "id": "req-quiet",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "created_at": (clock.now - timedelta(hours=1)).isoformat(),
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
        },
    }
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
        approval_request_id="req-quiet",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
        approval_by_id={"req-quiet": {"id": "req-quiet", "status": "pending"}},
    )
    _, _ = await _seed_parked_row(db, inbox_dir, request_id="req-quiet")

    for _ in range(4):
        clock.now = clock.now + timedelta(hours=12)  # sail past 24h re-ask window
        await monitor.check_once()

    disp = monitor._autonomous_dispatcher
    disp.approval_gate.approval_manager.cancel.assert_not_called()
    disp.route.assert_not_called()
    from genesis.db.crud import inbox_items

    row = await inbox_items.get_by_id(db, "row-req-quiet")
    assert row["status"] == "processing"
    assert row["error_message"] == f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-quiet"


@pytest.mark.asyncio
async def test_precheck_refreshes_when_new_files_added_while_blocked(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """When an inbox_evaluation approval is pending but new files arrive,
    the approval is cancelled and files are detected so a fresh approval
    reflecting the current inbox state can be created.

    Note: this case seeds no live parked row, so under the orphan-guard the
    approval is cancelled via the orphan path before the refresh path; the
    end state (file detected, one route()) is equivalent. The genuine
    refresh-with-live-rows / fold path is pinned separately by
    ``test_refresh_path_with_live_rows_folds_unchanged_sibling``.
    """
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
        mode="blocked",
        reason="approval requested",
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
    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%new-while-blocked%'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1, (
        f"new file should have been detected after stale approval cancel, got: {rows}"
    )
    # Dispatch was attempted (creating a fresh approval)
    monitor._autonomous_dispatcher.route.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_path_with_live_rows_folds_unchanged_sibling(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """Park-onto-pending (2026-08-18 idempotent-approval semantics): when an
    approval with LIVE parked rows is pending and genuinely-new content
    arrives, the orphan-guard HOLDS (live rows exist), the request is NEVER
    cancelled, the unchanged parked sibling stays parked untouched on the
    SAME request, and the new file's drop parks alongside it (the gate's
    stable site key re-attaches it — mocked here as the same decision id).
    No new approval message is ever produced for content changes."""
    pending_site_row = {
        "id": "req-a",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "_context": {"subsystem": "inbox", "policy_id": "inbox_evaluation"},
    }
    # The gate's stable key re-attaches new drops to the SAME pending request.
    decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval pending",
        approval_request_id="req-a",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=decision,
        pending_sites=[pending_site_row],
        approval_by_id={"req-a": {"id": "req-a", "status": "pending"}},
    )
    # Live parked row for the UNCHANGED sibling, bound to req-a.
    await _seed_parked_row(db, inbox_dir, request_id="req-a", filename="held.md")

    # Genuinely-new content arrives while the approval is pending.
    (inbox_dir / "arrived.md").write_text("brand new content")
    await monitor.check_once()

    disp = monitor._autonomous_dispatcher
    # The held approval is NEVER cancelled by new content.
    disp.approval_gate.approval_manager.cancel.assert_not_called()

    # Both files are parked on the SAME request — the sibling's original row
    # untouched, the new file's drop alongside it.
    from genesis.db.crud import inbox_items

    rows = [
        dict(r)
        for r in await (
            await db.execute(
                "SELECT file_path, status, error_message FROM inbox_items "
                "WHERE status='processing' AND error_message = ?",
                (f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-a",),
            )
        ).fetchall()
    ]
    parked = {Path(r["file_path"]).name for r in rows}
    assert parked == {"held.md", "arrived.md"}, parked

    # The sibling's original parked row is STILL the live one (not superseded —
    # its file never changed).
    old = await inbox_items.get_by_id(db, "row-req-a")
    assert old["status"] == "processing"
    assert (old["error_message"] or "").startswith("awaiting_approval:req-a")


@pytest.mark.asyncio
async def test_resume_pass_dispatches_on_pending_to_approved_transition(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """With a wired approval manager, the resume pass dispatches an
    awaiting row ONLY when the approval transitions from pending to
    approved.  While pending, it must not dispatch."""
    # Scan 1: wired dispatcher returns blocked-pending → row parked
    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
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

    rows1 = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows1) == 1
    assert rows1[0]["status"] == "processing"
    assert (rows1[0]["error_message"] or "").startswith("awaiting_approval:")

    # Scan 2: approval still pending.  Wire dispatcher to have a pending
    # site (pre-check will skip new detection) and approval_by_id to
    # return pending (resume pass will skip dispatch).  The test asserts
    # that NO dispatch happens this scan.
    pending_site_row = {
        "id": "req-transition-1",
        "status": "pending",
        "action_type": "autonomous_cli_fallback",
        "_context": {
            "subsystem": "inbox",
            "policy_id": "inbox_evaluation",
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
    rows2 = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md'",
                )
            ).fetchall()
        )
    ]
    assert len(rows2) == 1
    assert rows2[0]["id"] == rows1[0]["id"]
    assert rows2[0]["status"] == "processing"

    # Scan 3: approval now approved.  Resume pass should detect the
    # transition, dispatch the item, and CC should run to completion.
    approved_decision = AutonomousDispatchDecision(
        mode="cli_approved",
        reason="CLI fallback approved",
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
    rows3 = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%transition.md' "
                    "ORDER BY created_at",
                )
            ).fetchall()
        )
    ]
    assert len(rows3) == 1
    assert rows3[0]["id"] == rows1[0]["id"]
    assert rows3[0]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_payload",
    [
        "inbox-items-v2:not-json",
        b"inbox-items-v2:not-json",
    ],
)
async def test_resume_corrupt_v2_batch_rederives_without_full_file_replay(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    corrupt_payload,
):
    """Corrupt structured storage is not the legacy full-file fallback."""
    from genesis.db.crud import inbox_items

    path, _ = await _seed_parked_row(
        db,
        inbox_dir,
        request_id="req-corrupt",
        filename="corrupt.md",
        content="first item\n\nsecond item",
    )
    await db.execute(
        "UPDATE inbox_items SET batch_items = ?, retry_count = ? WHERE id = ?",
        (
            corrupt_payload,
            monitor._config.max_retries - 1,
            "row-req-corrupt",
        ),
    )
    await db.commit()

    new_pending = AutonomousDispatchDecision(
        mode="blocked",
        reason="fresh approval required",
        approval_request_id="req-fresh",
    )
    dispatcher = _make_wired_dispatcher(
        decision=new_pending,
        approval_by_id={
            "req-corrupt": {"id": "req-corrupt", "status": "approved"},
        },
    )
    dispatcher.approval_gate.mark_consumed = AsyncMock(return_value=True)
    monitor._autonomous_dispatcher = dispatcher

    result = await monitor.check_once()

    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_awaited()
    corrupt = await inbox_items.get_by_id(db, "row-req-corrupt")
    assert corrupt["status"] == "processing"
    assert corrupt["error_message"] == (
        f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-fresh"
    )
    assert corrupt["retry_count"] == monitor._config.max_retries - 1
    assert inbox_items.batch_items_for_dispatch(corrupt["batch_items"]) == "first item"
    dispatcher.approval_gate.mark_consumed.assert_awaited_once_with("req-corrupt")
    rows = await db.execute_fetchall(
        "SELECT id, status, error_message, batch_items FROM inbox_items "
        "WHERE file_path = ? ORDER BY rowid",
        (str(path),),
    )
    assert len(rows) == 2
    assert all(row["status"] == "processing" for row in rows)
    assert all(
        row["error_message"] == f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-fresh"
        for row in rows
    )
    assert [
        inbox_items.batch_items_for_dispatch(row["batch_items"])
        for row in rows
    ] == ["first item", "second item"]


@pytest.mark.asyncio
async def test_corrupt_parked_batch_retries_after_healthy_sibling_completes(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """A completed sibling must not strand a corrupt batch at the same hash."""
    from genesis.db.crud import inbox_items

    path, content_hash = await _seed_parked_row(
        db,
        inbox_dir,
        request_id="req-siblings",
        filename="corrupt-siblings.md",
        content="first item\n\nsecond item",
    )
    corrupt_id = "row-req-siblings"
    healthy_id = "row-req-siblings-healthy"
    await db.execute(
        "UPDATE inbox_items SET batch_items = ?, drop_id = ? WHERE id = ?",
        (
            f"{inbox_items.BATCH_ITEMS_V2_PREFIX}not-json",
            "drop-siblings",
            corrupt_id,
        ),
    )
    await inbox_items.create(
        db,
        id=healthy_id,
        file_path=str(path),
        content_hash=content_hash,
        status="processing",
        created_at="2026-03-10T11:00:01+00:00",
        drop_id="drop-siblings",
        batch_items=inbox_items.serialize_batch_items(["second item"]),
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-siblings",
    )
    await db.commit()

    approved = AutonomousDispatchDecision(
        mode="cli_approved",
        reason="CLI fallback approved",
        approval_request_id="req-siblings",
    )
    first_dispatcher = _make_wired_dispatcher(
        decision=approved,
        approval_by_id={
            "req-siblings": {"id": "req-siblings", "status": "approved"},
        },
    )
    first_dispatcher.approval_gate.mark_consumed = AsyncMock(return_value=True)
    monitor._autonomous_dispatcher = first_dispatcher
    mock_invoker.run.return_value = _success_output("healthy sibling evaluated")

    first = await monitor.check_once()

    assert first.batches_dispatched == 1
    mock_invoker.run.assert_awaited_once()
    assert "second item" in mock_invoker.run.await_args.args[0].prompt
    assert "first item" not in mock_invoker.run.await_args.args[0].prompt
    corrupt = await inbox_items.get_by_id(db, corrupt_id)
    healthy = await inbox_items.get_by_id(db, healthy_id)
    assert corrupt["status"] == "failed"
    assert corrupt["error_message"] == "batch_items_corrupt_restart"
    assert healthy["status"] == "completed"

    mock_invoker.run.reset_mock()
    fresh_pending = AutonomousDispatchDecision(
        mode="blocked",
        reason="fresh approval required",
        approval_request_id="req-siblings-fresh",
    )
    monitor._autonomous_dispatcher = _make_wired_dispatcher(
        decision=fresh_pending,
    )

    second = await monitor.check_once()

    assert second.items_retried == 1
    assert second.batches_dispatched == 0
    mock_invoker.run.assert_not_awaited()
    recovered = await inbox_items.get_by_id(db, corrupt_id)
    assert recovered["status"] == "processing"
    assert recovered["error_message"] == (
        f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-siblings-fresh"
    )
    assert inbox_items.batch_items_for_dispatch(recovered["batch_items"]) == "first item"


@pytest.mark.asyncio
async def test_resume_pass_marks_row_failed_on_rejected_transition(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """When the wired approval manager reports the approval as rejected,
    the resume pass must mark the inbox row failed (with a rejection
    message) and not dispatch anything."""
    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%will-be-rejected%'",
                )
            ).fetchall()
        )
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "reject" in (rows[0]["error_message"] or "").lower()


@pytest.mark.asyncio
async def test_resume_pass_invalidates_row_on_missing_approval(
    monitor,
    inbox_dir,
    mock_invoker,
    db,
):
    """When the approval row is missing entirely (approval_manager
    returns None), the inbox row must be invalidated with the
    APPROVAL_INVALIDATED_PREFIX marker so the next scan re-detects the
    file as new."""
    from genesis.db.crud import inbox_items

    pending_decision = AutonomousDispatchDecision(
        mode="blocked",
        reason="approval requested",
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

    rows = [
        dict(r)
        for r in (
            await (
                await db.execute(
                    "SELECT * FROM inbox_items WHERE file_path LIKE '%gone.md'",
                )
            ).fetchall()
        )
    ]
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

    evaluation = (
        "# Inbox Evaluation\n\n**Classification:** Technology\n\nThis article from example.com "
        + "x" * 300
    )
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

    evaluation = (
        "# Inbox Evaluation\n\n**Classification:** Technology\n\nA LinkedIn post by Hao Hoang "
        + "x" * 300
    )
    source = "Check out https://www.linkedin.com/posts/some-post"
    assert _passes_coherence_check(evaluation, source) is True


def test_coherence_check_passes_with_domain_stem_fallback():
    """Unknown domains still match via stem extraction (e.g. 'langchain' from 'langchain.com')."""
    from genesis.inbox.monitor import _passes_coherence_check

    evaluation = (
        "# Inbox Evaluation\n\n**Classification:** Technology\n\nLangChain's new feature "
        + "x" * 300
    )
    source = "https://www.langchain.com/blog/something"
    assert _passes_coherence_check(evaluation, source) is True


# ── Baseline guard tests ──────────────────────────────────────────────


class TestMergeEvaluatedContent:
    """Unit tests for _merge_evaluated_content."""

    def test_empty_prev_returns_source_lines(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content(None, "alpha\nbeta\n")
        lines = result.splitlines()
        assert "alpha" in lines
        assert "beta" in lines

    def test_empty_source_returns_prev_lines(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content("alpha\nbeta\n", "")
        lines = result.splitlines()
        assert "alpha" in lines
        assert "beta" in lines

    def test_union_of_both(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content("alpha\nbeta", "beta\ngamma")
        lines = set(result.splitlines())
        assert lines == {"alpha", "beta", "gamma"}

    def test_deduplicates_stripped(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content("  alpha  \nbeta", "alpha\n  beta  ")
        lines = result.splitlines()
        assert lines.count("alpha") == 1
        assert lines.count("beta") == 1

    def test_blank_lines_excluded(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content("\n\nalpha\n\n", "\n\nbeta\n\n")
        assert "" not in result.splitlines()

    def test_sorted_output(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content("gamma\nalpha", "beta")
        assert result.splitlines() == ["alpha", "beta", "gamma"]

    def test_both_none_and_empty(self):
        from genesis.inbox.monitor import _merge_evaluated_content

        result = _merge_evaluated_content(None, "")
        assert result == ""


@pytest.mark.asyncio
async def test_baseline_guard_survives_file_clear(
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """evaluated_content preserves detection-time content even if the file
    is cleared during evaluation (the race condition that caused Genesis-19
    through Genesis-26 to re-evaluate the same items)."""
    from genesis.db.crud import inbox_items

    clock = _FakeClock()
    # items_per_eval=3 pins this test's original single-batch scenario (the
    # default is now 1); the guard under test is baseline-vs-file-clear, not
    # batch grouping.
    config = InboxConfig(watch_path=inbox_dir, batch_size=1, items_per_eval=3)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )

    original_content = "Numenta\n\nhttps://example.com/article-1\n\nhttps://example.com/article-2\n"

    # One canned response serves BOTH evaluations in this test, so it must
    # cover the second drop's URL (article-3) as well.
    mock_invoker.run.return_value = _success_output(
        "# Inbox Evaluation\n\n## 1. Numenta\nGood stuff.\n\n"
        "## 2. example.com/article-1\nInteresting.\n\n"
        "## 3. example.com/article-2\nNoted.\n\n"
        "## 4. example.com/article-3\nAlso noted.\n" + "x" * 300
    )

    # Write file and do first evaluation
    (inbox_dir / "Genesis.md").write_text(original_content)
    result = await mon.check_once()
    assert result.batches_dispatched == 1

    # Verify evaluated_content was stored
    row = await inbox_items.get_by_file_path(
        db,
        str(inbox_dir / "Genesis.md"),
    )
    assert row is not None
    stored = row["evaluated_content"]
    assert stored is not None
    # All original lines should be in the merged baseline
    assert "Numenta" in stored
    assert "https://example.com/article-1" in stored
    assert "https://example.com/article-2" in stored

    # Simulate: file is cleared by sync during NEXT evaluation
    # First, advance clock past cooldown
    clock.now += timedelta(hours=2)

    # Write file with only new content (simulates user clearing old items)
    (inbox_dir / "Genesis.md").write_text("https://example.com/article-3\n")

    result2 = await mon.check_once()
    assert result2.batches_dispatched == 1

    # Verify: evaluated_content now has BOTH old and new lines
    row2 = await inbox_items.get_by_file_path(
        db,
        str(inbox_dir / "Genesis.md"),
    )
    stored2 = row2["evaluated_content"]
    assert "Numenta" in stored2, "Old content lost from baseline"
    assert "https://example.com/article-1" in stored2, "Old URL lost"
    assert "https://example.com/article-2" in stored2, "Old URL lost"
    assert "https://example.com/article-3" in stored2, "New URL missing"


@pytest.mark.asyncio
async def test_baseline_guard_delta_only_new_items(
    db,
    mock_invoker,
    mock_session_manager,
    inbox_dir,
    tmp_path,
):
    """After a successful evaluation, adding new items to the file should
    produce a delta containing ONLY the new items, not previously evaluated ones."""
    from genesis.db.crud import inbox_items
    from genesis.inbox.monitor import _compute_new_content

    clock = _FakeClock()
    config = InboxConfig(watch_path=inbox_dir, batch_size=1)
    writer = ResponseWriter(watch_path=inbox_dir, timezone="UTC")
    mon = InboxMonitor(
        db=db,
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        config=config,
        writer=writer,
        clock=clock,
    )

    mock_invoker.run.return_value = _success_output(
        "# Eval\n\n## 1. article-1\nGood.\n\n## 2. article-2\nNoted.\n" + "x" * 300
    )

    # Initial evaluation
    (inbox_dir / "test.md").write_text(
        "https://example.com/article-1\nhttps://example.com/article-2\n"
    )
    await mon.check_once()

    # Get the stored baseline
    prev = await inbox_items.get_evaluated_content(
        db,
        str(inbox_dir / "test.md"),
    )
    assert prev is not None

    # Add a new URL
    new_content = (
        "https://example.com/article-1\n"
        "https://example.com/article-2\n"
        "https://example.com/article-3\n"
    )

    delta = _compute_new_content(prev, new_content)
    assert "article-3" in delta
    assert "article-1" not in delta
    assert "article-2" not in delta
