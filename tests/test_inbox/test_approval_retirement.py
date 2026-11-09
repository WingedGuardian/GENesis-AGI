"""Approval retirement must finish before inbox work can use the site key again."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
from genesis.autonomy.cli_policy import AutonomousCliPolicy
from genesis.autonomy.dispatch_router import AutonomousDispatchRouter
from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables
from genesis.inbox import monitor as monitor_module
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.scanner import compute_hash
from genesis.inbox.types import InboxConfig
from genesis.inbox.writer import ResponseWriter


@pytest.fixture
async def system(tmp_path):
    """Real approval/dispatch state; external delivery and evaluator are inert."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        manager = ApprovalManager(db=db)

        def policy():
            return AutonomousCliPolicy(manual_approval_required=True)

        gate = AutonomousCliApprovalGate(
            runtime=SimpleNamespace(), approval_manager=manager, policy_loader=policy,
        )
        gate._send_request = AsyncMock()
        status, request_id, _ = await gate.ensure_approval(
            subsystem="inbox", policy_id="inbox_evaluation",
            action_label="inbox evaluation", invocation=None, extra_context=None,
        )
        assert status == "pending"
        assert await manager.resolve(
            request_id, status="approved", resolved_by="test:fixture",
        )
        dispatcher = AutonomousDispatchRouter(
            router=None, approval_gate=gate, policy_loader=policy,
        )
        dispatcher.route = AsyncMock(wraps=dispatcher.route)
        monitor = InboxMonitor(
            db=db, invoker=AsyncMock(), session_manager=AsyncMock(),
            config=InboxConfig(watch_path=watch),
            writer=ResponseWriter(watch_path=watch, timezone="UTC"),
        )
        monitor.set_autonomous_dispatcher(dispatcher)
        monitor._dispatch_one_batch = AsyncMock(return_value=True)
        yield SimpleNamespace(
            db=db, watch=watch, manager=manager, gate=gate,
            dispatcher=dispatcher, monitor=monitor, request_id=request_id,
        )


async def _park(system, *, row_id="corrupt", corrupt=True, retries=5):
    path = system.watch / f"{row_id}.md"
    path.write_text(f"{row_id} original item")
    await inbox_items.create(
        system.db, id=row_id, file_path=str(path), content_hash=compute_hash(path),
        status="processing", created_at=datetime.now(UTC).isoformat(),
        retry_count=retries, drop_id=f"drop-{row_id}",
        batch_items=(
            "inbox-items-v2:not-json" if corrupt
            else inbox_items.serialize_batch_items([path.read_text()])
        ),
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}{system.request_id}",
    )
    return path, await inbox_items.get_by_id(system.db, row_id)


def _fail_consumption(system, monkeypatch, *, attempts=2):
    real_consume = system.gate.mark_consumed
    remaining = attempts

    async def consume(request_id):
        nonlocal remaining
        if remaining:
            remaining -= 1
            raise sqlite3.OperationalError("synthetic consume failure")
        return await real_consume(request_id)

    mocked = AsyncMock(side_effect=consume)
    monkeypatch.setattr(system.gate, "mark_consumed", mocked)
    return mocked


async def _add_site_work(system, kind):
    """A different file can reuse exactly the same stable approval key."""
    path = system.watch / "other.md"
    if kind == "new":
        path.write_text("another new item")
        return
    content = "already evaluated\n\nanother retry item"
    path.write_text(content)
    await inbox_items.create(
        system.db, id="other-completed", file_path=str(path),
        content_hash=compute_hash(path), status="completed",
        created_at=datetime.now(UTC).isoformat(),
    )
    await inbox_items.update_status(
        system.db, "other-completed", status="completed",
        evaluated_content="already evaluated",
    )
    if kind == "modified":
        path.write_text(content + "\n\na newly appended item")
    else:
        assert kind == "retry"
        await inbox_items.create(
            system.db, id="other-retry", file_path=str(path),
            content_hash=compute_hash(path), status="failed",
            created_at=datetime.now(UTC).isoformat(), retry_count=0,
            error_message="ordinary evaluation failure",
            batch_items=inbox_items.serialize_batch_items(["another retry item"]),
        )
        assert str(path) in await inbox_items.get_retriable_failure_files(system.db)


async def _rows(system):
    return [dict(row) for row in await system.db.execute_fetchall(
        "SELECT * FROM inbox_items ORDER BY id",
    )]


async def _fresh_request(system):
    requests = await system.manager.get_recent()
    assert len(requests) == 2
    old = await system.manager.get_by_id(system.request_id)
    assert old["consumed_at"]
    fresh = next(row for row in requests if row["id"] != system.request_id)
    assert fresh["status"] == "pending"
    return fresh["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "old_retries"), [(1, 0), (1, 3), (3, 0), (3, 5)])
async def test_corrupt_retirement_holds_then_requires_fresh_approval(
    system, monkeypatch, cap, old_retries,
):
    from dataclasses import replace

    system.monitor._config = replace(system.monitor.config, max_retries=cap)
    _, original = await _park(system, retries=old_retries)
    consume = _fail_consumption(system, monkeypatch)

    first = await system.monitor.check_once()

    assert first.batches_dispatched == 0
    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    assert consume.await_count == 2
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_not_awaited()
    assert len(await system.manager.get_recent()) == 1
    assert not (await system.manager.get_by_id(system.request_id))["consumed_at"]

    await system.monitor.check_once()

    fresh_id = await _fresh_request(system)
    recovered = await inbox_items.get_by_id(system.db, "corrupt")
    assert recovered["retry_count"] == min(old_retries, cap - 1)
    assert recovered["error_message"] == f"awaiting_approval:{fresh_id}"
    assert inbox_items.batch_items_for_dispatch(recovered["batch_items"]) == "corrupt original item"
    system.monitor._dispatch_one_batch.assert_not_awaited()
    assert await system.manager.resolve(fresh_id, status="approved", resolved_by="test:fixture")

    await system.monitor.check_once()

    system.monitor._dispatch_one_batch.assert_awaited_once()
    item = system.monitor._dispatch_one_batch.await_args.args[0]
    assert item.approval_reqid == fresh_id
    assert item.content == "corrupt original item"
    system.monitor._invoker.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["new", "modified", "retry"])
@pytest.mark.parametrize("uncertain", ["consume", "lookup"])
async def test_uncertain_retirement_defers_all_new_site_work(
    system, monkeypatch, kind, uncertain,
):
    await _park(system)
    await _add_site_work(system, kind)
    original_rows = await _rows(system)
    if uncertain == "consume":
        _fail_consumption(system, monkeypatch)
    else:
        monkeypatch.setattr(
            system.manager, "get_by_id",
            AsyncMock(side_effect=sqlite3.OperationalError("synthetic lookup failure")),
        )

    result = await system.monitor.check_once()

    assert result.batches_dispatched == 0
    assert await _rows(system) == original_rows
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_not_awaited()
    assert len(await system.manager.get_recent()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["corrupt", "changed", "hash_missing"])
async def test_retirement_hold_survives_later_source_changes(
    system, monkeypatch, exit_kind,
):
    path, original = await _park(system)
    await _add_site_work(system, "new")
    consume = _fail_consumption(system, monkeypatch, attempts=4)
    if exit_kind == "changed":
        path.write_text("changed before first scan")
    elif exit_kind == "hash_missing":
        path.unlink()

    await system.monitor.check_once()

    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    assert consume.await_count == 2
    system.dispatcher.route.assert_not_awaited()
    path.write_text("edited while retirement was held")

    await system.monitor.check_once()

    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    assert consume.await_count == 4
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_not_awaited()

    await system.monitor.check_once()

    await _fresh_request(system)
    system.monitor._dispatch_one_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_disappearing_during_read_retires_grant_before_invalidation(system, monkeypatch):
    path, _ = await _park(system, corrupt=False)
    real_read = monitor_module.read_content
    calls = []

    def vanish_during_read(candidate):
        if candidate == path:
            calls.append(candidate)
            path.unlink()
        return real_read(candidate)

    monkeypatch.setattr(monitor_module, "read_content", vanish_during_read)

    await system.monitor.check_once()

    assert calls == [path], "the read-time disappearance must actually execute"
    old = await system.manager.get_by_id(system.request_id)
    assert old["consumed_at"]
    row = await inbox_items.get_by_id(system.db, "corrupt")
    assert row["error_message"] == f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}source file vanished"
    system.monitor._dispatch_one_batch.assert_not_awaited()
    monkeypatch.setattr(monitor_module, "read_content", real_read)
    path.write_text("reappeared source with new content")

    await system.monitor.check_once()

    await _fresh_request(system)
    system.monitor._dispatch_one_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_retirement_allows_only_already_approved_sibling(
    system, monkeypatch,
):
    _, original = await _park(system)
    await _park(system, row_id="healthy", corrupt=False, retries=0)
    await _add_site_work(system, "new")
    consume = _fail_consumption(system, monkeypatch)

    result = await system.monitor.check_once()

    assert result.batches_dispatched == 1
    assert consume.await_count == 3
    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_awaited_once()
    item = system.monitor._dispatch_one_batch.await_args.args[0]
    assert item.id == "healthy"
    assert item.approval_reqid == system.request_id
    assert len(await system.manager.get_recent()) == 1


@pytest.mark.asyncio
async def test_valid_resume_also_keeps_marker_when_retirement_fails(system, monkeypatch):
    _, original = await _park(system, corrupt=False, retries=0)
    await _add_site_work(system, "new")
    _fail_consumption(system, monkeypatch)

    result = await system.monitor.check_once()

    assert result.batches_dispatched == 0
    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_not_awaited()
    assert len(await system.manager.get_recent()) == 1


@pytest.mark.asyncio
async def test_cancelled_retirement_preserves_awaiting_row(system, monkeypatch):
    _, original = await _park(system)
    monkeypatch.setattr(
        system.gate, "mark_consumed", AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await system.monitor.check_once()

    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    assert not (await system.manager.get_by_id(system.request_id))["consumed_at"]
    system.dispatcher.route.assert_not_awaited()
    system.monitor._dispatch_one_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_consumption_survives_row_write_failure(system, monkeypatch):
    _, original = await _park(system)
    real_fail = inbox_items.mark_failed_keeping_retries

    async def fail_write(db, row_id, **kwargs):
        if row_id == "corrupt":
            raise sqlite3.OperationalError("synthetic row write failure")
        return await real_fail(db, row_id, **kwargs)

    monkeypatch.setattr(inbox_items, "mark_failed_keeping_retries", fail_write)
    with pytest.raises(sqlite3.OperationalError, match="synthetic row write failure"):
        await system.monitor.check_once()

    assert await inbox_items.get_by_id(system.db, "corrupt") == original
    assert (await system.manager.get_by_id(system.request_id))["consumed_at"]
    system.monitor._dispatch_one_batch.assert_not_awaited()
    monkeypatch.setattr(inbox_items, "mark_failed_keeping_retries", real_fail)

    await system.monitor.check_once()

    await _fresh_request(system)
    system.monitor._dispatch_one_batch.assert_not_awaited()
