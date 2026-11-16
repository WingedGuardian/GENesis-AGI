"""Actual physical completion, independent of a canceled asyncio proxy."""

import asyncio
import sqlite3
import threading

import pytest

from genesis.db.request_database import RequestDatabase

pytestmark = pytest.mark.asyncio


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "physical.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE samples (value TEXT)")
    return path


async def test_cancel_retains_unit_until_actual_file_use_finishes(database_path, tmp_path):
    db = RequestDatabase(database_path, max_units=1)
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    resource = (tmp_path / "resource").open("w")
    errors = []

    def operation():
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10)
            resource.write("actual physical use")
        except ValueError:
            errors.append("actual write to closed file")
            raise
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def owner():
        async with db.unit_of_work():
            # Baseline characterization deliberately exercises the old thread
            # path until the ownership API exists. Removing that API stays RED.
            run = getattr(db, "run_in_thread", asyncio.to_thread)
            await run(operation)

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        await asyncio.sleep(.05)
        retained = not task.done()
        if not retained:
            resource.close()  # effective old-boundary fault, owned file only
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await asyncio.gather(task, return_exceptions=True)
        assert (retained, errors) == (True, [])
        assert task.cancelled()
        assert db._slots._value == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()
        resource.close()
