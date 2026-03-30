"""ToolProvider protocol — the universal interface for external capabilities."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genesis.providers.types import ProviderCapability, ProviderResult, ProviderStatus


@runtime_checkable
class ToolProvider(Protocol):
    """Any external tool/service that Genesis can invoke.

    Implementations must define ``name`` and ``capability`` as class or instance
    attributes and implement the two async methods.
    """

    name: str
    capability: ProviderCapability

    async def check_health(self) -> ProviderStatus: ...

    async def invoke(self, request: dict) -> ProviderResult: ...
