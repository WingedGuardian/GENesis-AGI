"""Tests for genesis.providers.protocol."""

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)


class FakeProvider:
    """Concrete implementation for protocol tests."""

    name = "fake"
    capability = ProviderCapability(
        content_types=("test",),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.FREE,
        description="Fake provider for tests",
    )

    async def check_health(self) -> ProviderStatus:
        return ProviderStatus.AVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        return ProviderResult(success=True, data=request, provider_name=self.name)


class IncompleteProvider:
    """Missing invoke method."""

    name = "broken"
    capability = ProviderCapability()

    async def check_health(self) -> ProviderStatus:
        return ProviderStatus.AVAILABLE


class TestToolProviderProtocol:
    def test_runtime_checkable(self):
        assert isinstance(FakeProvider(), ToolProvider)

    def test_incomplete_not_instance(self):
        assert not isinstance(IncompleteProvider(), ToolProvider)

    @pytest.mark.asyncio
    async def test_invoke(self):
        p = FakeProvider()
        result = await p.invoke({"q": "test"})
        assert result.success
        assert result.data == {"q": "test"}

    @pytest.mark.asyncio
    async def test_health_check(self):
        p = FakeProvider()
        status = await p.check_health()
        assert status == ProviderStatus.AVAILABLE
