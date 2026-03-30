"""Tests for ModuleRegistry integration with GenesisRuntime."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.modules.registry import ModuleRegistry


class TestModuleRegistryLifecycle:
    @pytest.mark.asyncio()
    async def test_load_and_list(self):
        registry = ModuleRegistry()
        mod = MagicMock()
        mod.name = "test_mod"
        mod.enabled = True
        mod.register = AsyncMock()

        registry.set_runtime(MagicMock())
        await registry.load_module(mod)

        assert "test_mod" in registry.list_modules()
        assert "test_mod" in registry.list_enabled()
        mod.register.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_unload(self):
        registry = ModuleRegistry()
        mod = MagicMock()
        mod.name = "test_mod"
        mod.enabled = True
        mod.register = AsyncMock()
        mod.deregister = AsyncMock()

        await registry.load_module(mod)
        await registry.unload_module("test_mod")

        assert "test_mod" not in registry.list_modules()
        mod.deregister.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_unload_all(self):
        registry = ModuleRegistry()
        for name in ["a", "b", "c"]:
            mod = MagicMock()
            mod.name = name
            mod.enabled = True
            mod.register = AsyncMock()
            mod.deregister = AsyncMock()
            await registry.load_module(mod)

        assert len(registry.list_modules()) == 3
        await registry.unload_all()
        assert len(registry.list_modules()) == 0

    @pytest.mark.asyncio()
    async def test_get_returns_none_for_unknown(self):
        registry = ModuleRegistry()
        assert registry.get("nonexistent") is None

    @pytest.mark.asyncio()
    async def test_list_enabled_filters_disabled(self):
        registry = ModuleRegistry()
        enabled_mod = MagicMock()
        enabled_mod.name = "enabled"
        enabled_mod.enabled = True
        enabled_mod.register = AsyncMock()

        disabled_mod = MagicMock()
        disabled_mod.name = "disabled"
        disabled_mod.enabled = False
        disabled_mod.register = AsyncMock()

        await registry.load_module(enabled_mod)
        await registry.load_module(disabled_mod)

        assert "enabled" in registry.list_enabled()
        assert "disabled" not in registry.list_enabled()


class TestRuntimeModuleRegistryProperty:
    def test_module_registry_property_exists(self):
        """Verify the runtime has a module_registry property."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime()
        assert hasattr(rt, "module_registry")
        assert rt.module_registry is None  # Before bootstrap

    def test_init_checks_includes_modules(self):
        """Verify modules is in the bootstrap init checks map."""
        from genesis.runtime import GenesisRuntime

        assert "modules" in GenesisRuntime._INIT_CHECKS
        assert GenesisRuntime._INIT_CHECKS["modules"] == "_module_registry"
