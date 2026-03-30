"""Tests for genesis.modules.base — CapabilityModule protocol."""

from __future__ import annotations

from typing import Any

from genesis.modules.base import CapabilityModule


class ConcreteModule:
    """Minimal concrete implementation satisfying CapabilityModule."""

    def __init__(self, name: str = "test", enabled: bool = True) -> None:
        self._name = name
        self._enabled = enabled

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def register(self, runtime: Any) -> None:
        pass

    async def deregister(self) -> None:
        pass

    def get_research_profile_name(self) -> str | None:
        return None

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        return None

    async def record_outcome(self, outcome: dict) -> None:
        pass

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        return None

    def configurable_fields(self) -> list[dict]:
        return []

    def update_config(self, updates: dict) -> dict:
        return {}


class TestCapabilityModuleProtocol:
    def test_concrete_class_satisfies_protocol(self):
        mod = ConcreteModule()
        assert isinstance(mod, CapabilityModule)

    def test_isinstance_detection(self):
        """runtime_checkable protocol works with isinstance."""
        assert isinstance(ConcreteModule(), CapabilityModule)
        assert not isinstance("not a module", CapabilityModule)

    def test_properties_accessible(self):
        mod = ConcreteModule(name="crypto", enabled=False)
        assert mod.name == "crypto"
        assert mod.enabled is False

    def test_get_research_profile_name(self):
        mod = ConcreteModule()
        assert mod.get_research_profile_name() is None
