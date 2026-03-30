"""Health probe providers — wraps genesis.observability.health probes."""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)


@runtime_checkable
class HealthProbeProvider(ToolProvider, Protocol):
    """Specialized ToolProvider for health probes."""

    async def probe(self) -> dict: ...


class QdrantProbeAdapter:
    """Wraps probe_qdrant as a ToolProvider."""

    name = "qdrant_probe"
    capability = ProviderCapability(
        categories=(ProviderCategory.HEALTH,),
        cost_tier=CostTier.FREE,
        description="Qdrant vector DB health probe",
    )

    async def check_health(self) -> ProviderStatus:
        result = await self.probe()
        status = result.get("status", "down")
        if status == "healthy":
            return ProviderStatus.AVAILABLE
        if status == "degraded":
            return ProviderStatus.DEGRADED
        return ProviderStatus.UNAVAILABLE

    async def probe(self) -> dict:
        from genesis.observability.health import probe_qdrant
        result = await probe_qdrant()
        return {"status": str(result.status), "latency_ms": result.latency_ms, "message": result.message}

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        data = await self.probe()
        latency = (time.monotonic() - start) * 1000
        return ProviderResult(
            success=data.get("status") != "down",
            data=data,
            latency_ms=round(latency, 2),
            provider_name=self.name,
        )


class OllamaProbeAdapter:
    """Wraps probe_ollama as a ToolProvider."""

    name = "ollama_probe"
    capability = ProviderCapability(
        categories=(ProviderCategory.HEALTH,),
        cost_tier=CostTier.FREE,
        description="Ollama SLM health probe",
    )

    async def check_health(self) -> ProviderStatus:
        result = await self.probe()
        status = result.get("status", "down")
        if status == "healthy":
            return ProviderStatus.AVAILABLE
        if status == "degraded":
            return ProviderStatus.DEGRADED
        return ProviderStatus.UNAVAILABLE

    async def probe(self) -> dict:
        from genesis.observability.health import probe_ollama
        result = await probe_ollama()
        return {"status": str(result.status), "latency_ms": result.latency_ms, "message": result.message}

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        data = await self.probe()
        latency = (time.monotonic() - start) * 1000
        return ProviderResult(
            success=data.get("status") != "down",
            data=data,
            latency_ms=round(latency, 2),
            provider_name=self.name,
        )
