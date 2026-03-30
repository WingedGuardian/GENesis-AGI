"""Tests for genesis.providers.registry."""

import pytest

from genesis.providers.registry import ProviderRegistry
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)


class FakeProvider:
    def __init__(self, name, categories=(), content_types=(), cost_tier=CostTier.FREE):
        self.name = name
        self.capability = ProviderCapability(
            content_types=content_types,
            categories=categories,
            cost_tier=cost_tier,
            description=f"Fake {name}",
        )

    async def check_health(self) -> ProviderStatus:
        return ProviderStatus.AVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        return ProviderResult(success=True, provider_name=self.name)


class TestRegistration:
    def test_register_and_get(self):
        reg = ProviderRegistry()
        p = FakeProvider("test")
        reg.register(p)
        assert reg.get("test") is p

    def test_get_missing(self):
        reg = ProviderRegistry()
        assert reg.get("nope") is None

    def test_unregister(self):
        reg = ProviderRegistry()
        reg.register(FakeProvider("x"))
        assert reg.unregister("x")
        assert reg.get("x") is None

    def test_unregister_missing(self):
        reg = ProviderRegistry()
        assert not reg.unregister("nope")

    def test_list_all(self):
        reg = ProviderRegistry()
        reg.register(FakeProvider("a"))
        reg.register(FakeProvider("b"))
        assert len(reg.list_all()) == 2


class TestCategoryLookup:
    def test_list_by_category(self):
        reg = ProviderRegistry()
        reg.register(FakeProvider("s1", categories=(ProviderCategory.SEARCH,)))
        reg.register(FakeProvider("s2", categories=(ProviderCategory.SEARCH,)))
        reg.register(FakeProvider("e1", categories=(ProviderCategory.EMBEDDING,)))
        results = reg.list_by_category(ProviderCategory.SEARCH)
        assert len(results) == 2
        names = {p.name for p in results}
        assert names == {"s1", "s2"}

    def test_list_by_category_empty(self):
        reg = ProviderRegistry()
        assert reg.list_by_category(ProviderCategory.STT) == []


class TestContentTypeRouting:
    def test_route_by_content_type(self):
        reg = ProviderRegistry()
        reg.register(FakeProvider("a", content_types=("web_page",), cost_tier=CostTier.CHEAP))
        reg.register(FakeProvider("b", content_types=("web_page",), cost_tier=CostTier.FREE))
        reg.register(FakeProvider("c", content_types=("pdf",), cost_tier=CostTier.FREE))

        results = reg.route_by_content_type("web_page")
        assert len(results) == 2
        # Cheapest first
        assert results[0].name == "b"
        assert results[1].name == "a"

    def test_route_no_match(self):
        reg = ProviderRegistry()
        assert reg.route_by_content_type("video") == []


class TestInfo:
    def test_info_returns_snapshot(self):
        reg = ProviderRegistry()
        reg.register(FakeProvider("x", categories=(ProviderCategory.HEALTH,)))
        info = reg.info("x")
        assert info is not None
        assert info.name == "x"
        assert ProviderCategory.HEALTH in info.capability.categories

    def test_info_missing(self):
        reg = ProviderRegistry()
        assert reg.info("nope") is None


class TestDBSync:
    @pytest.mark.asyncio
    async def test_record_invocation_no_db(self):
        """No DB = no-op, no error."""
        reg = ProviderRegistry(db=None)
        await reg.record_invocation("test")

    @pytest.mark.asyncio
    async def test_record_gap_no_db(self):
        reg = ProviderRegistry(db=None)
        result = await reg.record_gap("video", ["none"])
        assert result is None

    @pytest.mark.asyncio
    async def test_register_and_sync_no_db(self):
        """No DB = register in memory only, no error."""
        reg = ProviderRegistry(db=None)
        p = FakeProvider("x")
        await reg.register_and_sync(p)
        assert reg.get("x") is p
