"""Tests for GenesisRuntime singleton."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.runtime import GenesisRuntime


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Reset singleton between tests."""
    GenesisRuntime.reset()
    yield
    GenesisRuntime.reset()


def _all_init_patches():
    """Context managers that patch all bootstrap init steps."""
    return [
        patch("genesis.runtime.GenesisRuntime._load_secrets"),
        patch("genesis.runtime.GenesisRuntime._init_db", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_tool_registry", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_observability"),
        patch("genesis.runtime.GenesisRuntime._init_providers"),
        patch("genesis.runtime.GenesisRuntime._init_modules", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_awareness", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_router"),
        patch("genesis.runtime.GenesisRuntime._init_perception"),
        patch("genesis.runtime.GenesisRuntime._init_cc_relay"),
        patch("genesis.runtime.GenesisRuntime._init_memory"),
        patch("genesis.runtime.GenesisRuntime._init_pipeline", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_surplus", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_learning", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_inbox", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_reflection", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_health_data"),
        patch("genesis.runtime.GenesisRuntime._init_outreach", new_callable=AsyncMock),
        patch("genesis.runtime.GenesisRuntime._init_autonomy"),
    ]


@contextlib.contextmanager
def _patched_bootstrap(rt):
    """Patch all bootstrap init steps and set critical subsystem attributes."""
    patches = _all_init_patches()
    mocks = {}
    for p in patches:
        m = p.start()
        mocks[p.attribute] = m

    # Set critical subsystem attributes so _run_init_step marks them "ok"
    async def fake_init_db():
        rt._db = MagicMock()
    mocks["_init_db"].side_effect = fake_init_db

    def fake_init_observability():
        rt._event_bus = MagicMock()
    mocks["_init_observability"].side_effect = fake_init_observability

    def fake_init_router():
        rt._router = MagicMock()
    mocks["_init_router"].side_effect = fake_init_router

    try:
        yield mocks
    finally:
        for p in patches:
            p.stop()


async def _bootstrap_with_db(rt):
    """Bootstrap runtime with all inits patched and _db set."""
    with _patched_bootstrap(rt) as mocks, \
         patch.object(rt, "_load_persisted_job_health", new_callable=AsyncMock):
        await rt.bootstrap()
    return [], mocks


class TestSingleton:
    def test_instance_returns_same_object(self):
        a = GenesisRuntime.instance()
        b = GenesisRuntime.instance()
        assert a is b

    def test_reset_clears_state(self):
        a = GenesisRuntime.instance()
        GenesisRuntime.reset()
        b = GenesisRuntime.instance()
        assert a is not b

    def test_fresh_instance_not_bootstrapped(self):
        rt = GenesisRuntime.instance()
        assert rt.is_bootstrapped is False

    def test_all_properties_none_before_bootstrap(self):
        rt = GenesisRuntime.instance()
        assert rt.db is None
        assert rt.event_bus is None
        assert rt.awareness_loop is None
        assert rt.router is None
        assert rt.reflection_engine is None
        assert rt.cc_invoker is None
        assert rt.session_manager is None
        assert rt.checkpoint_manager is None
        assert rt.cc_reflection_bridge is None
        assert rt.memory_store is None
        assert rt.triage_pipeline is None
        assert rt.surplus_scheduler is None
        assert rt.learning_scheduler is None
        assert rt.inbox_monitor is None
        assert rt.provider_registry is None
        assert rt.research_orchestrator is None
        assert rt.circuit_breakers is None
        assert rt.cost_tracker is None
        assert rt.dead_letter_queue is None
        assert rt.deferred_work_queue is None
        assert rt.cc_budget_tracker is None
        assert rt.health_data is None


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_idempotent(self):
        """Calling bootstrap twice doesn't duplicate initialization."""
        rt = GenesisRuntime.instance()

        with _patched_bootstrap(rt) as mocks:
            await rt.bootstrap()
            assert rt.is_bootstrapped is True
            assert mocks["_init_db"].call_count == 1

            # Second call should be a no-op
            await rt.bootstrap()
            assert mocks["_init_db"].call_count == 1  # NOT called again

    @pytest.mark.asyncio
    async def test_bootstrap_sets_bootstrapped(self):
        rt = GenesisRuntime.instance()
        assert rt.is_bootstrapped is False

        with _patched_bootstrap(rt):
            await rt.bootstrap()
            assert rt.is_bootstrapped is True

    @pytest.mark.asyncio
    async def test_bootstrap_aborts_if_db_fails(self):
        """If DB init fails, bootstrap aborts early and is NOT marked complete."""
        rt = GenesisRuntime.instance()

        with patch("genesis.runtime.GenesisRuntime._load_secrets"), \
             patch("genesis.runtime.GenesisRuntime._init_db", new_callable=AsyncMock) as mock_init_db, \
             patch("genesis.runtime.GenesisRuntime._init_observability"), \
             patch("genesis.runtime.GenesisRuntime._init_providers"), \
             patch("genesis.runtime.GenesisRuntime._init_awareness", new_callable=AsyncMock) as mock_awareness:
            # _init_db doesn't set _db → stays None → bootstrap returns early
            mock_init_db.side_effect = AsyncMock(return_value=None)

            await rt.bootstrap()
            assert rt.is_bootstrapped is False
            mock_awareness.assert_not_called()

    @pytest.mark.asyncio
    async def test_bootstrap_graceful_qdrant_failure(self):
        """MemoryStore fails but everything else continues."""
        rt = GenesisRuntime.instance()

        with _patched_bootstrap(rt) as mocks:
            # Memory init "fails" (doesn't set _memory_store)
            mocks["_init_memory"].return_value = None

            await rt.bootstrap()
            assert rt.is_bootstrapped is True
            assert rt.memory_store is None

    @pytest.mark.asyncio
    async def test_bootstrap_graceful_no_inbox_config(self):
        """No inbox config → inbox_monitor stays None, rest works."""
        rt = GenesisRuntime.instance()

        with _patched_bootstrap(rt) as mocks:
            # Inbox init leaves _inbox_monitor as None
            mocks["_init_inbox"].return_value = None

            await rt.bootstrap()
            assert rt.is_bootstrapped is True
            assert rt.inbox_monitor is None

    @pytest.mark.asyncio
    async def test_reset_allows_rebootstrap(self):
        """After reset(), bootstrap can run again."""
        rt1 = GenesisRuntime.instance()

        with _patched_bootstrap(rt1):
            await rt1.bootstrap()
            assert rt1.is_bootstrapped is True

        GenesisRuntime.reset()
        rt2 = GenesisRuntime.instance()
        assert rt2.is_bootstrapped is False

        with _patched_bootstrap(rt2):
            await rt2.bootstrap()
            assert rt2.is_bootstrapped is True


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_stops_all_subsystems(self):
        """Shutdown calls stop/shutdown on all active subsystems."""
        rt = GenesisRuntime.instance()
        rt._bootstrapped = True
        rt._db = AsyncMock()

        rt._awareness_loop = AsyncMock()
        rt._inbox_monitor = AsyncMock()
        rt._surplus_scheduler = AsyncMock()
        rt._reflection_scheduler = AsyncMock()

        # Learning scheduler is a raw APScheduler — has sync .shutdown()
        rt._learning_scheduler = MagicMock()

        await rt.shutdown()

        rt._reflection_scheduler.stop.assert_awaited_once()
        rt._inbox_monitor.stop.assert_awaited_once()
        rt._surplus_scheduler.stop.assert_awaited_once()
        rt._learning_scheduler.shutdown.assert_called_once_with(wait=False)
        rt._awareness_loop.stop.assert_awaited_once()
        rt._db.close.assert_awaited_once()
        assert rt.is_bootstrapped is False

    @pytest.mark.asyncio
    async def test_shutdown_handles_partial_bootstrap(self):
        """Shutdown works when only DB is set (other subsystems None)."""
        rt = GenesisRuntime.instance()
        rt._bootstrapped = True
        rt._db = AsyncMock()
        # All other subsystems are None (default)

        await rt.shutdown()

        rt._db.close.assert_awaited_once()
        assert rt.is_bootstrapped is False

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self):
        """Calling shutdown twice doesn't error."""
        rt = GenesisRuntime.instance()
        rt._bootstrapped = True
        rt._db = AsyncMock()

        await rt.shutdown()
        assert rt.is_bootstrapped is False

        # Second call is a no-op
        await rt.shutdown()
        # db.close should only be called once
        rt._db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_clears_bootstrapped_flag(self):
        rt = GenesisRuntime.instance()
        rt._bootstrapped = True
        rt._db = AsyncMock()

        await rt.shutdown()
        assert rt.is_bootstrapped is False

    @pytest.mark.asyncio
    async def test_shutdown_continues_on_subsystem_error(self):
        """If one subsystem fails to stop, others still get stopped."""
        rt = GenesisRuntime.instance()
        rt._bootstrapped = True
        rt._db = AsyncMock()

        rt._reflection_scheduler = AsyncMock()
        rt._reflection_scheduler.stop.side_effect = RuntimeError("boom")

        rt._awareness_loop = AsyncMock()

        await rt.shutdown()

        # Despite reflection_scheduler error, awareness_loop still stopped
        rt._awareness_loop.stop.assert_awaited_once()
        rt._db.close.assert_awaited_once()
        assert rt.is_bootstrapped is False


class TestProviderRegistryBootstrap:
    @pytest.mark.asyncio
    async def test_init_providers_creates_registry(self):
        """_init_providers creates registry and registers web_search."""
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry is not None
        assert rt.research_orchestrator is not None
        assert rt.provider_registry.get("web_search") is not None

    @pytest.mark.asyncio
    async def test_init_providers_registers_ollama_embedding(self, monkeypatch):
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("ollama_embedding") is not None

    @pytest.mark.asyncio
    async def test_init_providers_no_ollama_by_default(self, monkeypatch):
        monkeypatch.delenv("GENESIS_ENABLE_OLLAMA", raising=False)
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("ollama_embedding") is None

    @pytest.mark.asyncio
    async def test_init_providers_registers_health_probes(self, monkeypatch):
        monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("qdrant_probe") is not None
        assert rt.provider_registry.get("ollama_probe") is not None

    @pytest.mark.asyncio
    async def test_init_providers_stt_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_GROQ", raising=False)
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("groq_stt") is None

    @pytest.mark.asyncio
    async def test_init_providers_stt_with_api_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_GROQ", "test")
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("groq_stt") is not None

    @pytest.mark.asyncio
    async def test_init_providers_mistral_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_MISTRAL", raising=False)
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert rt.provider_registry.get("mistral_embedding") is None

    def test_provider_count_minimum(self, monkeypatch):
        """At minimum: web_search + qdrant_probe = 2 (cloud-primary, no Ollama)."""
        monkeypatch.delenv("GENESIS_ENABLE_OLLAMA", raising=False)
        rt = GenesisRuntime.instance()
        rt._db = None
        rt._event_bus = None

        rt._init_providers()

        assert len(rt.provider_registry.list_all()) >= 2
