"""Tests for mail CRUD operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import mail_items


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create an in-memory DB with the processed_emails table."""
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                id              TEXT PRIMARY KEY,
                message_id      TEXT NOT NULL,
                imap_uid        INTEGER,
                sender          TEXT NOT NULL,
                subject         TEXT NOT NULL,
                received_at     TEXT,
                body_preview    TEXT,
                layer1_verdict  TEXT,
                status          TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'processing', 'completed', 'skipped', 'failed')
                ),
                batch_id        TEXT,
                created_at      TEXT NOT NULL,
                processed_at    TEXT,
                error_message   TEXT,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                content_hash    TEXT
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id "
            "ON processed_emails(message_id)"
        )
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_create_and_get(db):
    item_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await mail_items.create(
        db,
        id=item_id,
        message_id="<test@example.com>",
        imap_uid=42,
        sender="alice@example.com",
        subject="Test Subject",
        received_at=now,
        body_preview="Hello...",
        created_at=now,
    )

    row = await mail_items.get_by_id(db, item_id)
    assert row is not None
    assert row["message_id"] == "<test@example.com>"
    assert row["sender"] == "alice@example.com"
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_exists_by_message_id(db):
    now = datetime.now(UTC).isoformat()
    await mail_items.create(
        db,
        id=str(uuid.uuid4()),
        message_id="<unique@example.com>",
        imap_uid=1,
        sender="test@test.com",
        subject="Test",
        created_at=now,
    )

    assert await mail_items.exists_by_message_id(db, "<unique@example.com>") is True
    assert await mail_items.exists_by_message_id(db, "<missing@example.com>") is False


@pytest.mark.asyncio
async def test_update_status(db):
    item_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await mail_items.create(
        db,
        id=item_id,
        message_id="<status@example.com>",
        imap_uid=1,
        sender="test@test.com",
        subject="Test",
        created_at=now,
    )

    await mail_items.update_status(
        db, item_id, status="completed", processed_at=now,
    )

    row = await mail_items.get_by_id(db, item_id)
    assert row["status"] == "completed"
    assert row["processed_at"] == now


@pytest.mark.asyncio
async def test_update_layer1_verdict(db):
    item_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await mail_items.create(
        db,
        id=item_id,
        message_id="<verdict@example.com>",
        imap_uid=1,
        sender="test@test.com",
        subject="Test",
        created_at=now,
    )

    await mail_items.update_layer1_verdict(db, item_id, verdict="evaluate")

    row = await mail_items.get_by_id(db, item_id)
    assert row["layer1_verdict"] == "evaluate"
