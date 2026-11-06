"""Transaction ownership, observed through a separate SQLite reader."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db.connection import get_db
from genesis.db.request_database import RequestDatabase
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import ProviderActivityTracker

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["scoped", "legacy"])
async def database(tmp_path, request):
    path = tmp_path / "units.db"
    db = await get_db(path, foreign_keys=False)
    await db.execute("CREATE TABLE events (value TEXT)")
    await db.commit()
    if request.param == "scoped":
        await db.close()
        db = RequestDatabase(path, max_units=4)
    boundary = InstrumentationMiddleware(ProviderActivityTracker(), "test", db=db)

    async def run(fn):
        if request.param == "scoped":
            async with db.unit_of_work(independent=True):
                return await fn()
        context = SimpleNamespace(message=SimpleNamespace(name="synthetic", arguments={}))
        return await boundary.on_call_tool(context, lambda _: fn())

    @asynccontextmanager
    async def background():
        if request.param == "scoped":
            async with db.unit_of_work():
                yield db
        else:
            yield db
            await db.commit()

    try:
        yield SimpleNamespace(
            run=run, background=background, db=db, path=path, scoped=request.param == "scoped",
        )
    finally:
        await db.close()


async def rows(database):
    async with (
        aiosqlite.connect(database.path) as observer,
        observer.execute("SELECT value FROM events ORDER BY value") as cur,
    ):
        return [row[0] for row in await cur.fetchall()]


async def test_peer_cannot_commit_failed_write(database):
    wrote, peer_active, failed = (asyncio.Event() for _ in range(3))

    async def writer():
        try:
            await database.run(write)
        except ValueError:
            pass
        finally:
            failed.set()

    async def write():
        await database.db.execute("INSERT INTO events VALUES ('partial')")
        wrote.set()
        await peer_active.wait()
        raise ValueError("failed write")

    async def peer():
        await wrote.wait()
        await database.run(wait)

    async def wait():
        peer_active.set()
        await failed.wait()

    await asyncio.gather(writer(), peer())
    assert await rows(database) == ([] if database.scoped else ["partial"])


async def test_peer_cannot_split_two_statement_write(database):
    wrote, peer_committed = asyncio.Event(), asyncio.Event()
    intermediate = []

    async def writer():
        await database.db.execute("INSERT INTO events VALUES ('first')")
        wrote.set()
        await peer_committed.wait()
        await database.db.execute("INSERT INTO events VALUES ('second')")

    async def peer():
        await wrote.wait()
        await database.run(lambda: asyncio.sleep(0))
        intermediate.extend(await rows(database))
        peer_committed.set()

    await asyncio.gather(database.run(writer), peer())
    assert intermediate == ([] if database.scoped else ["first"])
    assert await rows(database) == ["first", "second"]


async def test_solo_failure_cannot_discard_background_write(database):
    async def fail():
        raise ValueError("unrelated tool failed")

    async def unrelated_call():
        with pytest.raises(ValueError):
            await database.run(fail)

    async with database.background() as db:
        await db.execute("INSERT INTO events VALUES ('background')")
        await asyncio.create_task(unrelated_call())
    assert await rows(database) == (["background"] if database.scoped else [])


async def test_peer_cannot_commit_cancelled_write(database):
    wrote, peer_active, cancelled = (asyncio.Event() for _ in range(3))

    async def writer():
        await database.db.execute("INSERT INTO events VALUES ('cancelled')")
        wrote.set()
        await asyncio.Event().wait()

    async def peer():
        await wrote.wait()
        await database.run(wait)

    async def wait():
        peer_active.set()
        await cancelled.wait()

    task = asyncio.create_task(database.run(writer))
    peer_task = asyncio.create_task(peer())
    await peer_active.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled.set()
    await peer_task
    assert await rows(database) == ([] if database.scoped else ["cancelled"])
