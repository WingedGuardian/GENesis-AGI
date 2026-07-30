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

    async def test_reset_accepts_any_focus(self, db):
        """Focus reset is a simple write-through — behavioral regex removed
        in PR #456 (computed focus makes it unnecessary)."""
        _conn, db_path = db

        from genesis.mcp.health.ego_tools import _impl_ego_focus_reset

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path), \
             patch("genesis.memory.essential_knowledge.generate_and_write", new_callable=AsyncMock):
            result = await _impl_ego_focus_reset("holding back until user is ready")

        assert result["status"] == "reset"

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


# -- Thread 2: ego_directive + ego_proposal_resolve are user-only --


@pytest.fixture
async def directive_db(tmp_path):
    """A file DB carrying only the ego_directives table (for the create path)."""
    db_path = tmp_path / "dir.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_directives"])
        await conn.commit()
        yield conn, db_path


class TestEgoDirectiveUserOnly:
    """ego_directive stamps source="user", so it must be refused from an
    autonomous/dispatched session and allowed only from a foreground/supervised
    one (is_dispatched_session_env gate)."""

    async def test_refused_when_dispatched(self, directive_db, monkeypatch):
        conn, db_path = directive_db
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)

        from genesis.mcp.health.ego_tools import ego_directive

        with patch(
            "genesis.mcp.health.ego_tools._get_db_path", return_value=db_path
        ):
            result = await ego_directive.fn(
                "test directive", priority="high", ego_target="genesis_ego"
            )
        assert result["status"] == "refused"
        row = await (
            await conn.execute("SELECT COUNT(*) AS c FROM ego_directives")
        ).fetchone()
        assert row["c"] == 0  # nothing written

    async def test_allowed_when_supervised(self, directive_db, monkeypatch):
        _conn, db_path = directive_db
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "1")

        from genesis.mcp.health.ego_tools import ego_directive

        with patch(
            "genesis.mcp.health.ego_tools._get_db_path", return_value=db_path
        ):
            result = await ego_directive.fn(
                "test directive", priority="high", ego_target="user_ego"
            )
        assert result["status"] == "created"

    async def test_allowed_when_no_cc_session(self, directive_db, monkeypatch):
        _conn, db_path = directive_db
        monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
        monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)

        from genesis.mcp.health.ego_tools import ego_directive

        with patch(
            "genesis.mcp.health.ego_tools._get_db_path", return_value=db_path
        ):
            result = await ego_directive.fn(
                "test directive", priority="normal", ego_target="user_ego"
            )
        assert result["status"] == "created"


class TestEgoProposalResolveUserOnly:
    """ego_proposal_resolve is a user authority (and its withdrawn-proposal path
    forges a source="user" directive) — refused from dispatched sessions."""

    async def test_refused_when_dispatched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)

        from genesis.mcp.health.ego_tools import ego_proposal_resolve

        # Patch the db path to an empty tmp file so that even if the gate
        # FAILED to fire, the call could never touch the real database.
        with patch(
            "genesis.mcp.health.ego_tools._get_db_path",
            return_value=tmp_path / "empty.db",
        ):
            result = await ego_proposal_resolve.fn("reject", proposal_ids="whatever")
        assert result["status"] == "refused"

    async def test_allowed_when_no_cc_session_reaches_logic(self, tmp_path, monkeypatch):
        """A non-dispatched caller passes the gate (reaches normal handling)."""
        monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
        monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)

        db_path = tmp_path / "res.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(TABLES["ego_proposals"])
            await conn.commit()

        from genesis.mcp.health.ego_tools import ego_proposal_resolve

        with patch(
            "genesis.mcp.health.ego_tools._get_db_path", return_value=db_path
        ):
            result = await ego_proposal_resolve.fn("reject", proposal_ids="none-such")
        # Passed the gate -> normal handling (no such proposal), NOT "refused".
        assert result["status"] != "refused"


class TestUserAuthorityToolsDisallowedInCycle:
    def test_directive_and_resolve_disallowed(self):
        from genesis.ego.session import _EGO_CYCLE_DISALLOWED_TOOLS

        assert "mcp__genesis-health__ego_directive" in _EGO_CYCLE_DISALLOWED_TOOLS
        assert (
            "mcp__genesis-health__ego_proposal_resolve" in _EGO_CYCLE_DISALLOWED_TOOLS
        )
