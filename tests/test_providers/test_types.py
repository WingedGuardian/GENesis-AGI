"""Tests for genesis.providers.types."""

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderInfo,
    ProviderResult,
    ProviderStatus,
)


class TestEnums:
    def test_provider_category_values(self):
        assert ProviderCategory.SEARCH == "search"
        assert ProviderCategory.STT == "stt"
        assert ProviderCategory.EMBEDDING == "embedding"
        assert ProviderCategory.WEB == "web"
        assert ProviderCategory.ANALYSIS == "analysis"
        assert ProviderCategory.EXTRACTION == "extraction"
        assert ProviderCategory.HEALTH == "health"

    def test_cost_tier_values(self):
        assert CostTier.FREE == "free"
        assert CostTier.CHEAP == "cheap"
        assert CostTier.MODERATE == "moderate"
        assert CostTier.EXPENSIVE == "expensive"

    def test_provider_status_values(self):
        assert ProviderStatus.AVAILABLE == "available"
        assert ProviderStatus.DEGRADED == "degraded"
        assert ProviderStatus.UNAVAILABLE == "unavailable"

    def test_cost_tier_matches_db_check(self):
        """CostTier values must match the tool_registry CHECK constraint."""
        db_values = {"free", "cheap", "moderate", "expensive"}
        enum_values = {t.value for t in CostTier}
        assert enum_values == db_values


class TestProviderCapability:
    def test_frozen(self):
        cap = ProviderCapability(
            content_types=("web_page",),
            categories=(ProviderCategory.SEARCH,),
            cost_tier=CostTier.FREE,
            description="Test",
        )
        assert cap.content_types == ("web_page",)
        assert cap.cost_tier == CostTier.FREE

    def test_defaults(self):
        cap = ProviderCapability()
        assert cap.content_types == ()
        assert cap.categories == ()
        assert cap.cost_tier == CostTier.FREE
        assert cap.description == ""


class TestProviderResult:
    def test_success_result(self):
        r = ProviderResult(success=True, data={"key": "val"}, provider_name="test")
        assert r.success
        assert r.data == {"key": "val"}
        assert r.error is None

    def test_error_result(self):
        r = ProviderResult(success=False, error="timeout", provider_name="test")
        assert not r.success
        assert r.error == "timeout"


class TestProviderInfo:
    def test_snapshot(self):
        cap = ProviderCapability(description="search engine")
        info = ProviderInfo(name="web_search", capability=cap)
        assert info.name == "web_search"
        assert info.status == ProviderStatus.AVAILABLE
        assert info.invocation_count == 0
