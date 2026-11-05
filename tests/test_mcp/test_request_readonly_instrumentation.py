"""The scoped migration retains legacy read-only instrumentation boundaries."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.db.connection import get_db
from genesis.db.request_database import RequestDatabase
from genesis.observability.mcp_middleware import install_instrumentation


@pytest.mark.parametrize("persist", (False, True))
@pytest.mark.parametrize("fail", (False, True))
async def test_legacy_readonly_metrics_do_not_finalize_a_borrowed_transaction(tmp_path, persist, fail):
    path = tmp_path / "legacy.db"
    db = await get_db(path)
    try:
        await db.execute("CREATE TABLE samples (value TEXT)")
        await db.commit()
        await db.execute("INSERT INTO samples VALUES ('pending')")
        server = FastMCP("legacy-readonly-fixture")
        tracker = SimpleNamespace(persist_activity=persist, record=Mock())
        install_instrumentation(server, tracker, "health", db)

        @server.tool()
        async def read() -> int:
            rows = await db.execute_fetchall("SELECT value FROM samples")
            if fail:
                raise ValueError("synthetic tool failure")
            return len(rows)

        async with Client(server) as client:
            if fail:
                with pytest.raises(ToolError):
                    await client.call_tool("read")
            else:
                assert (await client.call_tool("read")).data == 1
        assert db.in_transaction is (not persist)
        with sqlite3.connect(path) as reader:
            rows = reader.execute("SELECT value FROM samples").fetchall()
        assert rows == ([('pending',)] if persist and not fail else [])
    finally:
        await db.rollback()
        await db.close()


@pytest.mark.parametrize("fail", (False, True))
async def test_readonly_metrics_do_not_disable_mandatory_scoped_ownership(tmp_path, fail):
    path = tmp_path / "scoped.db"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE samples (value TEXT)")
    db = RequestDatabase(path, max_units=1)
    try:
        server = FastMCP("scoped-readonly-fixture")
        tracker = SimpleNamespace(persist_activity=False, record=Mock())
        install_instrumentation(server, tracker, "health", db)

        @server.tool()
        async def write() -> str:
            await db.execute("INSERT INTO samples VALUES ('scoped')")
            if fail:
                raise ValueError("synthetic tool failure")
            return "written"

        async with Client(server) as client:
            if fail:
                with pytest.raises(ToolError):
                    await client.call_tool("write")
            else:
                assert (await client.call_tool("write")).data == "written"
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT value FROM samples").fetchall() == ([] if fail else [('scoped',)])
        assert not db._owners
        assert db._slots._value == 1
    finally:
        await db.close()
