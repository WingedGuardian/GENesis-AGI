"""Tests for genesis.memory.embeddings — backend chain architecture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from genesis.memory.embeddings import (
    DashScopeBackend,
    DeepInfraBackend,
    EmbeddingProvider,
    EmbeddingUnavailableError,
    OllamaBackend,
)


def _ok_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


VEC_1024 = [0.1] * 1024


class TestEnrich:
    def test_enrich_with_tags(self) -> None:
        result = EmbeddingProvider.enrich("hello", "observation", ["tag1", "tag2"])
        assert result == "observation: tag1 tag2: hello"

    def test_enrich_without_tags(self) -> None:
        result = EmbeddingProvider.enrich("hello", "observation", [])
        assert result == "observation: hello"


class TestOllamaBackend:
    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"embeddings": [VEC_1024]}))
        backend = OllamaBackend(url="http://fake:11434", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        call_url = client.post.call_args[0][0]
        assert "/api/embed" in call_url

    @pytest.mark.asyncio
    async def test_embed_failure_raises(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend = OllamaBackend(url="http://fake:11434", client=client)
        with pytest.raises(httpx.ConnectError):
            await backend.embed("test")

    @pytest.mark.asyncio
    async def test_is_available_true(self) -> None:
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        client.get = AsyncMock(return_value=resp)
        backend = OllamaBackend(url="http://fake:11434", client=client)
        assert await backend.is_available() is True

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend = OllamaBackend(url="http://fake:11434", client=client)
        assert await backend.is_available() is False


class TestDeepInfraBackend:
    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(
            return_value=_ok_response({"data": [{"embedding": VEC_1024}]})
        )
        backend = DeepInfraBackend(api_key="test-key", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        call_url = client.post.call_args[0][0]
        assert "deepinfra" in call_url

    @pytest.mark.asyncio
    async def test_auth_header(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(
            return_value=_ok_response({"data": [{"embedding": VEC_1024}]})
        )
        backend = DeepInfraBackend(api_key="my-secret", client=client)
        await backend.embed("test")
        headers = client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-secret"


class TestDashScopeBackend:
    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(
            return_value=_ok_response({"data": [{"embedding": VEC_1024}]})
        )
        backend = DashScopeBackend(api_key="test-key", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        call_url = client.post.call_args[0][0]
        assert "dashscope" in call_url

    @pytest.mark.asyncio
    async def test_dimensions_param(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(
            return_value=_ok_response({"data": [{"embedding": VEC_1024}]})
        )
        backend = DashScopeBackend(api_key="key", dimensions=1024, client=client)
        await backend.embed("test")
        body = client.post.call_args[1]["json"]
        assert body["dimensions"] == 1024


class TestEmbedProviderChain:
    @pytest.mark.asyncio
    async def test_ollama_primary_succeeds(self) -> None:
        """Ollama succeeds — no cloud fallback."""
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"embeddings": [VEC_1024]}))
        ollama = OllamaBackend(url="http://fake:11434", client=client)
        deepinfra = AsyncMock()
        deepinfra.name = "deepinfra_embedding"

        p = EmbeddingProvider(backends=[ollama, deepinfra], cache_dir=None)
        result = await p.embed("test")
        assert result == VEC_1024
        deepinfra.embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_ollama_fails_deepinfra_succeeds(self) -> None:
        """Ollama down → falls to DeepInfra."""
        ollama_client = MagicMock()
        ollama_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        ollama = OllamaBackend(url="http://fake:11434", client=ollama_client)

        deepinfra_client = MagicMock()
        deepinfra_client.post = AsyncMock(
            return_value=_ok_response({"data": [{"embedding": VEC_1024}]})
        )
        deepinfra = DeepInfraBackend(api_key="key", client=deepinfra_client)

        p = EmbeddingProvider(backends=[ollama, deepinfra], cache_dir=None)
        result = await p.embed("test")
        assert result == VEC_1024

    @pytest.mark.asyncio
    async def test_all_fail_raises(self) -> None:
        """All backends fail → EmbeddingUnavailableError."""
        b1 = AsyncMock()
        b1.name = "b1"
        b1.embed = AsyncMock(side_effect=Exception("fail"))
        b2 = AsyncMock()
        b2.name = "b2"
        b2.embed = AsyncMock(side_effect=Exception("fail"))

        p = EmbeddingProvider(backends=[b1, b2], cache_dir=None)
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("test")

    @pytest.mark.asyncio
    async def test_embed_batch(self) -> None:
        b = AsyncMock()
        b.name = "test"
        b.embed = AsyncMock(return_value=VEC_1024)
        p = EmbeddingProvider(backends=[b], cache_dir=None)
        results = await p.embed_batch(["a", "b", "c"])
        assert len(results) == 3
        assert all(r == VEC_1024 for r in results)

    @pytest.mark.asyncio
    async def test_no_backends_raises(self) -> None:
        p = EmbeddingProvider(backends=[], cache_dir=None)
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("test")

    @pytest.mark.asyncio
    async def test_failure_counter_suppresses_spam(self) -> None:
        """After 3 consecutive failures, backend errors log at DEBUG not WARNING."""
        b_fail = AsyncMock()
        b_fail.name = "ollama_embedding"
        b_fail.embed = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        b_ok = AsyncMock()
        b_ok.name = "deepinfra_embedding"
        b_ok.embed = AsyncMock(return_value=VEC_1024)
        p = EmbeddingProvider(backends=[b_fail, b_ok], cache_dir=None)

        # After 4 calls, ollama should have 4 consecutive failures
        for _ in range(4):
            result = await p.embed(f"text-{_}")
            assert result == VEC_1024

        assert p._consecutive_backend_failures["ollama_embedding"] == 4

    @pytest.mark.asyncio
    async def test_failure_counter_resets_on_success(self) -> None:
        """Consecutive failure counter resets when backend succeeds."""
        call_count = 0

        async def _flaky_embed(text):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.ConnectError("refused")
            return VEC_1024

        b = AsyncMock()
        b.name = "flaky"
        b.embed = _flaky_embed
        p = EmbeddingProvider(backends=[b], cache_dir=None)

        # First 2 calls fail
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("text1")
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("text2")
        assert p._consecutive_backend_failures["flaky"] == 2

        # Third call succeeds
        result = await p.embed("text3")
        assert result == VEC_1024
        assert p._consecutive_backend_failures["flaky"] == 0


class TestOllamaRetry:
    @pytest.mark.asyncio
    async def test_retries_once_on_read_timeout(self) -> None:
        """OllamaBackend retries once on ReadTimeout before failing."""
        call_count = 0

        async def _timeout_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadTimeout("timeout")
            return _ok_response({"embeddings": [VEC_1024]})

        client = MagicMock()
        client.post = _timeout_then_succeed
        backend = OllamaBackend(url="http://fake:11434", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        assert call_count == 2  # 1 timeout + 1 success

    @pytest.mark.asyncio
    async def test_raises_after_two_timeouts(self) -> None:
        """OllamaBackend raises after retry also times out."""
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        backend = OllamaBackend(url="http://fake:11434", client=client)
        with pytest.raises(httpx.ReadTimeout):
            await backend.embed("test")
        assert client.post.call_count == 2  # original + 1 retry

    @pytest.mark.asyncio
    async def test_non_timeout_errors_not_retried(self) -> None:
        """Non-timeout errors are raised immediately without retry."""
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend = OllamaBackend(url="http://fake:11434", client=client)
        with pytest.raises(httpx.ConnectError):
            await backend.embed("test")
        assert client.post.call_count == 1  # no retry
