"""Tests for genesis.modules.registry — ModuleRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.modules.registry import ModuleRegistry


class MockModule:
    """Concrete mock module for registry tests."""

    def __init__(
        self, name: str = "mock", enabled: bool = True
    ) -> None:
        self._name = name
        self._enabled = enabled
        self.register = AsyncMock()
        self.deregister = AsyncMock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_research_profile_name(self) -> str | None:
        return None

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        return None

    async def record_outcome(self, outcome: dict) -> None:
        pass

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        return None


class TestModuleRegistry:
    async def test_load_module_adds_to_registry(self):
        reg = ModuleRegistry()
        mod = MockModule("alpha")
        await reg.load_module(mod)
        assert reg.get("alpha") is mod

    async def test_load_module_skips_duplicate(self):
        reg = ModuleRegistry()
        mod1 = MockModule("alpha")
        mod2 = MockModule("alpha")
        await reg.load_module(mod1)
        await reg.load_module(mod2)
        assert reg.get("alpha") is mod1

    async def test_unload_module_calls_deregister_and_removes(self):
        reg = ModuleRegistry()
        mod = MockModule("alpha")
        await reg.load_module(mod)
        await reg.unload_module("alpha")
        mod.deregister.assert_awaited_once()
        assert reg.get("alpha") is None

    async def test_unload_module_unknown_name_is_safe(self):
        reg = ModuleRegistry()
        await reg.unload_module("nonexistent")  # should not raise

    async def test_get_returns_module_or_none(self):
        reg = ModuleRegistry()
        assert reg.get("nope") is None
        mod = MockModule("found")
        await reg.load_module(mod)
        assert reg.get("found") is mod

    async def test_list_modules_returns_names(self):
        reg = ModuleRegistry()
        await reg.load_module(MockModule("a"))
        await reg.load_module(MockModule("b"))
        assert sorted(reg.list_modules()) == ["a", "b"]

    async def test_list_enabled_filters_by_enabled(self):
        reg = ModuleRegistry()
        await reg.load_module(MockModule("on", enabled=True))
        await reg.load_module(MockModule("off", enabled=False))
        assert reg.list_enabled() == ["on"]

    async def test_unload_all_clears_everything(self):
        reg = ModuleRegistry()
        m1 = MockModule("a")
        m2 = MockModule("b")
        await reg.load_module(m1)
        await reg.load_module(m2)
        await reg.unload_all()
        assert reg.list_modules() == []
        m1.deregister.assert_awaited_once()
        m2.deregister.assert_awaited_once()

    async def test_load_module_calls_register_with_runtime(self):
        reg = ModuleRegistry()
        runtime = object()
        reg.set_runtime(runtime)
        mod = MockModule("alpha")
        await reg.load_module(mod)
        mod.register.assert_awaited_once_with(runtime)

    async def test_load_module_without_runtime_skips_register(self):
        reg = ModuleRegistry()
        mod = MockModule("alpha")
        await reg.load_module(mod)
        mod.register.assert_not_awaited()
