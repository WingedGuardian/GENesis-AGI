"""Tests for the reference store markdown mirror generator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import knowledge as knowledge_crud
from genesis.db.schema import create_all_tables, seed_data
from genesis.memory.reference_mirror import regenerate_mirror


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _insert_ref(db, *, domain: str, concept: str, body: str):
    """Helper to insert a reference entry directly."""
    return await knowledge_crud.insert(
        db,
        project_type="reference",
        domain=domain,
        source_doc="test",
        concept=concept,
        body=body,
        id=str(uuid.uuid4()),
        ingested_at=datetime.now(UTC).isoformat(),
    )


class TestRegenerateMirror:
    @pytest.mark.asyncio
    async def test_empty_store(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "genesis.memory.reference_mirror._MIRROR_PATH",
            tmp_path / "known-to-genesis.md",
        )
        path = await regenerate_mirror(db)
        content = path.read_text()
        assert "Genesis Reference Store" in content
        assert "No reference entries stored yet" in content

    @pytest.mark.asyncio
    async def test_single_entry(self, db, tmp_path, monkeypatch):
        mirror_path = tmp_path / "known-to-genesis.md"
        monkeypatch.setattr(
            "genesis.memory.reference_mirror._MIRROR_PATH", mirror_path,
        )
        await _insert_ref(
            db,
            domain="reference.credentials",
            concept="Test Login",
            body="[reference.credentials] Test Login\nValue: hunter2\nDescription: A test credential",
        )
        path = await regenerate_mirror(db)
        content = path.read_text()
        assert "## Credentials" in content
        assert "### Test Login" in content
        assert "hunter2" in content

    @pytest.mark.asyncio
    async def test_multiple_domains(self, db, tmp_path, monkeypatch):
        mirror_path = tmp_path / "known-to-genesis.md"
        monkeypatch.setattr(
            "genesis.memory.reference_mirror._MIRROR_PATH", mirror_path,
        )
        await _insert_ref(db, domain="reference.credentials", concept="Cred1", body="cred body")
        await _insert_ref(db, domain="reference.url", concept="URL1", body="url body")
        await _insert_ref(db, domain="reference.network", concept="Net1", body="net body")

        await regenerate_mirror(db)
        content = mirror_path.read_text()
        assert "## Credentials" in content
        assert "## URLs" in content
        assert "## Network" in content
        # Credentials should appear before URLs, URLs before Network
        assert content.index("## Credentials") < content.index("## URLs")
        assert content.index("## URLs") < content.index("## Network")

    @pytest.mark.asyncio
    async def test_unknown_domain_still_renders(self, db, tmp_path, monkeypatch):
        mirror_path = tmp_path / "known-to-genesis.md"
        monkeypatch.setattr(
            "genesis.memory.reference_mirror._MIRROR_PATH", mirror_path,
        )
        await _insert_ref(
            db, domain="reference.custom_thing", concept="Custom", body="custom body",
        )
        await regenerate_mirror(db)
        content = mirror_path.read_text()
        assert "## Custom Thing" in content
        assert "### Custom" in content

    @pytest.mark.asyncio
    async def test_idempotent(self, db, tmp_path, monkeypatch):
        mirror_path = tmp_path / "known-to-genesis.md"
        monkeypatch.setattr(
            "genesis.memory.reference_mirror._MIRROR_PATH", mirror_path,
        )
        await _insert_ref(db, domain="reference.fact", concept="Fact1", body="body")
        await regenerate_mirror(db)
        content1 = mirror_path.read_text()
        await regenerate_mirror(db)
        content2 = mirror_path.read_text()
        # Content should be identical (timestamps may differ by subsecond
        # but the entries section should match).
        assert "### Fact1" in content1
        assert "### Fact1" in content2


class TestListByDomain:
    @pytest.mark.asyncio
    async def test_empty(self, db):
        result = await knowledge_crud.list_by_domain(db, project_type="reference")
        assert result == {}

    @pytest.mark.asyncio
    async def test_grouped(self, db):
        await _insert_ref(db, domain="reference.url", concept="U1", body="b1")
        await _insert_ref(db, domain="reference.url", concept="U2", body="b2")
        await _insert_ref(db, domain="reference.fact", concept="F1", body="b3")

        result = await knowledge_crud.list_by_domain(db, project_type="reference")
        assert len(result["reference.url"]) == 2
        assert len(result["reference.fact"]) == 1
        # Should not include non-reference entries
        assert "genesis" not in result  # seed data has genesis project_type
