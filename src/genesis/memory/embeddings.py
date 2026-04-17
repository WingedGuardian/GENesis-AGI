"""Embedding provider with configurable backend chains.

Two chain configurations for split read/write paths:
  Storage (writes): Ollama → DeepInfra → DashScope (cost-optimized, local first)
  Recall (reads):   DeepInfra → DashScope → Ollama (latency-optimized, cloud first)

All backends use qwen3-embedding at 1024 dimensions for vector space
compatibility. Cache keys are text-based (SHA256 of "qwen3-embedding:{text}"),
NOT provider-dependent — two instances sharing the same L2 diskcache dir
see each other's entries.

Two-level cache: L1 in-process dict (fast, per-process) backed by
L2 diskcache on disk (shared across all MCP server processes).
Embeddings are deterministic for a given model+text, so long TTLs are safe.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.provider_activity import ProviderActivityTracker

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".genesis" / "embedding_cache"

_HTTPX_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.HTTPStatusError,
)


class EmbeddingUnavailableError(Exception):
    """Raised when all embedding backends are unavailable."""


class EmbeddingBackend(Protocol):
    """Protocol for embedding backends in the provider chain."""

    @property
    def name(self) -> str: ...
    async def embed(self, text: str) -> list[float]: ...
    async def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


class OllamaBackend:
    """Local Ollama embedding backend (qwen3-embedding, fp16 recommended).

    Uses a 60s timeout (Ollama can be slow under GPU contention or cold model
    loading) and retries once on ReadTimeout before propagating the failure.
    """

    def __init__(
        self,
        url: str,
        model: str = "qwen3-embedding:0.6b-fp16",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=60.0)

    @property
    def name(self) -> str:
        return "ollama_embedding"

    async def embed(self, text: str) -> list[float]:
        last_exc: Exception | None = None
        for attempt in range(2):  # 1 retry on timeout
            try:
                resp = await self._client.post(
                    f"{self._url.rstrip('/')}/api/embed",
                    json={"model": self._model, "input": text, "keep_alive": -1},
                )
                resp.raise_for_status()
                return resp.json()["embeddings"][0]
            except httpx.ReadTimeout as exc:
                last_exc = exc
                if attempt == 0:
                    import asyncio
                    await asyncio.sleep(1.0)  # brief backoff before retry
                    continue
                raise
            except Exception:
                raise
        raise last_exc  # type: ignore[misc]  # unreachable, but satisfies type checker

    async def is_available(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self._url.rstrip('/')}/api/tags", timeout=5.0,
            )
            return resp.status_code == 200
        except _HTTPX_ERRORS:
            return False


class DeepInfraBackend:
    """DeepInfra cloud embedding backend (OpenAI-compatible API)."""

    def __init__(
        self,
        api_key: str,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=30.0)

    @property
    def name(self) -> str:
        return "deepinfra_embedding"

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            "https://api.deepinfra.com/v1/openai/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self._model, "input": [text]},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def is_available(self) -> bool:
        return True  # Cloud API — assume available, let embed() fail if not


class DashScopeBackend:
    """Alibaba DashScope cloud embedding backend (OpenAI-compatible API).

    Uses text-embedding-v4 with explicit dimensions=1024 for vector space
    compatibility. NOTE: text-embedding-v4 may run the 8B variant —
    validate cosine similarity with local 0.6B before trusting as fallback.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._client = client or httpx.AsyncClient(timeout=30.0)

    @property
    def name(self) -> str:
        return "dashscope_embedding"

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "input": [text],
                "dimensions": self._dimensions,
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def is_available(self) -> bool:
        return True  # Cloud API — assume available, let embed() fail if not


# ---------------------------------------------------------------------------
# Main embedding provider
# ---------------------------------------------------------------------------


class EmbeddingProvider:
    """Embedding provider with backend chain and two-level cache.

    Backend chain order: Ollama (if enabled) → DeepInfra → DashScope.
    If all backends fail, raises EmbeddingUnavailableError.
    Caller (MemoryStore) falls to FTS5-only and queues for later embedding.
    """

    def __init__(
        self,
        *,
        backends: list[EmbeddingBackend] | None = None,
        activity_tracker: ProviderActivityTracker | None = None,
        event_bus: GenesisEventBus | None = None,
        cache_dir: Path | None = _DEFAULT_CACHE_DIR,
    ) -> None:
        self._backends = backends if backends is not None else self._build_default_chain()
        self._cache: dict[str, tuple[list[float], float]] = {}
        self._cache_ttl: float = 86400.0  # 24 hours
        self._cache_max: int = 2048
        self._tracker = activity_tracker
        self._event_bus = event_bus

        # Observability counters
        self._l1_hits: int = 0
        self._l2_hits: int = 0
        self._misses: int = 0
        self._remote_calls: int = 0
        self._consecutive_backend_failures: dict[str, int] = {}

        # L2 shared disk cache
        self._disk_cache = None
        if cache_dir is not None:
            try:
                import diskcache
                cache_dir.mkdir(parents=True, exist_ok=True)
                self._disk_cache = diskcache.Cache(
                    str(cache_dir), size_limit=100_000_000,  # 100 MB
                )
            except Exception:
                logger.warning(
                    "Failed to initialize diskcache at %s, using L1-only",
                    cache_dir, exc_info=True,
                )

        backend_names = [b.name for b in self._backends]
        logger.info("Embedding provider initialized: chain=%s", backend_names)

    @staticmethod
    def build_chain(*, ollama_first: bool = True) -> list[EmbeddingBackend]:
        """Build backend chain with configurable priority order.

        Args:
            ollama_first: If True, Ollama leads (storage/write path).
                         If False, cloud leads (recall/read path).
        """
        import os

        from genesis.env import (
            dashscope_api_key,
            deepinfra_api_key,
            ollama_enabled,
            ollama_url,
        )

        ollama_backends: list[EmbeddingBackend] = []
        if ollama_enabled():
            model = os.environ.get(
                "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b-fp16",
            )
            ollama_backends.append(OllamaBackend(url=ollama_url(), model=model))

        cloud_backends: list[EmbeddingBackend] = []
        di_key = deepinfra_api_key()
        if di_key:
            cloud_backends.append(DeepInfraBackend(api_key=di_key))
        ds_key = dashscope_api_key()
        if ds_key:
            cloud_backends.append(DashScopeBackend(api_key=ds_key))

        if ollama_first:
            chain = ollama_backends + cloud_backends
        else:
            chain = cloud_backends + ollama_backends

        if not chain:
            logger.warning(
                "No embedding backends configured. Set GENESIS_ENABLE_OLLAMA=true, "
                "API_KEY_DEEPINFRA, or API_KEY_QWEN in secrets.env."
            )

        return chain

    @staticmethod
    def _build_default_chain() -> list[EmbeddingBackend]:
        """Build default backend chain (Ollama first — storage/write path)."""
        return EmbeddingProvider.build_chain(ollama_first=True)

    @property
    def tracker(self) -> ProviderActivityTracker | None:
        """Activity tracker for recording call metrics."""
        return self._tracker

    # -- Cache layer (unchanged from original) --

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"qwen3-embedding:{text}".encode()).hexdigest()

    def _cache_get(self, text: str) -> list[float] | None:
        key = self._cache_key(text)

        # L1: in-process dict
        entry = self._cache.get(key)
        if entry is not None:
            vec, ts = entry
            if time.monotonic() - ts <= self._cache_ttl:
                self._l1_hits += 1
                return vec
            del self._cache[key]

        # L2: shared diskcache
        if self._disk_cache is not None:
            try:
                vec = self._disk_cache.get(key)
                if vec is not None:
                    self._l2_hits += 1
                    self._l1_put(key, vec)
                    return vec
            except Exception:
                logger.debug("diskcache get failed for key %s", key[:12], exc_info=True)

        self._misses += 1
        return None

    def _l1_put(self, key: str, vec: list[float]) -> None:
        if len(self._cache) >= self._cache_max:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[key] = (vec, time.monotonic())

    def _cache_put(self, text: str, vec: list[float]) -> None:
        key = self._cache_key(text)
        self._l1_put(key, vec)
        if self._disk_cache is not None:
            try:
                self._disk_cache.set(key, vec, expire=604800)
            except Exception:
                logger.debug("diskcache set failed for key %s", key[:12], exc_info=True)

    def cache_stats(self) -> dict:
        return {
            "l1_size": len(self._cache),
            "l2_size": len(self._disk_cache) if self._disk_cache is not None else 0,
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "misses": self._misses,
            "remote_calls": self._remote_calls,
        }

    # -- Public API --

    async def is_available(self) -> bool:
        """Check if at least one embedding backend is reachable."""
        for backend in self._backends:
            try:
                if await backend.is_available():
                    return True
            except Exception:
                continue
        return False

    async def embed(self, text: str) -> list[float]:
        """Embed single text, returns 1024-dim vector."""
        cached = self._cache_get(text)
        if cached is not None:
            if self._tracker:
                self._tracker.record(
                    "embedding", latency_ms=0, success=True, cache_hit=True,
                )
            return cached

        vec = await self._embed_remote(text)
        self._cache_put(text, vec)
        return vec

    async def _embed_remote(self, text: str) -> list[float]:
        """Try each backend in chain order. First success wins."""
        self._remote_calls += 1
        if self._remote_calls % 100 == 0:
            stats = self.cache_stats()
            logger.debug(
                "Embedding cache: L1=%d/%d L2=%d hits=%d+%d misses=%d remote=%d",
                stats["l1_size"], self._cache_max, stats["l2_size"],
                stats["l1_hits"], stats["l2_hits"], stats["misses"],
                stats["remote_calls"],
            )

        errors: list[tuple[str, Exception]] = []

        for backend in self._backends:
            t0 = time.monotonic()
            try:
                vec = await backend.embed(text)
                latency = (time.monotonic() - t0) * 1000
                if self._tracker:
                    self._tracker.record(
                        backend.name, latency_ms=latency, success=True,
                    )
                # Reset failure counter on success
                self._consecutive_backend_failures[backend.name] = 0
                if errors:
                    # Log fallback event if primary failed
                    failed_names = [name for name, _ in errors]
                    details = "; ".join(
                        f"{n}: {type(e).__name__}" + (f" ({e})" if str(e) else "")
                        for n, e in errors
                    )
                    await self._emit_embedding_event(
                        "embedding.fallback",
                        f"{'→'.join(failed_names)}→{backend.name} fallback ({details})",
                        "warning",
                    )
                return vec
            except Exception as exc:
                latency = (time.monotonic() - t0) * 1000
                if self._tracker:
                    self._tracker.record(
                        backend.name, latency_ms=latency, success=False,
                    )
                fails = self._consecutive_backend_failures.get(backend.name, 0) + 1
                self._consecutive_backend_failures[backend.name] = fails
                # Log full traceback only on first few failures; suppress spam
                # after repeated failures from the same backend (portability).
                exc_desc = f"{type(exc).__name__}" + (f": {exc}" if str(exc) else " (no details)")
                if fails <= 3:
                    logger.warning(
                        "Embedding backend '%s' failed (%d consecutive): %s",
                        backend.name, fails, exc_desc, exc_info=True,
                    )
                elif fails % 50 == 0:
                    logger.warning(
                        "Embedding backend '%s' still failing (%d consecutive): %s",
                        backend.name, fails, exc_desc,
                    )
                else:
                    logger.debug(
                        "Embedding backend '%s' failed (%d consecutive): %s",
                        backend.name, fails, exc_desc,
                    )
                errors.append((backend.name, exc))

        # All backends failed
        failed_names = [name for name, _ in errors]
        await self._emit_embedding_event(
            "embedding.failed",
            f"All embedding backends failed: {', '.join(failed_names)}",
            "error",
        )
        msg = f"All embedding backends failed: {failed_names}"
        raise EmbeddingUnavailableError(msg)

    async def _emit_embedding_event(
        self, event_type: str, message: str, severity: str,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from genesis.observability.types import Severity, Subsystem

            sev = Severity(severity)
            await self._event_bus.emit(
                Subsystem.PROVIDERS, sev, event_type, message,
            )
        except Exception:
            logger.debug("Failed to emit embedding event", exc_info=True)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        return [await self.embed(t) for t in texts]

    @staticmethod
    def enrich(content: str, memory_type: str, tags: list[str]) -> str:
        """Contextual enrichment: prepend type and tags before embedding."""
        if tags:
            return f"{memory_type}: {' '.join(tags)}: {content}"
        return f"{memory_type}: {content}"
