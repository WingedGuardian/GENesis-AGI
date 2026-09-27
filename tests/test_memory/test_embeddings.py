"""Tests for genesis.memory.embeddings — backend chain architecture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from genesis.memory.embeddings import (
    CANONICAL_VECTOR_SPACE,
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
        deepinfra.vector_space = CANONICAL_VECTOR_SPACE

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
        b1.vector_space = "test-space"
        b1.embed = AsyncMock(side_effect=Exception("fail"))
        b2 = AsyncMock()
        b2.name = "b2"
        b2.vector_space = "test-space"
        b2.embed = AsyncMock(side_effect=Exception("fail"))

        p = EmbeddingProvider(backends=[b1, b2], cache_dir=None)
        with pytest.raises(EmbeddingUnavailableError):
            await p.embed("test")

    @pytest.mark.asyncio
    async def test_embed_batch(self) -> None:
        b = AsyncMock()
        b.name = "test"
        b.vector_space = "test-space"
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
        b_fail.vector_space = "test-space"
        b_fail.embed = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        b_ok = AsyncMock()
        b_ok.name = "deepinfra_embedding"
        b_ok.vector_space = "test-space"
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
        b.vector_space = "test-space"
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

    def test_the_builder_DEFAULT_is_cloud_first(self, monkeypatch) -> None:
        """The default is the change; an explicit-argument test cannot bind it.

        FIFTEEN callers construct `EmbeddingProvider()` with no chain and
        inherit whatever this default is — six in the package (trace,
        stale-embedding repair, procedural embedding and its promoter, the
        procedural MCP, the session-awareness worker) and nine in `scripts/`,
        the busiest being `genesis_mcp_server.py`, the provider behind
        `memory_store` / `reference_store` / `knowledge_ingest` for every
        session. Flipping only `runtime/init/memory.py` would have left every
        one of them on local inference and made the change cosmetic. (They are
        not all write paths — an earlier revision said so; the query-embedding
        callers are named in the build_chain docstring.)

        This docstring said "six" until an audit enumerated `scripts/` too — the
        original count swept `src/` only and was repeated into the changelog and
        the builder docstring before anyone checked it.

        MEASURED 2026-09-26 through this chain, 20 calls each: Ollama p50
        2395.8ms, DeepInfra p50 207.8ms.
        """
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")

        chain = EmbeddingProvider.build_chain()
        names = [b.name for b in chain]

        assert "deepinfra_embedding" in names and "ollama_embedding" in names, (
            f"both backends must be present for this test to mean anything: {names}"
        )
        assert names.index("deepinfra_embedding") < names.index("ollama_embedding"), (
            f"the DEFAULT chain must lead with the cloud backend, got {names}"
        )

    def test_a_bare_EmbeddingProvider_is_cloud_first(self, monkeypatch) -> None:
        """The path the six bare callers actually take, end to end.

        `build_chain()` and `_build_default_chain()` are separate lines; binding
        only the former leaves the second free to disagree, which is exactly how
        the two could drift apart unnoticed.
        """
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")

        # cache_dir=None: every other test in this file does the same. The
        # default opens ~/.genesis/embedding_cache, which is shared live with
        # the running MCP servers and sits at its 100 MB eviction ceiling. This
        # test asserts on _backends, so the cache is pure side effect.
        provider = EmbeddingProvider(cache_dir=None)
        names = [b.name for b in provider._backends]

        assert "deepinfra_embedding" in names and "ollama_embedding" in names, names
        assert names.index("deepinfra_embedding") < names.index("ollama_embedding"), (
            f"a provider built with no explicit chain must be cloud-first, got {names}"
        )

    def test_ollama_first_is_still_reachable_when_asked_for(self, monkeypatch) -> None:
        """CONTROL — the flip must change the default, not remove the capability.

        Ollama-first remains the right order for anything that must not leave the
        host. If this goes red the parameter has stopped working rather than the
        default having moved.
        """
        monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")

        chain = EmbeddingProvider.build_chain(ollama_first=True)
        names = [b.name for b in chain]

        assert names.index("ollama_embedding") < names.index("deepinfra_embedding"), (
            f"ollama_first=True must still lead with Ollama, got {names}"
        )

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

    def test_the_storage_chain_is_cloud_first(self) -> None:
        """The one line the flip exists for, and it was unlocked.

        MEASURED: reverting `storage_backends` to `ollama_first=True` left all
        59 tests in this file green — including the three added with the flip,
        because those bind the BUILDER default and this binds the CALLER. The
        purpose-built harness for exactly this was sitting twenty lines above
        and went unused.
        """
        src = self._init_source()
        assert "storage_backends = EmbeddingProvider.build_chain(" in src
        storage_call = src.split("storage_backends = EmbeddingProvider.build_chain(")[1]
        storage_call = storage_call.split(")")[0]
        assert "ollama_first=False" in storage_call, (
            "storage must lead with the cloud backend — without this line the "
            "flip is inert for the runtime, whatever the builder default says"
        )

    def test_the_tier_decision_reads_the_config_lever(self) -> None:
        """A hardcoded True would pass both tests above; the lever must be used."""
        src = self._init_source()
        assert "embed_priority_tier" in src
        assert "priority = embed_priority_tier()" in src


class TestOneVectorSpacePerChain:
    """A chain may only contain backends that produce vectors in ONE space.

    Matching dimension proves nothing: DashScope's text-embedding-v4 and the
    Qwen3-Embedding-0.6B model are both 1024-d, but they place the same text at
    unrelated coordinates. A write through one and a query through the other
    scores as noise with no error anywhere. Before this, the cloud-first flip put
    DashScope AHEAD of the local Qwen3 backend whenever DeepInfra was absent, so
    every successful DashScope write went into the Qwen3 corpus.

    The env matrix below is swept rather than sampled: every combination of
    {ollama off, ollama qwen3, ollama other-model} x {deepinfra key} x
    {dashscope key}, and for each the STORAGE chain and the RECALL chain must
    resolve to the same single space — the property the corpus depends on.
    """

    @staticmethod
    def _env(monkeypatch, *, ollama: str | None, deepinfra: bool, dashscope: bool) -> None:
        if ollama is None:
            monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "false")
            monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
        else:
            monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")
            monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", ollama)
        if deepinfra:
            monkeypatch.setenv("API_KEY_DEEPINFRA", "k")
        else:
            monkeypatch.delenv("API_KEY_DEEPINFRA", raising=False)
        if dashscope:
            monkeypatch.setenv("API_KEY_QWEN", "k")
        else:
            monkeypatch.delenv("API_KEY_QWEN", raising=False)

    @staticmethod
    def _names(chain) -> list[str]:
        return [b.name for b in chain]

    @pytest.mark.parametrize(
        "ollama",
        [None, "qwen3-embedding:0.6b-fp16", "hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0", "nomic-embed-text"],
    )
    @pytest.mark.parametrize("deepinfra", [False, True])
    @pytest.mark.parametrize("dashscope", [False, True])
    def test_storage_and_recall_resolve_to_one_shared_space(
        self, monkeypatch, ollama, deepinfra, dashscope,
    ) -> None:
        self._env(monkeypatch, ollama=ollama, deepinfra=deepinfra, dashscope=dashscope)

        storage = EmbeddingProvider.build_chain(ollama_first=False)
        recall = EmbeddingProvider.build_chain(ollama_first=False, priority_tier=True)

        storage_spaces = {b.vector_space for b in storage}
        recall_spaces = {b.vector_space for b in recall}
        configured = ollama is not None or deepinfra or dashscope
        if not configured:
            assert storage == [] and recall == []
            return
        assert len(storage_spaces) == 1, f"storage chain mixes spaces: {storage_spaces}"
        assert storage_spaces == recall_spaces, (
            f"storage {storage_spaces} and recall {recall_spaces} must agree on the model"
        )
        # And the provider built from each carries that one space.
        assert (
            EmbeddingProvider(backends=storage, cache_dir=None).vector_space
            == EmbeddingProvider(backends=recall, cache_dir=None).vector_space
        )

    def test_dashscope_never_leads_a_qwen3_local_chain(self, monkeypatch) -> None:
        """The P1 shape: DashScope + local Qwen3, no DeepInfra.

        Before the fix the cloud-first order made this [dashscope, ollama], so a
        healthy DashScope answered every write and Ollama was never reached.
        """
        self._env(monkeypatch, ollama="qwen3-embedding:0.6b-fp16", deepinfra=False, dashscope=True)
        assert self._names(EmbeddingProvider.build_chain()) == ["ollama_embedding"]
        assert self._names(
            EmbeddingProvider.build_chain(ollama_first=False, priority_tier=True)
        ) == ["ollama_embedding"]

    def test_full_install_keeps_the_cloud_first_flip_within_qwen3(self, monkeypatch) -> None:
        """All three configured: the flip survives, DashScope does not."""
        self._env(monkeypatch, ollama="qwen3-embedding:0.6b-fp16", deepinfra=True, dashscope=True)
        assert self._names(EmbeddingProvider.build_chain()) == [
            "deepinfra_embedding", "ollama_embedding",
        ]

    def test_dashscope_alone_is_still_usable(self, monkeypatch) -> None:
        """CONTROL — a DashScope-only install keeps working, in its own space."""
        self._env(monkeypatch, ollama=None, deepinfra=False, dashscope=True)
        chain = EmbeddingProvider.build_chain()
        assert self._names(chain) == ["dashscope_embedding"]
        assert chain[0].vector_space != CANONICAL_VECTOR_SPACE

    def test_the_corpus_space_is_anchored_to_the_local_model(self, monkeypatch) -> None:
        """A non-Qwen3 local model + DeepInfra: storage has been writing the LOCAL
        model's vectors, so the chain stays in that space rather than switching the
        corpus to DeepInfra's model because cloud now leads the order."""
        self._env(monkeypatch, ollama="nomic-embed-text", deepinfra=True, dashscope=False)
        assert self._names(EmbeddingProvider.build_chain()) == ["ollama_embedding"]

    def test_a_hand_built_mixed_chain_is_refused(self) -> None:
        with pytest.raises(ValueError, match="vector space"):
            EmbeddingProvider(
                backends=[
                    DeepInfraBackend(api_key="k", client=MagicMock()),
                    DashScopeBackend(api_key="k", client=MagicMock()),
                ],
                cache_dir=None,
            )

    def test_upstream_named_qwen3_pull_keeps_the_cloud_rung(self, monkeypatch) -> None:
        """Same weights pulled under the upstream repo name are the canonical space.

        A library-tag-only rule dropped DeepInfra from BOTH chains here, so
        recall lost its priority-tier cloud rung and ran local-only.
        """
        self._env(
            monkeypatch, ollama="hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0",
            deepinfra=True, dashscope=False,
        )
        assert self._names(
            EmbeddingProvider.build_chain(ollama_first=False, priority_tier=True)
        ) == ["deepinfra_embedding", "ollama_embedding"]

    def test_an_excluded_dashscope_is_never_constructed(self, monkeypatch) -> None:
        """Callers build a provider per call; an excluded backend must not
        leave an HTTP client behind each time."""
        from genesis.memory import embeddings as mod

        built: list[object] = []
        real_init = mod.DashScopeBackend.__init__

        def spy(self, *a, **k):
            built.append(self)
            real_init(self, *a, **k)

        monkeypatch.setattr(mod.DashScopeBackend, "__init__", spy)
        self._env(monkeypatch, ollama="qwen3-embedding:0.6b-fp16", deepinfra=True, dashscope=True)
        EmbeddingProvider.build_chain()
        assert built == []
        # Control: with nothing else configured it IS built.
        self._env(monkeypatch, ollama=None, deepinfra=False, dashscope=True)
        EmbeddingProvider.build_chain()
        assert len(built) == 1

    def test_known_spaces(self) -> None:
        assert OllamaBackend(url="http://x", client=MagicMock()).vector_space == CANONICAL_VECTOR_SPACE
        assert DeepInfraBackend(api_key="k", client=MagicMock()).vector_space == CANONICAL_VECTOR_SPACE
        assert DashScopeBackend(api_key="k", client=MagicMock()).vector_space != CANONICAL_VECTOR_SPACE

    def test_cache_keys_are_scoped_by_space(self) -> None:
        """A vector cached by one space must never be served to another.

        The pre-existing key ("qwen3-embedding:{text}") was shared by EVERY
        backend, so it may hold a DashScope fallback vector; no space reads it.
        """
        import hashlib

        qwen = EmbeddingProvider(
            backends=[DeepInfraBackend(api_key="k", client=MagicMock())], cache_dir=None,
        )
        dash = EmbeddingProvider(
            backends=[DashScopeBackend(api_key="k", client=MagicMock())], cache_dir=None,
        )
        legacy = hashlib.sha256(b"qwen3-embedding:hello").hexdigest()
        assert qwen._cache_key("hello") != legacy
        assert dash._cache_key("hello") != legacy
        assert qwen._cache_key("hello") != dash._cache_key("hello")


class TestStandaloneMemoryMcpSplitsRecall:
    """The standalone memory MCP must not recall through the storage provider.

    It used to pass ONE bare provider as ``embedding_provider``, which
    ``genesis.mcp.memory.init`` reuses for both MemoryStore and HybridRetriever.
    With the default chain now cloud-first on the ordinary tier, memory_recall
    would inherit that tier's documented queue under load. Source assertion, for
    the same reason as TestRecallChainWiring: the lifespan needs a live DB and
    Qdrant to execute.
    """

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (Path(__file__).parents[2] / "scripts" / "genesis_mcp_server.py").read_text()

    def test_recall_provider_is_separate_and_priority_tier(self) -> None:
        src = self._src()
        assert "recall_embedding_provider=recall_embedding" in src
        assert "storage_embedding_provider=storage_embedding" in src
        assert "priority_tier=embed_priority_tier()" in src

    def test_the_shared_legacy_argument_is_gone(self) -> None:
        """Control: if the legacy kwarg were still passed, init would reuse it."""
        assert "embedding_provider=embedding," not in self._src()


class _SpaceFake:
    """A minimal backend double that DECLARES its space, as every backend must."""

    def __init__(self, name: str, *, space: object, model: str = "m", fail: bool = False) -> None:
        self.name = name
        self.vector_space = space
        self._model = model
        self._fail = fail
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self._fail:
            raise httpx.ConnectError("down")
        return [0.5] * 4

    async def is_available(self) -> bool:
        return not self._fail


class TestFreshCollectionAnchor:
    """A caller writing into a brand-new, EMPTY collection has no corpus to match.

    The default anchor is the space of the historical storage leader, because a
    live collection was written in that space. A fresh collection (the
    LongMemEval ephemeral store) was not written in any space yet, so anchoring
    it to the local model threw away the cloud rung the caller asked to lead.
    """

    _env = staticmethod(TestOneVectorSpacePerChain._env)

    @staticmethod
    def _names(chain) -> list[str]:
        return [b.name for b in chain]

    def test_fresh_collection_keeps_the_cloud_leader_beside_a_foreign_local_model(
        self, monkeypatch,
    ) -> None:
        self._env(monkeypatch, ollama="nomic-embed-text", deepinfra=True, dashscope=False)
        assert self._names(
            EmbeddingProvider.build_chain(ollama_first=False, fresh_collection=True)
        ) == ["deepinfra_embedding"]

    def test_the_corpus_anchor_is_still_the_default(self, monkeypatch) -> None:
        """CONTROL — without the flag a live corpus keeps its local space."""
        self._env(monkeypatch, ollama="nomic-embed-text", deepinfra=True, dashscope=False)
        assert self._names(EmbeddingProvider.build_chain(ollama_first=False)) == [
            "ollama_embedding",
        ]

    @pytest.mark.parametrize(
        "ollama", [None, "qwen3-embedding:0.6b-fp16", "nomic-embed-text"],
    )
    @pytest.mark.parametrize("deepinfra", [False, True])
    @pytest.mark.parametrize("dashscope", [False, True])
    @pytest.mark.parametrize("ollama_first", [False, True])
    def test_a_fresh_chain_is_one_space_led_by_the_requested_order(
        self, monkeypatch, ollama, deepinfra, dashscope, ollama_first,
    ) -> None:
        self._env(monkeypatch, ollama=ollama, deepinfra=deepinfra, dashscope=dashscope)
        chain = EmbeddingProvider.build_chain(ollama_first=ollama_first, fresh_collection=True)
        if not (ollama or deepinfra or dashscope):
            assert chain == []
            return
        assert len({b.vector_space for b in chain}) == 1
        # The leader is the first backend the caller's ORDER names.
        clouds = (["deepinfra_embedding"] if deepinfra else []) + (
            ["dashscope_embedding"] if dashscope else []
        )
        local = ["ollama_embedding"] if ollama else []
        expected_leader = (local + clouds if ollama_first else clouds + local)[0]
        assert chain[0].name == expected_leader

    @pytest.mark.parametrize(
        "rel", ["src/genesis/eval/longmemeval/store.py", "src/genesis/eval/longmemeval/runner.py"],
    )
    def test_the_ephemeral_eval_stores_request_a_fresh_chain(self, rel) -> None:
        from pathlib import Path

        src = (Path(__file__).parents[2] / rel).read_text()
        assert "build_chain(ollama_first=False, fresh_collection=True)" in src


class TestSpaceDeclarationIsMandatory:
    """The one-space rule used to skip any backend that declared no space.

    So an undeclared backend could sit beside a Qwen3 one and write whatever
    model it is into the same collection, and every undeclared provider shared
    one cache namespace. A backend now either names its space or is refused.
    """

    def test_an_undeclared_backend_beside_a_declared_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="vector space"):
            EmbeddingProvider(
                backends=[
                    _SpaceFake("custom", space=None),
                    DeepInfraBackend(api_key="k", client=MagicMock()),
                ],
                cache_dir=None,
            )

    @pytest.mark.parametrize("space", [None, "", MagicMock()])
    def test_an_undeclared_backend_alone_is_refused(self, space) -> None:
        with pytest.raises(ValueError, match="vector space"):
            EmbeddingProvider(backends=[_SpaceFake("custom", space=space)], cache_dir=None)

    def test_declared_same_space_fakes_are_accepted(self) -> None:
        """CONTROL — declaring the space is all it takes."""
        p = EmbeddingProvider(
            backends=[_SpaceFake("a", space="s"), _SpaceFake("b", space="s")], cache_dir=None,
        )
        assert p.vector_space == "s"

    def test_an_empty_chain_is_still_constructible(self) -> None:
        """No backend means nothing can be written, so nothing can mix."""
        assert EmbeddingProvider(backends=[], cache_dir=None).vector_space is None


class TestLastBackendIsObserved:
    """Which backend wrote a vector is observed, not inferred from chain order."""

    @pytest.mark.asyncio
    async def test_a_fallback_answer_is_recorded_as_the_last_backend(self) -> None:
        primary = _SpaceFake("primary", space="s", model="cloud-model", fail=True)
        fallback = _SpaceFake("fallback", space="s", model="local-model")
        p = EmbeddingProvider(backends=[primary, fallback], cache_dir=None)
        assert p.last_backend is None
        await p.embed("hello")
        assert p.last_backend is fallback

    @pytest.mark.asyncio
    async def test_the_primary_is_recorded_when_it_answers(self) -> None:
        """CONTROL — the field follows the answer, it is not pinned to a rung."""
        primary = _SpaceFake("primary", space="s", model="cloud-model")
        fallback = _SpaceFake("fallback", space="s", model="local-model")
        p = EmbeddingProvider(backends=[primary, fallback], cache_dir=None)
        await p.embed("hello")
        assert p.last_backend is primary
