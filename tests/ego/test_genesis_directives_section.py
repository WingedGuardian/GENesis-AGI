"""Tests for the Genesis-ego directives context section (PR-3).

The Genesis (COO) ego previously could not see directives targeted at it —
`genesis_context` had no directives section, so a `genesis_ego` directive was
immortal and invisible. This verifies the new section renders active
`genesis_ego` directives, ignores `user_ego` ones, excludes resolved rows, and
stays empty (no polluting header) when there are none.
"""

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES, create_all_tables
from genesis.ego.genesis_context import GenesisEgoContextBuilder


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_directives"])
        await conn.commit()
        yield conn


def _builder(conn):
    return GenesisEgoContextBuilder(db=conn)


class TestGenesisDirectivesSection:
    async def test_empty_when_no_directives(self, db):
        assert await _builder(db)._directives_section() == ""

    async def test_renders_active_genesis_directive(self, db):
        await ego_crud.create_directive(
            db,
            content="Weekly install test needs to run.",
            priority="high",
            ego_target="genesis_ego",
            source="user",
        )
        out = await _builder(db)._directives_section()
        assert "## User Directives" in out
        assert "[HIGH]" in out
        assert "Weekly install test" in out
        # Soft framing (no must-propose): invites resolve/disagree, not silence.
        assert "silently" in out.lower()

    async def test_ignores_user_ego_directives(self, db):
        # A user_ego directive must NOT bleed into the COO's section.
        await ego_crud.create_directive(
            db,
            content="User-only directive",
            priority="high",
            ego_target="user_ego",
            source="user",
        )
        assert await _builder(db)._directives_section() == ""

    async def test_excludes_resolved(self, db):
        did = await ego_crud.create_directive(
            db,
            content="Already handled by cron",
            priority="high",
            ego_target="genesis_ego",
            source="user",
        )
        await ego_crud.resolve_directive(db, did, status="completed", resolution="done")
        assert await _builder(db)._directives_section() == ""

    async def test_shows_id_for_resolution(self, db):
        # The rendered id is what the ego echoes back in resolved_directives.
        did = await ego_crud.create_directive(
            db,
            content="Resolve me by id",
            priority="critical",
            ego_target="genesis_ego",
            source="user",
        )
        out = await _builder(db)._directives_section()
        assert f"id={did}" in out

    async def test_wired_into_full_build(self):
        # Proves the ("directives", ...) tuple is actually reached by build()
        # (it is also in _ALWAYS_SECTIONS, so never weighted to skip). Uses a
        # full schema so every other section builder has its tables.
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await create_all_tables(conn)
            await ego_crud.create_directive(
                conn,
                content="Render-me directive in full build",
                priority="critical",
                ego_target="genesis_ego",
                source="user",
            )
            out = await GenesisEgoContextBuilder(db=conn).build()
            assert "## User Directives" in out
            assert "Render-me directive in full build" in out
