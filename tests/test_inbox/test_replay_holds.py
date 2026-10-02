"""A replay hold survives scanner retries and cap/configuration changes."""

import aiosqlite
import pytest

from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def held(db, *, storage=None, error="replay_unsafe: after tools"):
    await inbox_items.create(
        db, id="held", file_path="/inbox/a.md", content_hash="h",
        status="failed", created_at="2026-10-02T00:00:00+00:00",
        error_message=error, retry_count=1,
        batch_items=(inbox_items.serialize_batch_items(["https://example.com/a"])
                     if storage is None else storage),
    )


@pytest.mark.asyncio
async def test_hold_excluded_from_all_retry_readers_even_with_raised_cap(db):
    await held(db)
    assert await inbox_items.get_retriable_failed(db, "/inbox/a.md", max_retries=100) is None
    assert await inbox_items.get_retriable_failed_rows(db, "/inbox/a.md", max_retries=100) == []
    assert await inbox_items.get_retriable_failure_files(db, max_retries=100) == []
    assert await inbox_items.get_all_known(db, max_retries=100) == {"/inbox/a.md": "h"}
    assert await inbox_items.get_handled_batch_content(db, "/inbox/a.md", max_retries=100) == [
        "https://example.com/a"
    ]
    assert await inbox_items.get_evaluated_content(db, "/inbox/a.md") is None


@pytest.mark.asyncio
async def test_abandonment_and_cached_reuse_cannot_erase_hold(db):
    await held(db)
    assert await inbox_items.mark_file_failures_abandoned(db, "/inbox/a.md", max_retries=100) == 0
    assert not await inbox_items.reuse_as_pending(
        db, "held", drop_id="new", batch_items="replacement", content_hash="new",
        created_at="2026-10-02T01:00:00+00:00",
    )
    row = await inbox_items.get_by_id(db, "held")
    assert row["error_message"] == "replay_unsafe: after tools"
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_hold_prefix_is_literal(db):
    await held(db, error="replayXunsafe: ordinary failure")
    assert await inbox_items.get_retriable_failure_files(db, max_retries=100) == ["/inbox/a.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", [
    "inbox-items-v2:{broken", "legacy\nambiguous", "",
    "inbox-items-v2:" + "[" * 2000 + "]" * 2000,
])
async def test_opaque_hold_requires_file_level_block(db, storage):
    await held(db, storage=storage)
    assert await inbox_items.get_opaque_replay_hold_files(db) == ["/inbox/a.md"]


@pytest.mark.asyncio
async def test_release_is_one_row_cas_and_resets_budget(db):
    await held(db)
    row = await inbox_items.get_by_id(db, "held")
    assert await inbox_items.release_replay_hold(db, row, released_at="now")
    assert not await inbox_items.release_replay_hold(db, row, released_at="later")
    updated = await inbox_items.get_by_id(db, "held")
    assert updated["retry_count"] == 0
    assert updated["error_message"] == "replay_authorized:now:replay_unsafe: after tools"
    for field in ("file_path", "batch_items", "content_hash", "drop_id", "evaluated_content", "response_path"):
        assert updated[field] == row[field]
    assert await inbox_items.get_retriable_failure_files(db) == ["/inbox/a.md"]


@pytest.mark.asyncio
async def test_operator_inspect_acknowledged_release_and_missing_changed_source(tmp_path):
    from genesis.inbox.replay_hold import operate
    from genesis.inbox.scanner import compute_hash

    path = tmp_path / "genesis.db"
    source = tmp_path / "source.md"
    source.write_text("https://example.com/a")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await held(conn)
    await conn.execute("UPDATE inbox_items SET file_path=?, content_hash=? WHERE id='held'",
                       (str(source), compute_hash(source)))
    await conn.commit()
    original = await inbox_items.get_by_id(conn, "held")
    assert "replay-held" in await operate(path, "held")
    assert await inbox_items.get_by_id(conn, "held") == original
    with pytest.raises(ValueError, match="acknowledge"):
        await operate(path, "held", release=True)
    source.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        await operate(path, "held", release=True, acknowledge=True)
    source.unlink()
    with pytest.raises(ValueError, match="missing"):
        await operate(path, "held", release=True, acknowledge=True)
    source.write_text("https://example.com/a")
    assert "No work dispatched" in await operate(path, "held", release=True, acknowledge=True)
    with pytest.raises(ValueError, match="not a failed"):
        await operate(path, "held", release=True, acknowledge=True)
    await conn.close()


@pytest.mark.asyncio
async def test_operator_cli_runs_against_an_existing_isolated_database(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from genesis.inbox.scanner import compute_hash

    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source.md"
    source.write_text("https://example.com/a")
    path = tmp_path / "cli.db"
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    try:
        await held(conn)
        await conn.execute("UPDATE inbox_items SET file_path=?, content_hash=? WHERE id='held'",
                           (str(source), compute_hash(source)))
        await conn.commit()
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "GENESIS_HOME": str(tmp_path / "runtime")}
        command = [sys.executable, str(root / "scripts/inbox_replay_hold.py"), "--db", str(path), "--item", "held"]
        inspected = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10)
        assert inspected.returncode == 0 and "replay-held" in inspected.stdout
        refused = subprocess.run(command + ["--release"], env=env, text=True, capture_output=True, timeout=10)
        assert refused.returncode == 1 and "acknowledge" in refused.stderr
        released = subprocess.run(command + ["--release", "--acknowledge-replay"], env=env,
                                  text=True, capture_output=True, timeout=10)
        assert released.returncode == 0 and "No work dispatched" in released.stdout
        assert (await inbox_items.get_by_id(conn, "held"))["retry_count"] == 0
        missing = tmp_path / "missing.db"
        absent = subprocess.run([sys.executable, str(root / "scripts/inbox_replay_hold.py"),
                                 "--db", str(missing), "--item", "held"], env=env,
                                text=True, capture_output=True, timeout=10)
        assert absent.returncode == 1 and not missing.exists()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_operator_does_not_recreate_db_disappearing_after_inspection(tmp_path, monkeypatch):
    import sqlite3

    from genesis.inbox import replay_hold
    from genesis.inbox.scanner import compute_hash

    path = tmp_path / "vanishing.db"
    backup = tmp_path / "preserved.db"
    source = tmp_path / "source.md"
    source.write_text("https://example.com/a")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await held(conn)
    await conn.execute("UPDATE inbox_items SET file_path=?, content_hash=? WHERE id='held'",
                       (str(source), compute_hash(source)))
    await conn.commit()
    await conn.close()
    original_admission = replay_hold.assert_admitted

    def remove_between_inspection_and_writer(db_path):
        path.rename(backup)
        original_admission(db_path)

    monkeypatch.setattr(replay_hold, "assert_admitted", remove_between_inspection_and_writer)
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        await replay_hold.operate(path, "held", release=True, acknowledge=True)
    assert not path.exists() and backup.is_file()
    check = await aiosqlite.connect(backup)
    check.row_factory = aiosqlite.Row
    try:
        assert (await inbox_items.get_by_id(check, "held"))["error_message"].startswith("replay_unsafe:")
    finally:
        await check.close()
