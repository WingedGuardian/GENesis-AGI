"""Grace permits admitted work; expiration permits cancellation, not closure."""

import asyncio
import sqlite3

import pytest

from genesis.db.request_database import RequestDatabase

pytestmark = pytest.mark.asyncio


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "drain.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE samples (value TEXT)")
    return path


async def test_grace_preserves_work_that_finishes_before_deadline(database_path):
    db = RequestDatabase(database_path, max_units=1)
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def owner():
        async with db.unit_of_work():
            entered.set()
            try:
                await release.wait()
                await db.execute("INSERT INTO samples VALUES ('kept')")
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    task = asyncio.create_task(owner())
    await entered.wait()
    drain = getattr(db, "drain", lambda **kwargs: db.close())
    closing = asyncio.create_task(drain(grace_s=1))
    try:
        await asyncio.sleep(.05)
        release.set()
        await asyncio.gather(task, closing, return_exceptions=True)
        assert not cancelled
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT value FROM samples").fetchall() == [("kept",)]
    finally:
        release.set()
        await asyncio.gather(task, closing, return_exceptions=True)
        await db.close()
