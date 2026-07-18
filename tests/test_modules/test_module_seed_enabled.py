"""Native-module `enabled` propagation on fresh installs (Codex audit idx 28).

Before the fix, `_load_native_module` copied only identity fields (never
`enabled`), and the seed path in `_restore_module_states` hard-forced
`mod.enabled = False` for any module lacking a `_config` (i.e. every native
module). So a native module declaring `enabled: true` in its YAML
(content_pipeline) seeded DISABLED on every fresh DB, contradicting its own
declared default. These tests lock the corrected behaviour: the YAML `enabled`
flag reaches the instance at load and survives seeding, while `enabled: false`
natives and externals are unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.runtime.init.modules import _load_native_module, _restore_module_states

_CP_CLASS = "genesis.modules.content_pipeline.module.ContentPipelineModule"


class TestLoaderPropagatesEnabled:
    def test_enabled_true_reaches_instance(self):
        mod = _load_native_module(
            {"class": _CP_CLASS, "name": "content_pipeline", "enabled": True},
            "content-pipeline.yaml",
        )
        assert mod is not None
        assert mod.enabled is True

    def test_enabled_false_reaches_instance(self):
        mod = _load_native_module(
            {"class": _CP_CLASS, "name": "content_pipeline", "enabled": False},
            "content-pipeline.yaml",
        )
        assert mod is not None
        assert mod.enabled is False

    def test_missing_enabled_keeps_class_default(self):
        # No `enabled` key -> loader leaves the __init__ default untouched
        # (ContentPipelineModule defaults to disabled).
        mod = _load_native_module(
            {"class": _CP_CLASS, "name": "content_pipeline"},
            "content-pipeline.yaml",
        )
        assert mod is not None
        assert mod.enabled is False

    def test_non_bool_enabled_ignored(self):
        # Defensive: a malformed YAML value must not crash or coerce weirdly.
        mod = _load_native_module(
            {"class": _CP_CLASS, "name": "content_pipeline", "enabled": "yes"},
            "content-pipeline.yaml",
        )
        assert mod is not None
        assert mod.enabled is False  # unchanged from __init__ default


@pytest.fixture
async def module_db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE module_config (
            module_name TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()
    try:
        yield db
    finally:
        await db.close()


def _rt_with(db, module):
    """Minimal GenesisRuntime stand-in exposing what _restore_module_states reads."""
    registry = MagicMock()
    registry.list_modules.return_value = [module.name]
    registry.get.return_value = module
    return MagicMock(_db=db, _event_bus=AsyncMock(), _module_registry=registry)


class TestSeedPreservesLoaderEnabled:
    async def test_seed_persists_enabled_native(self, module_db):
        # A native loaded with enabled=True (no _config attr, like every native)
        # must seed ENABLED — not be clobbered to False.
        module = SimpleNamespace(name="content_pipeline", enabled=True)
        await _restore_module_states(_rt_with(module_db, module))

        from genesis.modules.persistence import load_all_module_states

        states = await load_all_module_states(module_db)
        assert states["content_pipeline"]["enabled"] is True

    async def test_seed_persists_disabled_native(self, module_db):
        module = SimpleNamespace(name="prediction_markets", enabled=False)
        await _restore_module_states(_rt_with(module_db, module))

        from genesis.modules.persistence import load_all_module_states

        states = await load_all_module_states(module_db)
        assert states["prediction_markets"]["enabled"] is False

    async def test_external_config_enabled_still_wins(self, module_db):
        # External modules carry a ProgramConfig (`_config.enabled`) — that branch
        # must still drive the seed, unchanged by this fix.
        module = SimpleNamespace(
            name="ext_mod", enabled=False, _config=SimpleNamespace(enabled=True)
        )
        await _restore_module_states(_rt_with(module_db, module))

        from genesis.modules.persistence import load_all_module_states

        states = await load_all_module_states(module_db)
        assert states["ext_mod"]["enabled"] is True
