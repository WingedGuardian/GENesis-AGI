"""Tests for call_site_recorder shared helper."""

import aiosqlite
import pytest

from genesis.observability.call_site_recorder import record_last_run


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "test.db"
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute("""
            CREATE TABLE call_site_last_run (
                call_site_id TEXT PRIMARY KEY,
                last_run_at TEXT NOT NULL,
                provider_used TEXT,
                model_id TEXT,
                response_text TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                success INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
        """)
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_record_inserts_new(db):
    await record_last_run(
        db, "test_site",
        provider="openrouter", model_id="haiku-3.5",
        response_text="hello world",
        input_tokens=100, output_tokens=50,
    )
    cursor = await db.execute("SELECT * FROM call_site_last_run WHERE call_site_id = ?", ("test_site",))
    row = await cursor.fetchone()
    assert row is not None
    assert row[2] == "openrouter"  # provider_used
    assert row[3] == "haiku-3.5"  # model_id
    assert row[4] == "hello world"  # response_text
    assert row[5] == 100  # input_tokens
    assert row[6] == 50  # output_tokens
    assert row[7] == 1  # success


@pytest.mark.asyncio
async def test_record_replaces_existing(db):
    await record_last_run(db, "test_site", provider="a", model_id="m1", response_text="first")
    await record_last_run(db, "test_site", provider="b", model_id="m2", response_text="second")

    cursor = await db.execute("SELECT provider_used, response_text FROM call_site_last_run WHERE call_site_id = ?", ("test_site",))
    row = await cursor.fetchone()
    assert row[0] == "b"
    assert row[1] == "second"


@pytest.mark.asyncio
async def test_record_failure(db):
    await record_last_run(
        db, "test_site", provider="x", model_id="m",
        response_text=None, success=False,
    )
    cursor = await db.execute("SELECT success FROM call_site_last_run WHERE call_site_id = ?", ("test_site",))
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_record_survives_missing_table(tmp_path):
    """Should not raise even if table doesn't exist — just logs a warning."""
    path = tmp_path / "empty.db"
    async with aiosqlite.connect(str(path)) as conn:
        # No table created — should not raise
        await record_last_run(conn, "x", provider="p", model_id="m", response_text="t")
