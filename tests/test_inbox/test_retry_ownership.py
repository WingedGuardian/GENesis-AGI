"""Retry budgets belong to saved logical batches, not their file position."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import inbox_items
from genesis.db.schema import TABLES
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.types import InboxConfig


@pytest.fixture
async def system(tmp_path):
    async with aiosqlite.connect(tmp_path / "fixture.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(TABLES["inbox_items"])
        monitor = InboxMonitor(
            db=db, invoker=AsyncMock(), session_manager=AsyncMock(),
            config=InboxConfig(watch_path=tmp_path, items_per_eval=1, max_retries=3),
        )
        yield db, monitor, tmp_path / "fixture.db"


async def seed(db, row_id, texts, retries=2):
    await inbox_items.create(
        db, id=row_id, file_path="source.md", content_hash="old-hash",
        status="failed", created_at="2026-01-01T00:00:00+00:00",
        batch_items=inbox_items.serialize_batch_items(texts), retry_count=retries,
        error_message="synthetic transient failure",
    )


@pytest.mark.asyncio
async def test_inserted_item_cannot_inherit_failed_items_budget(system):
    db, monitor, _ = system
    c, d = "https://example.com/C", "https://example.com/D"
    await seed(db, "old-C", [c])
    pending = []
    await monitor._queue_drop(
        "source.md", f"{d}\n\n{c}", "new-hash", datetime.now(UTC).isoformat(), pending, held_ids=[],
    )
    old = await inbox_items.get_by_id(db, "old-C")
    assert inbox_items.stored_item_texts(old["batch_items"]) == [c]
    assert old["retry_count"] == 2
    assert [item.content for item in pending] == [d, c]
    assert pending[1].id == "old-C"
    added = await inbox_items.get_by_id(db, pending[0].id)
    assert added["retry_count"] == 0
    await inbox_items.update_status(db, added["id"], status="failed")
    assert (await inbox_items.get_by_id(db, added["id"]))["retry_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_successor", [False, True])
@pytest.mark.parametrize("history_storage", ["structured", "missing", "ambiguous"])
async def test_unmatched_history_becoming_held_discards_drop(
    system, monkeypatch, exact_successor, history_storage,
):
    db, monitor, db_path = system
    c, d = "https://example.com/C", "https://example.com/D"
    await seed(db, "old-CD", [c, d])
    if history_storage != "structured":
        await db.execute(
            "UPDATE inbox_items SET batch_items=? WHERE id='old-CD'",
            (None if history_storage == "missing" else f"{c}\n{d}",),
        )
        await db.commit()
    if exact_successor:
        await seed(db, "new-C", [c], retries=1)
    select = inbox_items.get_retriable_failed_rows
    winner = None

    async def selected_then_held(*args, **kwargs):
        nonlocal winner
        rows = await select(*args, **kwargs)
        async with aiosqlite.connect(db_path) as other:
            await other.execute(
                "UPDATE inbox_items SET error_message='replay_unsafe: synthetic', batch_items=? "
                "WHERE id='old-CD'",
                (inbox_items.serialize_batch_items([c, d]),),
            )
            await other.commit()
        winner = await inbox_items.get_by_id(db, "old-CD")
        return rows

    monkeypatch.setattr(inbox_items, "get_retriable_failed_rows", selected_then_held)
    pending = []
    await monitor._queue_drop(
        "source.md", c, "new-hash", datetime.now(UTC).isoformat(), pending, held_ids=[],
    )
    assert pending == []
    assert await inbox_items.get_by_id(db, "old-CD") == winner
    cursor = await db.execute("SELECT COUNT(*) FROM inbox_items WHERE status='pending'")
    assert (await cursor.fetchone())[0] == 0


A = "https://example.com/A"
B = "https://example.com/B"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,source,size,owners", [
    ([[A], [B]], [B, A], 1, ["old-1", "old-0"]),
    ([[A], [B]], [B], 1, ["old-1"]),
    ([[A, B]], [A, B], 1, [None, None]),
    ([[A], [B]], [A, B], 2, [None]),
    ([[A, B]], [B, A], 2, [None]),
    ([[A, B]], [A, B], 2, ["old-0"]),
    ([[A], [A]], [A, A], 1, ["old-0"]),
    ([["  " + A + "  "]], [A], 1, ["old-0"]),
    ([["First intent\n" + A]], ["Second intent\n" + A], 1, [None]),
    ([["First intent\n" + A]], ["First intent\n" + A], 1, ["old-0"]),
    ([[A + "?utm_source=fixture"]], [A], 1, ["old-0"]),
    ([A], [A], 1, ["old-0"]),
    ([A + "\n" + B], [A, B], 1, [None, None]),
    ([None], [A], 1, [None]),
    ([""], [A], 1, [None]),
    (["inbox-items-v2:not-json"], [A], 1, [None]),
    ([], [A], 1, [None]),
    ([[A]], [], 1, []),
], ids=[
    "reorder", "remove", "split", "combine", "reorder-within-batch",
    "same-batch", "duplicate", "whitespace", "changed-annotation", "same-annotation",
    "tracking", "legacy-single",
    "legacy-ambiguous", "missing", "empty", "corrupt", "first-run", "empty-source",
])
async def test_batch_identity_controls_retry_ownership(system, stored, source, size, owners):
    db, monitor, _ = system
    monitor._config = replace(monitor.config, items_per_eval=size)
    originals = {}
    for index, payload in enumerate(stored):
        row_id = f"old-{index}"
        await inbox_items.create(
            db, id=row_id, file_path="source.md", content_hash="old-hash",
            status="failed", created_at=f"2026-01-0{index + 1}T00:00:00+00:00",
            retry_count=2 - index, error_message="synthetic failure",
            batch_items=(inbox_items.serialize_batch_items(payload)
                         if isinstance(payload, list) else payload),
        )
        originals[row_id] = await inbox_items.get_by_id(db, row_id)
    pending = []
    await monitor._queue_drop(
        "source.md", "\n\n".join(source), "new-hash", datetime.now(UTC).isoformat(), pending, held_ids=[],
    )
    assert len(pending) == len(owners)
    for item, owner in zip(pending, owners, strict=True):
        row = await inbox_items.get_by_id(db, item.id)
        if owner is None:
            assert item.id not in originals
            assert row["retry_count"] == 0
        else:
            assert item.id == owner
            assert row["retry_count"] == originals[owner]["retry_count"]
    for row_id, before in originals.items():
        if row_id not in owners:
            assert await inbox_items.get_by_id(db, row_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["insert", "reuse"])
@pytest.mark.parametrize("change", [
    "eligible", "null_error", "lookalike", "missing", "held", "invalidated",
    "at_cap", "completed", "pending", "claimed", "reparked",
])
async def test_queue_writers_recheck_selected_history(system, writer, change):
    db, _, db_path = system
    history_id = "history-'\"-雪"
    await seed(db, history_id, [A, B])
    await seed(db, "target", [A])
    target_before = await inbox_items.get_by_id(db, "target")
    async with aiosqlite.connect(db_path) as other:
        if change == "missing":
            await other.execute("DELETE FROM inbox_items WHERE id=?", (history_id,))
        elif change == "at_cap":
            await other.execute("UPDATE inbox_items SET retry_count=3 WHERE id=?", (history_id,))
        elif change in {"pending", "completed"}:
            await other.execute("UPDATE inbox_items SET status=? WHERE id=?", (change, history_id))
        else:
            status, error = {
                "eligible": ("failed", "ordinary"),
                "null_error": ("failed", None),
                "lookalike": ("failed", "replayXunsafe: ordinary"),
                "held": ("failed", "replay_unsafe: held"),
                "invalidated": ("failed", "approval_invalidated: old grant"),
                "claimed": ("processing", "dispatching:fixture"),
                "reparked": ("processing", "awaiting_approval:new"),
            }[change]
            await other.execute(
                "UPDATE inbox_items SET status=?, error_message=? WHERE id=?",
                (status, error, history_id),
            )
        await other.commit()
    before = [dict(row) for row in await db.execute_fetchall("SELECT * FROM inbox_items ORDER BY id")]
    kwargs = dict(
        drop_id="new", batch_items=inbox_items.serialize_batch_items([A]),
        content_hash="new", created_at="new-time", max_retries=3,
        expected_retry_ids=[history_id, history_id],
    )
    if writer == "insert":
        changed = await inbox_items.create_pending_if_retriable(
            db, "fresh", file_path="source.md", **kwargs,
        )
    else:
        changed = await inbox_items.reuse_as_pending(
            db, "target", expected_batch_items=target_before["batch_items"], **kwargs,
        )
    allowed = change in {"eligible", "null_error", "lookalike"}
    assert changed is allowed
    after = [dict(row) for row in await db.execute_fetchall("SELECT * FROM inbox_items ORDER BY id")]
    if not allowed:
        assert after == before
    else:
        target = await inbox_items.get_by_id(db, "fresh" if writer == "insert" else "target")
        assert target["status"] == "pending"
        assert target["retry_count"] == (0 if writer == "insert" else 2)
        assert target["content_hash"] == "new"
        assert target["created_at"] == "new-time"
        assert await inbox_items.get_by_id(db, history_id) == next(
            row for row in before if row["id"] == history_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "missing", "held", "at_cap"])
async def test_rearm_rechecks_own_snapshot_even_without_selected_ids(system, change):
    db, _, db_path = system
    await seed(db, "target", [A])
    original = await inbox_items.get_by_id(db, "target")
    async with aiosqlite.connect(db_path) as other:
        if change == "missing":
            await other.execute("DELETE FROM inbox_items WHERE id='target'")
        elif change == "identity":
            await other.execute("UPDATE inbox_items SET batch_items=? WHERE id='target'",
                                (inbox_items.serialize_batch_items([B]),))
        elif change == "held":
            await other.execute("UPDATE inbox_items SET error_message='replay_unsafe: held' WHERE id='target'")
        else:
            await other.execute("UPDATE inbox_items SET retry_count=3 WHERE id='target'")
        await other.commit()
    winner = await inbox_items.get_by_id(db, "target")
    assert not await inbox_items.reuse_as_pending(
        db, "target", drop_id="new", batch_items=original["batch_items"], content_hash="new",
        created_at="new-time", expected_batch_items=original["batch_items"],
        expected_retry_ids=[], max_retries=3,
    )
    assert await inbox_items.get_by_id(db, "target") == winner


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["retry", "modified"])
@pytest.mark.parametrize("first", ["reused", "inserted"])
async def test_hold_after_delta_snapshot_discards_drop(system, monkeypatch, lane, first):
    """A hold written between the delta read and selection is never armed.

    The held row is already absent from retry selection, so only the hold set
    read before the snapshot can tell either arming write that it went stale.
    """
    db, monitor, db_path = system
    c, d = "https://example.com/C", "https://example.com/D"
    source = db_path.parent / "inbox-source.md"
    source.write_text(f"{c}\n\n{d}" if first == "reused" else f"{d}\n\n{c}")
    for row_id, text in (("old-C", c), ("old-D", d)):
        await inbox_items.create(
            db, id=row_id, file_path=str(source), content_hash="old-hash",
            status="failed", created_at="2026-01-01T00:00:00+00:00",
            batch_items=inbox_items.serialize_batch_items([text]), retry_count=1,
            error_message="synthetic transient failure",
        )
    snapshot = inbox_items.get_handled_batch_content

    async def snapshot_then_held(*args, **kwargs):
        handled = await snapshot(*args, **kwargs)
        async with aiosqlite.connect(db_path) as other:
            await other.execute(
                "UPDATE inbox_items SET error_message='replay_unsafe: synthetic' WHERE id='old-D'",
            )
            await other.commit()
        return handled

    monkeypatch.setattr(inbox_items, "get_handled_batch_content", snapshot_then_held)
    files = {"modified": [source] if lane == "modified" else [],
             "retry": [source] if lane == "retry" else []}
    now = datetime.now(UTC)
    pending, _ = await monitor._phase_create_records(
        [], files["modified"], files["retry"], now, now.isoformat(),
    )
    assert pending == []
    cursor = await db.execute("SELECT COUNT(*) FROM inbox_items WHERE status='pending'")
    assert (await cursor.fetchone())[0] == 0
    held = await inbox_items.get_by_id(db, "old-D")
    assert held["status"] == "failed"
    assert held["error_message"] == "replay_unsafe: synthetic"
