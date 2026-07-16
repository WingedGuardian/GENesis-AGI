"""ego_goal_create origin (provenance) — additive ego autonomy PR-2.

The tool stamps who owns a goal: 'user' (default, fail-safe) or
'genesis_ego' (ego-created, later self-manageable additively). Origin is
immutable after create — user_goals.update() must silently drop it.
"""

from __future__ import annotations

from unittest.mock import patch

import aiosqlite
import pytest

from genesis.db.crud import user_goals
from genesis.db.schema import TABLES


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["user_goals"])
        await conn.commit()
        yield conn, db_path


async def _origin_of(conn, goal_id: str) -> str:
    cur = await conn.execute("SELECT origin FROM user_goals WHERE id = ?", (goal_id,))
    return (await cur.fetchone())["origin"]


class TestEgoGoalCreateOrigin:
    async def test_tool_always_stamps_user(self, db):
        conn, db_path = db
        from genesis.mcp.health.ego_tools import _impl_ego_goal_create as ego_goal_create

        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path):
            result = await ego_goal_create(title="A user goal")

        assert result["status"] == "created"
        assert result["origin"] == "user"
        assert await _origin_of(conn, result["goal_id"]) == "user"

    async def test_tool_surface_has_no_origin_argument(self, db):
        """Codex P1 (PR #1086): the MCP tool is reachable by any caller, so
        provenance must never be caller input. Passing origin must be a
        TypeError on both the impl and the registered tool schema —
        'genesis_ego' can only be stamped by trusted code via the CRUD."""
        _conn, db_path = db
        from genesis.mcp.health.ego_tools import _impl_ego_goal_create

        with (
            patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path),
            pytest.raises(TypeError),
        ):
            await _impl_ego_goal_create(title="sneaky", origin="genesis_ego")

    async def test_registered_tool_schema_excludes_origin(self):
        """The @mcp.tool wrapper (what remote callers see) must not expose an
        origin parameter either."""
        import inspect

        from genesis.mcp.health import ego_tools

        tool = ego_tools.ego_goal_create
        fn = getattr(tool, "fn", tool)  # FunctionTool wraps the coroutine
        assert "origin" not in inspect.signature(fn).parameters

    async def test_crud_invalid_origin_hits_check_constraint(self, db):
        """The CRUD trusted path is still constrained: the schema CHECK
        rejects unknown origins outright."""
        import sqlite3

        conn, _db_path = db
        with pytest.raises(sqlite3.IntegrityError):
            await user_goals.create(
                conn, title="bad", category="project", origin="admin"
            )


class TestOriginImmutable:
    async def test_update_silently_drops_origin(self, db):
        """origin is the security boundary — if it were mutable, the ego could
        relabel a user goal and gain autonomous control over it."""
        conn, _db_path = db
        gid = await user_goals.create(conn, title="mine", category="project")
        assert await _origin_of(conn, gid) == "user"

        changed = await user_goals.update(conn, gid, origin="genesis_ego")

        # Dropped by the allow-list: no update happened (or at minimum origin
        # is unchanged even if other fields were passed alongside).
        assert changed is False
        assert await _origin_of(conn, gid) == "user"

    async def test_update_with_origin_and_valid_field_keeps_origin(self, db):
        conn, _db_path = db
        gid = await user_goals.create(conn, title="mine", category="project")

        changed = await user_goals.update(conn, gid, origin="genesis_ego", priority="low")

        assert changed is True  # priority applied
        assert await _origin_of(conn, gid) == "user"  # origin untouched

    async def test_crud_create_accepts_origin(self, db):
        conn, _db_path = db
        gid = await user_goals.create(conn, title="ego's", category="project", origin="genesis_ego")
        assert await _origin_of(conn, gid) == "genesis_ego"


class TestEgoGoalListShowsOrigin:
    async def test_list_includes_origin_field(self, db):
        """PR-3a display fix: every row carries its provenance so the user
        can always tell whose goal is whose."""
        conn, db_path = db
        await user_goals.create(conn, title="User goal", category="career")
        await user_goals.create(
            conn, title="Ego goal", category="project", origin="genesis_ego",
        )
        await conn.commit()

        from genesis.mcp.health import ego_tools

        fn = getattr(ego_tools.ego_goal_list, "fn", ego_tools.ego_goal_list)
        with patch("genesis.mcp.health.ego_tools._get_db_path", return_value=db_path):
            result = await fn()

        assert result["status"] == "ok"
        origins = {g["title"]: g["origin"] for g in result["goals"]}
        assert origins == {"User goal": "user", "Ego goal": "genesis_ego"}
