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
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DeepInfraBackend(api_key="test-key", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        call_url = client.post.call_args[0][0]
        assert "deepinfra" in call_url

    @pytest.mark.asyncio
    async def test_auth_header(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DeepInfraBackend(api_key="my-secret", client=client)
        await backend.embed("test")
        headers = client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-secret"

    # ── service tier ──────────────────────────────────────────────────────
    #
    # DeepInfra queues DEFAULT-tier requests when a model is under load. MEASURED
    # 2026-09-04 on Qwen3-Embedding-0.6B, 3 runs at each size: default took
    # 8.6s / 13.3s / 7.8s at 25 / 120 / 600 tokens; priority took 602 / 684 /
    # 613ms. Priority being FLAT across input size is the tell — compute for a
    # 0.6B model is sub-second, so the seconds on default were admission queue,
    # not inference. Against the recall route's 4.5s deadline that meant a 100%
    # 503 rate (20/20 measured through the live endpoint).

    @pytest.mark.asyncio
    async def test_no_service_tier_by_default(self) -> None:
        """Absent means absent — never send a billable field unasked."""
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DeepInfraBackend(api_key="k", client=client)
        await backend.embed("test")
        assert "service_tier" not in client.post.call_args[1]["json"]

    @pytest.mark.asyncio
    async def test_service_tier_sent_when_set(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DeepInfraBackend(api_key="k", client=client, service_tier="priority")
        await backend.embed("test")
        assert client.post.call_args[1]["json"]["service_tier"] == "priority"

    @pytest.mark.asyncio
    async def test_service_tier_is_additive_only(self) -> None:
        """The tier must not disturb the rest of the payload."""
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DeepInfraBackend(api_key="k", client=client, service_tier="priority")
        await backend.embed("hello")
        body = client.post.call_args[1]["json"]
        assert body["model"] == "Qwen/Qwen3-Embedding-0.6B"
        assert body["input"] == ["hello"]


class TestDashScopeBackend:
    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
        backend = DashScopeBackend(api_key="test-key", client=client)
        result = await backend.embed("test")
        assert result == VEC_1024
        call_url = client.post.call_args[0][0]
        assert "dashscope" in call_url

    @pytest.mark.asyncio
    async def test_dimensions_param(self) -> None:
        client = MagicMock()
        client.post = AsyncMock(return_value=_ok_response({"data": [{"embedding": VEC_1024}]}))
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


class TestConnectionReuse:
    """Backends build warm-reuse AsyncClients (kills the 5s-expiry re-handshake).

    Asserts the tuned httpx.Limits / keepalive_expiry are applied and that
    HTTP/2 is negotiated only for the TLS cloud backends when ``h2`` is
    importable (never for cleartext Ollama).
    """

    @staticmethod
    def _pool(client: httpx.AsyncClient):
        # httpx AsyncClient -> AsyncHTTPTransport -> httpcore AsyncConnectionPool
        return client._transport._pool

    def test_embed_limits_are_tuned(self) -> None:
        from genesis.memory.embeddings import (
            _EMBED_KEEPALIVE_EXPIRY_S,
            _EMBED_MAX_KEEPALIVE_CONNECTIONS,
            _embed_limits,
        )

        limits = _embed_limits()
        assert limits.keepalive_expiry == _EMBED_KEEPALIVE_EXPIRY_S
        assert limits.keepalive_expiry >= 30.0  # survives sparse recall gaps
        assert limits.max_keepalive_connections == _EMBED_MAX_KEEPALIVE_CONNECTIONS
        # httpx default keepalive_expiry is 5s; we must beat it decisively.
        assert limits.keepalive_expiry > 5.0

    def test_deepinfra_client_tuned_and_http2(self) -> None:
        from genesis.memory.embeddings import (
            _EMBED_KEEPALIVE_EXPIRY_S,
            _EMBED_MAX_KEEPALIVE_CONNECTIONS,
            _http2_available,
        )

        backend = DeepInfraBackend(api_key="k")
        pool = self._pool(backend._client)
        assert pool._keepalive_expiry == _EMBED_KEEPALIVE_EXPIRY_S
        assert pool._max_keepalive_connections == _EMBED_MAX_KEEPALIVE_CONNECTIONS
        # HTTP/2 iff the optional h2 package is present in the venv.
        assert pool._http2 is _http2_available()

    def test_dashscope_client_tuned_and_http2(self) -> None:
        from genesis.memory.embeddings import (
            _EMBED_KEEPALIVE_EXPIRY_S,
            _http2_available,
        )

        backend = DashScopeBackend(api_key="k")
        pool = self._pool(backend._client)
        assert pool._keepalive_expiry == _EMBED_KEEPALIVE_EXPIRY_S
        assert pool._http2 is _http2_available()

    def test_ollama_client_tuned_but_no_http2(self) -> None:
        from genesis.memory.embeddings import _EMBED_KEEPALIVE_EXPIRY_S

        backend = OllamaBackend(url="http://fake:11434")
        pool = self._pool(backend._client)
        # Warm-reuse limits still apply to the local backend...
        assert pool._keepalive_expiry == _EMBED_KEEPALIVE_EXPIRY_S
        # ...but HTTP/2 must never be forced on the cleartext local endpoint.
        assert pool._http2 is False

    def test_injected_client_is_not_overridden(self) -> None:
        sentinel = MagicMock()
        backend = DeepInfraBackend(api_key="k", client=sentinel)
        assert backend._client is sentinel


class TestBuildChainPriorityTier:
    """WHICH chain pays for priority, and which does not.

    The split is not cosmetic. Recall is deadline-bound (a 4.5s route timeout) and
    is what broke; STORAGE embedding is a background write with no deadline, so it
    has no reason to pay 1.5x. `build_chain` already separates the two orderings
    (runtime/init/memory.py:82-83), so the billing split rides on a distinction
    that already exists rather than inventing one.

    Cost, MEASURED so the tradeoff is on the record: $0.010 -> $0.015 per 1M
    tokens, against 217 recall requests in 24h at ~120 tokens each. That is a
    difference of roughly half a cent per month.
    """

    @staticmethod
    def _deepinfra(chain):
        return next((b for b in chain if b.name == "deepinfra_embedding"), None)

    def test_recall_chain_requests_priority(self, monkeypatch) -> None:
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "false")
        chain = EmbeddingProvider.build_chain(ollama_first=False, priority_tier=True)
        backend = self._deepinfra(chain)
        assert backend is not None, "deepinfra must be in the recall chain"
        assert backend._service_tier == "priority"

    def test_storage_chain_stays_on_the_cheap_tier(self, monkeypatch) -> None:
        """The control. Without this, defaulting everything to priority would pass."""
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "false")
        chain = EmbeddingProvider.build_chain(ollama_first=True)
        backend = self._deepinfra(chain)
        assert backend is not None
        assert backend._service_tier is None

    def test_priority_defaults_off_at_the_builder(self, monkeypatch) -> None:
        """build_chain must not opt anyone in silently; the CALLER decides."""
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "false")
        chain = EmbeddingProvider.build_chain(ollama_first=False)
        assert self._deepinfra(chain)._service_tier is None


class TestRecallChainWiring:
    """The WIRING, not the capability — 'built != wired'.

    TestBuildChainPriorityTier proves build_chain CAN apply the tier. It does not
    prove the runtime DOES. Deleting `priority_tier=priority` from
    runtime/init/memory.py left every other test in this file green, which is
    exactly the hole this closes: the one line the whole change exists for was
    unlocked.

    Asserted against the source rather than by driving init(), which needs a live
    DB, Qdrant and a bootstrapped runtime. A source assertion is weaker than an
    executed one and is chosen knowingly: the alternative here is no lock at all.
    """

    @staticmethod
    def _init_source() -> str:
        from pathlib import Path

        import genesis.runtime.init.memory as mod

        return Path(mod.__file__).read_text()

    def test_recall_chain_opts_into_priority(self) -> None:
        src = self._init_source()
        assert "recall_backends = EmbeddingProvider.build_chain(" in src
        recall_call = src.split("recall_backends = EmbeddingProvider.build_chain(")[1]
        recall_call = recall_call.split(")")[0]
        assert "ollama_first=False" in recall_call
        assert "priority_tier=priority" in recall_call, (
            "the recall chain must pass the tier — without this line the fix is inert"
        )

    def test_storage_chain_does_not(self) -> None:
        """The control: if BOTH chains passed it, the test above would be vacuous."""
        src = self._init_source()
        storage_call = src.split("storage_backends = EmbeddingProvider.build_chain(")[1]
        storage_call = storage_call.split(")")[0]
        assert "priority_tier" not in storage_call, (
            "storage is a background write with no deadline — it must not pay 1.5x"
        )

    def test_the_tier_decision_reads_the_config_lever(self) -> None:
        """A hardcoded True would pass both tests above; the lever must be used."""
        src = self._init_source()
        assert "embed_priority_tier" in src
        assert "priority = embed_priority_tier()" in src
