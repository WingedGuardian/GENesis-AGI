"""Tests for knowledge_uploads CRUD operations + filename sanitization."""

import json

import pytest

from genesis.db.crud import knowledge_uploads
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = None
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def test_insert_and_get(db):
    uid = await knowledge_uploads.insert(
        db,
        filename="test.pdf",
        file_path="/tmp/test.pdf",
        file_size=1024,
        mime_type="application/pdf",
    )
    assert uid
    row = await knowledge_uploads.get(db, uid)
    assert row is not None
    assert row["filename"] == "test.pdf"
    assert row["file_size"] == 1024
    assert row["status"] == "uploaded"
    assert row["created_at"] is not None


async def test_get_nonexistent(db):
    assert await knowledge_uploads.get(db, "nonexistent") is None


async def test_update_status_processing(db):
    uid = await knowledge_uploads.insert(
        db, filename="a.txt", file_path="/tmp/a.txt", file_size=10,
    )
    updated = await knowledge_uploads.update_status(
        db, uid,
        status="processing",
        project_type="pro",
        domain="aws",
    )
    assert updated is True
    row = await knowledge_uploads.get(db, uid)
    assert row["status"] == "processing"
    assert row["project_type"] == "pro"
    assert row["domain"] == "aws"
    assert row["completed_at"] is None  # not a terminal state


async def test_update_status_completed(db):
    uid = await knowledge_uploads.insert(
        db, filename="b.txt", file_path="/tmp/b.txt", file_size=10,
    )
    await knowledge_uploads.update_status(db, uid, status="processing")
    updated = await knowledge_uploads.update_status(
        db, uid,
        status="completed",
        unit_ids=["u1", "u2"],
    )
    assert updated is True
    row = await knowledge_uploads.get(db, uid)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert json.loads(row["unit_ids"]) == ["u1", "u2"]


async def test_update_status_failed(db):
    uid = await knowledge_uploads.insert(
        db, filename="c.txt", file_path="/tmp/c.txt", file_size=10,
    )
    await knowledge_uploads.update_status(
        db, uid,
        status="failed",
        error_message="Processing crashed",
    )
    row = await knowledge_uploads.get(db, uid)
    assert row["status"] == "failed"
    assert row["error_message"] == "Processing crashed"
    assert row["completed_at"] is not None


async def test_list_recent(db):
    for i in range(5):
        await knowledge_uploads.insert(
            db, filename=f"f{i}.txt", file_path=f"/tmp/f{i}.txt", file_size=i * 100,
        )
    results = await knowledge_uploads.list_recent(db, limit=3)
    assert len(results) == 3
    # Newest first
    assert results[0]["filename"] == "f4.txt"


async def test_taxonomy_empty(db):
    tax = await knowledge_uploads.taxonomy(db)
    assert tax["project_types"] == []
    assert tax["domains"] == []


async def test_taxonomy_populated(db):
    from genesis.db.crud import knowledge

    await knowledge.insert(
        db, project_type="cloud-eng", domain="aws", source_doc="s1",
        concept="Test", body="Body",
    )
    await knowledge.insert(
        db, project_type="professional", domain="resume", source_doc="s2",
        concept="Test2", body="Body2",
    )
    tax = await knowledge_uploads.taxonomy(db)
    assert "cloud-eng" in tax["project_types"]
    assert "professional" in tax["project_types"]
    assert "aws" in tax["domains"]
    assert "resume" in tax["domains"]


async def test_atomic_transition_success(db):
    uid = await knowledge_uploads.insert(
        db, filename="a.txt", file_path="/tmp/a.txt", file_size=10,
    )
    ok = await knowledge_uploads.atomic_transition(
        db, uid, from_status="uploaded", to_status="processing",
        project_type="pro", domain="test",
    )
    assert ok is True
    row = await knowledge_uploads.get(db, uid)
    assert row["status"] == "processing"
    assert row["project_type"] == "pro"


async def test_atomic_transition_wrong_state(db):
    uid = await knowledge_uploads.insert(
        db, filename="b.txt", file_path="/tmp/b.txt", file_size=10,
    )
    # Transition to processing first
    await knowledge_uploads.atomic_transition(
        db, uid, from_status="uploaded", to_status="processing",
    )
    # Second attempt should fail (already processing)
    ok = await knowledge_uploads.atomic_transition(
        db, uid, from_status="uploaded", to_status="processing",
    )
    assert ok is False


async def test_delete(db):
    uid = await knowledge_uploads.insert(
        db, filename="del.txt", file_path="/tmp/del.txt", file_size=10,
    )
    assert await knowledge_uploads.delete(db, uid) is True
    assert await knowledge_uploads.get(db, uid) is None
    assert await knowledge_uploads.delete(db, "nonexistent") is False


# ─── Filename sanitization ─────────────────────────────────────────────────


def test_sanitize_filename():
    from genesis.dashboard.routes.knowledge_upload import _sanitize_filename

    # Path traversal attacks
    assert _sanitize_filename("../../../etc/passwd") == "passwd"
    assert _sanitize_filename("/absolute/path/file.txt") == "file.txt"
    # Dot-dot attack — must not allow '..' as filename
    assert _sanitize_filename("..") == "unnamed"
    assert _sanitize_filename(".") == "unnamed"
    assert _sanitize_filename("...") == "unnamed"
    # Normal files pass through
    assert _sanitize_filename("normal.pdf") == "normal.pdf"
    assert _sanitize_filename("file with spaces.txt") == "file with spaces.txt"
    # Null bytes and special chars stripped
    assert _sanitize_filename("file\x00null.txt") == "file_null.txt"
    # Empty
    assert _sanitize_filename("") == "unnamed"
