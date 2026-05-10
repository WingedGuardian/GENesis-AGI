"""Tests for the ego focus reset MCP tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES


@pytest.fixture
async def db(tmp_path):
    """In-memory DB with ego tables."""
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_state"])
        await conn.commit()
        yield conn, db_path


class TestEgoFocusReset:
    async def test_reset_clears_holdback_focus(self, db):
        conn, db_path = db
        await ego_crud.set_state(conn, key="ego_focus_summary", value="Holding back — user busy")
        await conn.commit()

        from genesis.mcp.health.ego_tools import _impl_ego_focus_reset

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path), \
             patch("genesis.memory.essential_knowledge.generate_and_write", new_callable=AsyncMock):
            result = await _impl_ego_focus_reset()

        assert result["status"] == "reset"
        assert result["focus_set_to"] == "general system awareness"
        assert result["details"]["ego_focus_summary"]["old"] == "Holding back — user busy"

    async def test_reset_with_custom_focus(self, db):
        conn, db_path = db
        await ego_crud.set_state(conn, key="ego_focus_summary", value="old focus")
        await conn.commit()

        from genesis.mcp.health.ego_tools import _impl_ego_focus_reset

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path), \
             patch("genesis.memory.essential_knowledge.generate_and_write", new_callable=AsyncMock):
            result = await _impl_ego_focus_reset("monitoring API costs")

        assert result["status"] == "reset"
        assert result["focus_set_to"] == "monitoring API costs"

    async def test_reset_rejects_behavioral_focus(self, db):
        _conn, db_path = db

        from genesis.mcp.health.ego_tools import _impl_ego_focus_reset

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path):
            result = await _impl_ego_focus_reset("holding back until user is ready")

        assert result["status"] == "rejected"
        assert "behavioral" in result["reason"].lower()

    async def test_reset_both_ego_keys(self, db):
        conn, db_path = db
        await ego_crud.set_state(conn, key="ego_focus_summary", value="old1")
        await ego_crud.set_state(conn, key="genesis_ego_focus_summary", value="old2")
        await conn.commit()

        from genesis.mcp.health.ego_tools import _impl_ego_focus_reset

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path), \
             patch("genesis.memory.essential_knowledge.generate_and_write", new_callable=AsyncMock):
            result = await _impl_ego_focus_reset()

        details = result["details"]
        assert "ego_focus_summary" in details
        assert "genesis_ego_focus_summary" in details
        assert details["ego_focus_summary"]["new"] == "general system awareness"
        assert details["genesis_ego_focus_summary"]["new"] == "general system awareness"
