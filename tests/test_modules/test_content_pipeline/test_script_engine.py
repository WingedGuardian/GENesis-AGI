"""Tests for genesis.modules.content_pipeline.script_engine."""

import aiosqlite
import pytest

from genesis.modules.content_pipeline.script_engine import (
    ScriptEngine,
    _row_to_script,
    ensure_table,
)
from genesis.modules.content_pipeline.types import ContentIdea


@pytest.fixture
async def db():
    """In-memory database with content_scripts table."""
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_table(conn)
        yield conn


@pytest.fixture
def idea():
    return ContentIdea(id="idea-1", source="manual", content="AGI is here", tags=["agi"])


class TestEnsureTable:
    @pytest.mark.asyncio
    async def test_fresh_install_has_all_columns(self):
        async with aiosqlite.connect(":memory:") as db:
            await ensure_table(db)
            cursor = await db.execute("PRAGMA table_info(content_scripts)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "status" in cols
            assert "register" in cols

    @pytest.mark.asyncio
    async def test_migration_adds_missing_columns(self):
        async with aiosqlite.connect(":memory:") as db:
            # Create old-schema table
            await db.execute("""
                CREATE TABLE content_scripts (
                    id TEXT PRIMARY KEY,
                    idea_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    voice_calibrated INTEGER NOT NULL DEFAULT 0,
                    anti_slop_passed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            await db.commit()

            await ensure_table(db)
            cursor = await db.execute("PRAGMA table_info(content_scripts)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "status" in cols
            assert "register" in cols

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self):
        async with aiosqlite.connect(":memory:") as db:
            await ensure_table(db)
            await ensure_table(db)  # Should not raise
            cursor = await db.execute("PRAGMA table_info(content_scripts)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "status" in cols


class TestDispatchMode:
    @pytest.mark.asyncio
    async def test_dispatch_mode_records_pending_draft(self, db, idea):
        engine = ScriptEngine(db, drafter=None, dispatch_mode=True)
        script = await engine.draft_script(idea, "medium", config={"register": "public_content"})

        assert script.status == "pending_draft"
        assert script.register == "public_content"
        assert script.content == idea.content  # Raw content, no LLM

    @pytest.mark.asyncio
    async def test_default_mode_drafts_immediately(self, db, idea):
        engine = ScriptEngine(db, drafter=None, dispatch_mode=False)
        script = await engine.draft_script(idea, "linkedin")

        assert script.status == "drafted"

    @pytest.mark.asyncio
    async def test_dispatch_mode_persists_to_db(self, db, idea):
        engine = ScriptEngine(db, drafter=None, dispatch_mode=True)
        script = await engine.draft_script(idea, "medium")

        cursor = await db.execute(
            "SELECT status, register FROM content_scripts WHERE id = ?",
            (script.id,),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending_draft"


class TestRowToScript:
    @pytest.mark.asyncio
    async def test_named_columns(self, db, idea):
        engine = ScriptEngine(db, drafter=None)
        script = await engine.draft_script(idea, "medium", config={"register": "public_content"})

        cursor = await db.execute("SELECT * FROM content_scripts WHERE id = ?", (script.id,))
        row = await cursor.fetchone()
        col_names = [desc[0] for desc in cursor.description]
        result = _row_to_script(row, col_names=col_names)

        assert result.id == script.id
        assert result.status == "drafted"
        assert result.register == "public_content"

    def test_positional_fallback_short_row(self):
        """Old-schema row without status/register columns."""
        row = ("id-1", "idea-1", "content", "medium", 0, 0, "2026-01-01")
        result = _row_to_script(row)

        assert result.id == "id-1"
        assert result.status == "drafted"  # Default
        assert result.register is None  # Default


class TestRefineScript:
    @pytest.mark.asyncio
    async def test_refine_preserves_register(self, db, idea):
        engine = ScriptEngine(db, drafter=None)
        original = await engine.draft_script(idea, "medium", config={"register": "public_content"})

        refined = await engine.refine_script(original.id, "make it shorter")
        assert refined.status == "refined"
        assert refined.register == "public_content"
        assert refined.id != original.id
