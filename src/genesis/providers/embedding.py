"""Embedding provider protocol and adapters.

Wraps genesis.memory.embeddings as ToolProviders for the provider registry.
"""

from __future__ import annotations

import logging
import os
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

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProviderProtocol(ToolProvider, Protocol):
    """Specialized ToolProvider for text embeddings."""

    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbeddingAdapter:
    """Ollama embedding as a ToolProvider."""

    name = "ollama_embedding"
    capability = ProviderCapability(
        content_types=("text",),
        categories=(ProviderCategory.EMBEDDING,),
        cost_tier=CostTier.FREE,
        description="Local embeddings via Ollama (qwen3-embedding)",
    )

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
    ) -> None:
        from genesis.env import ollama_url

        self._url = url or ollama_url()
        self._model = model or os.environ.get(
            "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b-fp16",
        )
        self._provider = None

    def _get_provider(self):
        if self._provider is None:
            from genesis.memory.embeddings import EmbeddingProvider, OllamaBackend
            self._provider = EmbeddingProvider(
                backends=[OllamaBackend(url=self._url, model=self._model)],
                cache_dir=None,
            )
        return self._provider

    async def check_health(self) -> ProviderStatus:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._url}/api/tags")
                if resp.status_code == 200:
                    return ProviderStatus.AVAILABLE
            return ProviderStatus.DEGRADED
        except Exception:
            return ProviderStatus.UNAVAILABLE

    async def embed(self, text: str) -> list[float]:
        return await self._get_provider().embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._get_provider().embed_batch(texts)

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            text = request.get("text", "")
            vec = await self.embed(text)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=True,
                data=vec,
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )


class CloudEmbeddingAdapter:
    """Cloud embedding (DeepInfra/DashScope) as a ToolProvider."""

    capability = ProviderCapability(
        content_types=("text",),
        categories=(ProviderCategory.EMBEDDING,),
        cost_tier=CostTier.CHEAP,
        description="Cloud embeddings via DeepInfra/DashScope (qwen3-embedding)",
    )

    def __init__(self, provider: str = "deepinfra") -> None:
        self._provider_name = provider
        self.name = f"{provider}_embedding"
        self._embed_provider = None

    def _get_provider(self):
        if self._embed_provider is None:
            from genesis.memory.embeddings import (
                DashScopeBackend,
                DeepInfraBackend,
                EmbeddingProvider,
            )

            backends = []
            if self._provider_name == "deepinfra":
                key = os.environ.get("API_KEY_DEEPINFRA", "").strip()
                if key:
                    backends.append(DeepInfraBackend(api_key=key))
            elif self._provider_name == "dashscope":
                key = os.environ.get("API_KEY_QWEN", "").strip()
                if key:
                    backends.append(DashScopeBackend(api_key=key))

            self._embed_provider = EmbeddingProvider(
                backends=backends, cache_dir=None,
            )
        return self._embed_provider

    async def check_health(self) -> ProviderStatus:
        if self._provider_name == "deepinfra" and os.environ.get("API_KEY_DEEPINFRA"):
            return ProviderStatus.AVAILABLE
        if self._provider_name == "dashscope" and os.environ.get("API_KEY_QWEN"):
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def embed(self, text: str) -> list[float]:
        return await self._get_provider().embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._get_provider().embed_batch(texts)

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            text = request.get("text", "")
            vec = await self.embed(text)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=True, data=vec,
                latency_ms=round(latency, 2), provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False, error=str(exc),
                latency_ms=round(latency, 2), provider_name=self.name,
            )
