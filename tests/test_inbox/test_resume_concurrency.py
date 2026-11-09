"""A resume snapshot must not overwrite another connection's newer work."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import inbox_items
from genesis.db.schema import TABLES
from genesis.inbox import monitor as monitor_module
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.scanner import compute_hash
from genesis.inbox.types import InboxConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [
    "rejected", "expired", "cancelled", "missing", "changed", "vanished",
    "vanished_on_read", "corrupt",
])
@pytest.mark.parametrize("winner", [
    None, "completed", "completed_same_marker", "claimed", "superseded", "reparked",
])
async def test_resume_preserves_newer_row(tmp_path, monkeypatch, transition, winner):
    source = tmp_path / "item.md"
    source.write_text("https://example.com/original")
    db_path = tmp_path / "fixture.db"
    now = datetime.now(UTC)
    marker = inbox_items.AWAITING_APPROVAL_PREFIX + "original-approval"
    async with aiosqlite.connect(db_path) as db, aiosqlite.connect(db_path) as other:
        db.row_factory = aiosqlite.Row
        await db.execute(TABLES["inbox_items"])
        await inbox_items.create(
            db, id="item", file_path=str(source), content_hash=compute_hash(source),
            status="processing", created_at=now.isoformat(), retry_count=5,
            error_message=marker,
            batch_items=("inbox-items-v2:broken" if transition == "corrupt"
                         else inbox_items.serialize_batch_items([source.read_text()])),
        )
        winner_row = None

        async def lookup(request_id):
            nonlocal winner_row
            assert request_id == "original-approval"
            if winner:
                state, error = {
                    "completed": ("completed", None),
                    "completed_same_marker": ("completed", marker),
                    "claimed": ("processing", "dispatching:original-approval"),
                    "superseded": ("superseded", "replaced"),
                    "reparked": ("processing", "awaiting_approval:new-approval"),
                }[winner]
                await other.execute(
                    "UPDATE inbox_items SET status=?, error_message=?, "
                    "processed_at='winner-time', retry_count=7 WHERE id='item'",
                    (state, error),
                )
                await other.commit()
                winner_row = await inbox_items.get_by_id(db, "item")
            if transition == "changed":
                source.write_text("https://example.com/replacement")
            elif transition == "vanished":
                source.unlink()
            if transition == "missing":
                return None
            return {"status": transition if transition in {
                "rejected", "expired", "cancelled",
            } else "approved"}

        if transition == "vanished_on_read":
            def vanish(_path):
                raise FileNotFoundError("synthetic read race")
            monkeypatch.setattr(monitor_module, "read_content", vanish)
        mon = InboxMonitor(
            db=db, invoker=AsyncMock(), session_manager=AsyncMock(),
            config=InboxConfig(watch_path=tmp_path, max_retries=3),
        )
        # Set directly: the public setter also schedules a live scan callback.
        mon._autonomous_dispatcher = SimpleNamespace(approval_gate=SimpleNamespace(
            approval_manager=SimpleNamespace(get_by_id=lookup),
        ))
        mon._consume_approval = AsyncMock(return_value="consumed")
        resumed, ids, paths, deferred = await mon._phase_resume(now, now.isoformat())
        after = await inbox_items.get_by_id(db, "item")
        assert (resumed, ids, paths, deferred) == ([], set(), set(), False)
        if winner:
            assert after == winner_row
        else:
            assert after["status"] == ("superseded" if transition == "changed" else "failed")
            expected_retries = (
                3 if transition == "rejected" else
                2 if transition in {"expired", "cancelled", "missing", "corrupt"} else
                5 if transition == "changed" else 6
            )
            assert after["retry_count"] == expected_retries
        # Grant retirement deliberately precedes the row CAS, even if we lose it.
        assert mon._consume_approval.await_count == int(transition in {
            "changed", "vanished", "vanished_on_read", "corrupt",
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    "increment", "explicit", "baseline", "preserve_baseline", "keep", "clamp",
])
@pytest.mark.parametrize("mode", ["ordinary", "parked", "lost", "completed", "absent"])
async def test_conditional_update_contract(tmp_path, operation, mode):
    """Pin ordinary callers, conditional results, and every SQL assignment variant."""
    async with aiosqlite.connect(tmp_path / "helper.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(TABLES["inbox_items"])
        marker = "awaiting_approval:original"
        if mode != "absent":
            await inbox_items.create(
                db, id="item", file_path="synthetic.md", content_hash="fixture",
                status="completed" if mode == "completed" else "processing",
                created_at="fixture-time", retry_count=5,
                error_message=(marker if mode in {"parked", "completed"}
                               else "dispatching:original"),
            )
            await db.execute("UPDATE inbox_items SET evaluated_content='old baseline'")
            await db.commit()
        before = await inbox_items.get_by_id(db, "item")
        kwargs = {} if mode == "ordinary" else {"expected_approval_marker": marker}
        if operation in {"keep", "clamp"}:
            changed = await inbox_items.mark_failed_keeping_retries(
                db, "item", error_message="fixture transition",
                retriable_below=3 if operation == "clamp" else None, **kwargs,
            )
        else:
            changed = await inbox_items.update_status(
                db, "item", error_message="fixture transition",
                status="completed" if operation in {"baseline", "preserve_baseline"} else "failed",
                retry_count=1 if operation == "explicit" else None,
                evaluated_content="new baseline" if operation == "baseline" else None,
                **kwargs,
            )
        after = await inbox_items.get_by_id(db, "item")
        assert changed is (mode in {"ordinary", "parked"})
        if mode not in {"ordinary", "parked"}:
            assert after == before
        else:
            assert after["status"] == (
                "completed" if operation in {"baseline", "preserve_baseline"} else "failed"
            )
            assert after["retry_count"] == {"increment": 6, "explicit": 1, "clamp": 2}.get(operation, 5)
            assert after["evaluated_content"] == (
                "new baseline" if operation == "baseline" else "old baseline"
            )
            assert after["error_message"] == "fixture transition"
