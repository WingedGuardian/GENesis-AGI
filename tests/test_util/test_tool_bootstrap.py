import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data


@pytest.mark.asyncio
async def test_bootstrap_populates_tools():
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        await seed_data(db)
        from genesis.util.tool_bootstrap import bootstrap_tool_registry
        count = await bootstrap_tool_registry(db)
        assert count >= 8
        from genesis.db.crud import tool_registry
        rows = await tool_registry.list_all(db)
        assert len(rows) >= 8


@pytest.mark.asyncio
async def test_bootstrap_idempotent():
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        await seed_data(db)
        from genesis.util.tool_bootstrap import bootstrap_tool_registry
        c1 = await bootstrap_tool_registry(db)
        c2 = await bootstrap_tool_registry(db)
        assert c2 == c1  # same count returned
        from genesis.db.crud import tool_registry
        rows = await tool_registry.list_all(db)
        # No duplicates from second run
        assert len(rows) == c1
