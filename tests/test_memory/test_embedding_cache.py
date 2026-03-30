"""Tests for EmbeddingProvider two-level cache (L1 dict + L2 diskcache)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from genesis.memory.embeddings import EmbeddingProvider, OllamaBackend


def _make_provider(cache_dir=None):
    """Create a provider with a fake Ollama backend for testing."""
    backend = OllamaBackend(url="http://fake:11434", model="qwen3-embedding:0.6b-fp16")
    return EmbeddingProvider(backends=[backend], cache_dir=cache_dir)


@pytest.fixture
def provider():
    """Provider with L2 disabled (no diskcache in tests by default)."""
    return _make_provider(cache_dir=None)


@pytest.fixture
def provider_with_l2(tmp_path):
    """Provider with L2 diskcache enabled via tmp directory."""
    return _make_provider(cache_dir=tmp_path / "embed_cache")


class TestL1Cache:
    def test_cache_miss_returns_none(self, provider: EmbeddingProvider):
        assert provider._cache_get("hello") is None

    def test_cache_put_and_get(self, provider: EmbeddingProvider):
        vec = [1.0, 2.0, 3.0]
        provider._cache_put("hello", vec)
        assert provider._cache_get("hello") == vec

    def test_cache_ttl_expiry(self, provider: EmbeddingProvider):
        vec = [1.0, 2.0]
        provider._cache_put("hello", vec)
        # Manually expire (TTL is now 24h = 86400s)
        key = provider._cache_key("hello")
        provider._cache[key] = (vec, time.monotonic() - 90000)
        assert provider._cache_get("hello") is None

    def test_cache_size_eviction(self, provider: EmbeddingProvider):
        provider._cache_max = 3
        for i in range(4):
            provider._cache_put(f"text_{i}", [float(i)])
        # Should have evicted the oldest, keeping 3
        assert len(provider._cache) == 3

    @pytest.mark.asyncio
    async def test_embed_caches_result(self, provider: EmbeddingProvider):
        fake_vec = [0.1] * 1024
        with patch.object(
            provider, "_embed_remote", new_callable=AsyncMock, return_value=fake_vec,
        ) as mock_remote:
            v1 = await provider.embed("test text")
            v2 = await provider.embed("test text")
            assert v1 == fake_vec
            assert v2 == fake_vec
            mock_remote.assert_called_once()  # second call served from cache


class TestCacheKeyConsistency:
    def test_same_text_same_key(self):
        """All providers using qwen3-embedding produce the same cache key for the same text."""
        p1 = _make_provider()
        p2 = _make_provider()
        assert p1._cache_key("hello") == p2._cache_key("hello")

    def test_different_text_different_key(self):
        p = _make_provider()
        assert p._cache_key("hello") != p._cache_key("world")


class TestL2DiskCache:
    def test_l2_disabled_when_cache_dir_none(self, provider: EmbeddingProvider):
        assert provider._disk_cache is None

    def test_l2_enabled_when_cache_dir_set(self, provider_with_l2: EmbeddingProvider):
        assert provider_with_l2._disk_cache is not None

    def test_l1_miss_l2_hit(self, provider_with_l2: EmbeddingProvider):
        vec = [1.0, 2.0, 3.0]
        # Write directly to L2 only
        key = provider_with_l2._cache_key("hello")
        provider_with_l2._disk_cache.set(key, vec, expire=3600)
        # L1 is empty, but _cache_get should find it in L2
        result = provider_with_l2._cache_get("hello")
        assert result == vec
        assert provider_with_l2._l2_hits == 1
        # Should also be promoted to L1
        assert key in provider_with_l2._cache

    def test_cache_put_writes_both_levels(self, provider_with_l2: EmbeddingProvider):
        vec = [4.0, 5.0, 6.0]
        provider_with_l2._cache_put("world", vec)
        key = provider_with_l2._cache_key("world")
        # L1
        assert key in provider_with_l2._cache
        # L2
        assert provider_with_l2._disk_cache.get(key) == vec

    @pytest.mark.asyncio
    async def test_full_flow_l1_miss_l2_miss_remote(self, provider_with_l2: EmbeddingProvider):
        fake_vec = [0.5] * 1024
        with patch.object(
            provider_with_l2, "_embed_remote", new_callable=AsyncMock, return_value=fake_vec,
        ) as mock_remote:
            result = await provider_with_l2.embed("new text")
            assert result == fake_vec
            mock_remote.assert_called_once()
            # Both L1 and L2 should now have it
            key = provider_with_l2._cache_key("new text")
            assert key in provider_with_l2._cache
            assert provider_with_l2._disk_cache.get(key) == fake_vec

    @pytest.mark.asyncio
    async def test_cross_process_sharing(self, tmp_path):
        """Two EmbeddingProvider instances sharing the same cache_dir see each other's entries."""
        cache_dir = tmp_path / "shared"
        p1 = _make_provider(cache_dir=cache_dir)
        p2 = _make_provider(cache_dir=cache_dir)
        vec = [9.0, 8.0, 7.0]
        # p1 writes
        p1._cache_put("shared_text", vec)
        # p2 reads (L1 miss, L2 hit)
        result = p2._cache_get("shared_text")
        assert result == vec
        assert p2._l2_hits == 1


class TestCacheStats:
    def test_initial_stats(self, provider: EmbeddingProvider):
        stats = provider.cache_stats()
        assert stats == {
            "l1_size": 0,
            "l2_size": 0,
            "l1_hits": 0,
            "l2_hits": 0,
            "misses": 0,
            "remote_calls": 0,
        }

    def test_stats_after_operations(self, provider: EmbeddingProvider):
        provider._cache_put("a", [1.0])
        provider._cache_get("a")  # L1 hit
        provider._cache_get("nonexistent")  # miss
        stats = provider.cache_stats()
        assert stats["l1_size"] == 1
        assert stats["l1_hits"] == 1
        assert stats["misses"] == 1

    @pytest.mark.asyncio
    async def test_remote_call_counter(self):
        """Remote call counter increments when a backend is called."""
        fake_vec = [0.1] * 1024
        b = AsyncMock()
        b.name = "test"
        b.embed = AsyncMock(return_value=fake_vec)
        p = EmbeddingProvider(backends=[b], cache_dir=None)
        await p.embed("unique_text")
        assert p.cache_stats()["remote_calls"] == 1


class TestBackendChain:
    @pytest.mark.asyncio
    async def test_first_backend_succeeds(self):
        """First backend in chain succeeds — no fallback."""
        b1 = AsyncMock()
        b1.name = "primary"
        b1.embed = AsyncMock(return_value=[1.0] * 1024)
        b2 = AsyncMock()
        b2.name = "fallback"

        p = EmbeddingProvider(backends=[b1, b2], cache_dir=None)
        vec = await p.embed("test")
        assert vec == [1.0] * 1024
        b1.embed.assert_called_once()
        b2.embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self):
        """Primary fails, fallback succeeds."""
        b1 = AsyncMock()
        b1.name = "primary"
        b1.embed = AsyncMock(side_effect=Exception("down"))
        b2 = AsyncMock()
        b2.name = "fallback"
        b2.embed = AsyncMock(return_value=[2.0] * 1024)

        p = EmbeddingProvider(backends=[b1, b2], cache_dir=None)
        vec = await p.embed("test")
        assert vec == [2.0] * 1024

    @pytest.mark.asyncio
    async def test_all_backends_fail(self):
        """All backends fail — raises EmbeddingUnavailableError."""
        from genesis.memory.embeddings import EmbeddingUnavailableError

        b1 = AsyncMock()
        b1.name = "b1"
        b1.embed = AsyncMock(side_effect=Exception("down"))
        b2 = AsyncMock()
        b2.name = "b2"
        b2.embed = AsyncMock(side_effect=Exception("also down"))

        p = EmbeddingProvider(backends=[b1, b2], cache_dir=None)
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("test")

    @pytest.mark.asyncio
    async def test_no_backends_raises(self):
        """Empty backend chain raises EmbeddingUnavailableError."""
        from genesis.memory.embeddings import EmbeddingUnavailableError

        p = EmbeddingProvider(backends=[], cache_dir=None)
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("test")
