"""Real client/cache cleanup, without remote requests or embedding inference."""

import asyncio
import sqlite3

import httpx
import pytest

from genesis.memory import embeddings


class Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.closes = 0

    async def handle_async_request(self, request):
        raise AssertionError("Lifecycle test must not issue requests")

    async def aclose(self):
        self.closes += 1


def backend(name, **options):
    if name == "ollama":
        return embeddings.OllamaBackend("http://127.0.0.1:1", **options)
    if name == "deepinfra":
        return embeddings.DeepInfraBackend("synthetic-key", **options)
    return embeddings.DashScopeBackend("synthetic-key", **options)


@pytest.mark.parametrize("name", ["ollama", "deepinfra", "dashscope"])
@pytest.mark.parametrize("owned", [False, True])
async def test_only_owned_client_closes_and_success_is_idempotent(monkeypatch, name, owned):
    transport = Transport()
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(embeddings, "_build_embed_client", lambda *a, **kw: client)
    instance = backend(name, **({} if owned else {"client": client}))
    try:
        assert not client.is_closed
        await instance.aclose()
        await instance.aclose()
        assert client.is_closed is owned
        assert transport.closes == int(owned)
    finally:
        await client.aclose()


@pytest.mark.parametrize("name", ["ollama", "deepinfra", "dashscope"])
async def test_falsey_injected_client_is_still_borrowed(monkeypatch, name):
    class FalseyClient(httpx.AsyncClient):
        def __bool__(self):
            return False

    client = FalseyClient(transport=Transport())

    def unexpected(*args, **kwargs):
        raise AssertionError("Injected client must not allocate a replacement")

    monkeypatch.setattr(embeddings, "_build_embed_client", unexpected)
    try:
        instance = backend(name, client=client)
        assert instance._client is client
        await instance.aclose()
        assert not client.is_closed
    finally:
        await client.aclose()


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_close_does_not_forget_owned_client(monkeypatch, error):
    client = httpx.AsyncClient(transport=Transport())
    original = client.aclose
    monkeypatch.setattr(embeddings, "_build_embed_client", lambda *a, **kw: client)
    instance = backend("ollama")

    async def failed():
        raise error()

    monkeypatch.setattr(client, "aclose", failed)
    try:
        with pytest.raises(error):
            await instance.aclose()
        assert not client.is_closed
        monkeypatch.setattr(client, "aclose", original)
        await instance.aclose()
        assert client.is_closed
    finally:
        await original()


class CacheBackend:
    name = "synthetic-cache-backend"
    vector_space = "synthetic-vector-space"

    async def embed(self, text):
        raise AssertionError("Lifecycle test must not embed")

    async def is_available(self):
        return False


def test_cache_cleanup_retires_original_connection_without_closing_peer(tmp_path):
    first = embeddings.EmbeddingProvider(backends=[CacheBackend()], cache_dir=tmp_path)
    second = embeddings.EmbeddingProvider(backends=[CacheBackend()], cache_dir=tmp_path)
    first_cache, second_cache = first._disk_cache, second._disk_cache
    first_connection, second_connection = first_cache._con, second_cache._con
    try:
        first.close_cache()
        first.close_cache()
        with pytest.raises(sqlite3.ProgrammingError):
            first_connection.execute("SELECT 1")
        assert second_connection.execute("SELECT 1").fetchone() == (1,)
        assert first._disk_cache is None
    finally:
        first_cache.close()
        second_cache.close()


def test_no_disk_cache_cleanup_is_idempotent():
    provider = embeddings.EmbeddingProvider(backends=[CacheBackend()], cache_dir=None)
    provider.close_cache()
    provider.close_cache()


def test_constructor_tail_failure_closes_completed_cache_prefix(tmp_path, monkeypatch):
    import diskcache

    created = []
    initialize = diskcache.Cache.__init__

    def capture(cache, *args, **kwargs):
        initialize(cache, *args, **kwargs)
        created.append((cache, cache._con))

    def fail(*args, **kwargs):
        raise ValueError("synthetic constructor tail failure")

    monkeypatch.setattr(diskcache.Cache, "__init__", capture)
    monkeypatch.setattr(embeddings.logger, "info", fail)
    try:
        with pytest.raises(ValueError, match="constructor tail failure"):
            embeddings.EmbeddingProvider(backends=[CacheBackend()], cache_dir=tmp_path)
        assert len(created) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            created[0][1].execute("SELECT 1")
    finally:
        for cache, _connection in created:
            cache.close()


@pytest.mark.parametrize("fail_second,different_space", [(False, False), (False, True), (True, False)])
def test_registers_every_created_backend_before_next_constructor(monkeypatch, fail_second, different_space):
    from genesis import env

    events, registered = [], []
    monkeypatch.setattr(env, "ollama_enabled", lambda: True)
    monkeypatch.setattr(env, "ollama_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(env, "deepinfra_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(env, "dashscope_api_key", lambda: "")

    class Local(CacheBackend):
        name = "local"

        def __init__(self, **kwargs):
            events.append("construct-local")

    class Cloud(CacheBackend):
        name = "cloud"

        def __init__(self, **kwargs):
            events.append("construct-cloud")
            if fail_second:
                raise ValueError("synthetic construction failure")

    monkeypatch.setattr(embeddings, "OllamaBackend", Local)
    monkeypatch.setattr(embeddings, "DeepInfraBackend", Cloud)
    if different_space:
        Cloud.vector_space = "different-synthetic-space"

    def register(instance):
        events.append("register-" + instance.name)
        registered.append(instance)

    if fail_second:
        with pytest.raises(ValueError, match="construction failure"):
            embeddings.EmbeddingProvider.build_chain(ollama_first=True, on_backend_created=register)
        assert events == ["construct-local", "register-local", "construct-cloud"]
        assert [item.name for item in registered] == ["local"]
    else:
        chain = embeddings.EmbeddingProvider.build_chain(ollama_first=True, on_backend_created=register)
        assert events == ["construct-local", "register-local", "construct-cloud", "register-cloud"]
        assert chain == registered[:1] if different_space else registered == chain
