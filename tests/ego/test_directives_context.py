"""Tests for the shared build_directives_section helper (PR-3 DRY extraction).

Both UserEgoContextBuilder and GenesisEgoContextBuilder delegate their
directive section to this one function; only ego_target/framing/error_body
differ. Covers the render, empty, ego-target filtering, and the error-body
fail-soft path (previously untested in either builder).
"""

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.directives_context import build_directives_section

_USER_FRAMING = "*user framing line.*\n"
_GEN_FRAMING = "*genesis framing line.*\n"


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_directives"])
        await conn.commit()
        yield conn


class TestBuildDirectivesSection:
    async def test_empty_returns_blank(self, db):
        out = await build_directives_section(
            db, "user_ego", framing=_USER_FRAMING, error_body="*err*"
        )
        assert out == ""

    async def test_renders_with_framing_and_header(self, db):
        await ego_crud.create_directive(
            db,
            content="Do the thing",
            priority="high",
            ego_target="user_ego",
            source="user",
        )
        out = await build_directives_section(
            db, "user_ego", framing=_USER_FRAMING, error_body="*err*"
        )
        assert out.startswith("## User Directives")
        assert "*user framing line.*" in out
        assert "[HIGH] Do the thing" in out

    async def test_filters_by_ego_target(self, db):
        await ego_crud.create_directive(
            db,
            content="genesis-only",
            priority="high",
            ego_target="genesis_ego",
            source="user",
        )
        # Query for user_ego -> the genesis directive must not appear.
        out = await build_directives_section(
            db, "user_ego", framing=_USER_FRAMING, error_body="*err*"
        )
        assert out == ""
        # Query for genesis_ego with its own framing -> it appears.
        out_g = await build_directives_section(
            db, "genesis_ego", framing=_GEN_FRAMING, error_body="*err*"
        )
        assert "genesis-only" in out_g
        assert "*genesis framing line.*" in out_g

    async def test_error_body_on_query_failure(self):
        # A closed/invalid connection makes the query raise -> error body,
        # not a crash (fail-soft).
        conn = await aiosqlite.connect(":memory:")
        await conn.close()
        out = await build_directives_section(
            conn,
            "user_ego",
            framing=_USER_FRAMING,
            error_body="*directives unavailable*",
        )
        assert "## User Directives" in out
        assert "*directives unavailable*" in out
