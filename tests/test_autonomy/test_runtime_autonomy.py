"""Tests for runtime autonomy wiring (Step 14)."""

from __future__ import annotations

import pytest

from genesis.runtime import GenesisRuntime


@pytest.fixture(autouse=True)
def _reset_runtime():
    GenesisRuntime.reset()
    yield
    GenesisRuntime.reset()


class TestAutonomyProperties:
    def test_autonomy_manager_default_none(self):
        rt = GenesisRuntime.instance()
        assert rt.autonomy_manager is None

    def test_action_classifier_default_none(self):
        rt = GenesisRuntime.instance()
        assert rt.action_classifier is None

    def test_task_verifier_default_none(self):
        rt = GenesisRuntime.instance()
        assert rt.task_verifier is None

    def test_protected_paths_default_none(self):
        rt = GenesisRuntime.instance()
        assert rt.protected_paths is None


class TestAutonomyInit:
    def test_init_autonomy_creates_components(self):
        """_init_autonomy loads protection, classifier, verifier even without DB."""
        rt = GenesisRuntime.instance()
        rt._init_autonomy()
        # Protected paths, classifier, verifier should be set
        assert rt.protected_paths is not None
        assert rt.action_classifier is not None
        assert rt.task_verifier is not None
        # autonomy_manager needs DB so should be None
        assert rt.autonomy_manager is None

    @pytest.mark.asyncio
    async def test_init_autonomy_with_db(self, tmp_path):
        """With a DB, autonomy_manager is also created."""
        import aiosqlite

        from genesis.db.schema import create_all_tables

        db_path = tmp_path / "test.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)

        rt = GenesisRuntime.instance()
        rt._db = conn
        rt._init_autonomy()

        assert rt.autonomy_manager is not None
        assert rt.protected_paths is not None
        assert rt.action_classifier is not None
        assert rt.task_verifier is not None

        await conn.close()

    def test_init_checks_includes_autonomy(self):
        """Bootstrap manifest should track autonomy step."""
        rt = GenesisRuntime.instance()
        assert "autonomy" in rt._INIT_CHECKS
        assert rt._INIT_CHECKS["autonomy"] == "_autonomy_manager"
