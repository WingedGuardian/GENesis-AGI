"""Shared fixtures for content pipeline tests."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.modules.content_pipeline import analytics, idea_bank, planner, publisher, script_engine


@pytest.fixture
async def db():
    """In-memory aiosqlite database with all content pipeline tables."""
    async with aiosqlite.connect(":memory:") as conn:
        await idea_bank.ensure_table(conn)
        await planner.ensure_table(conn)
        await script_engine.ensure_table(conn)
        await publisher.ensure_table(conn)
        await analytics.ensure_table(conn)
        yield conn
