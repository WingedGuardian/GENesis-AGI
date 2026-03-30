"""Tests for hybrid_retriever and context_injector on GenesisRuntime."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.runtime import GenesisRuntime


class TestRuntimeRetrieverProperties:
    def test_retriever_none_before_bootstrap(self):
        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()
        assert rt.hybrid_retriever is None
        assert rt.context_injector is None
        GenesisRuntime.reset()

    @pytest.mark.asyncio
    async def test_retriever_created_after_bootstrap(self):
        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()

        with (
            patch.object(rt, "_load_secrets"),
            patch.object(rt, "_init_db"),
            patch.object(rt, "_init_observability"),
            patch.object(rt, "_init_providers"),
            patch.object(rt, "_init_awareness"),
            patch.object(rt, "_init_router"),
            patch.object(rt, "_init_perception"),
            patch.object(rt, "_init_cc_relay"),
            patch.object(rt, "_init_surplus"),
            patch.object(rt, "_init_learning"),
            patch.object(rt, "_init_inbox"),
            patch.object(rt, "_init_reflection"),
        ):
            # Simulate DB being set
            rt._db = MagicMock()

            # Mock memory init to set retriever
            with (
                patch("genesis.runtime.EmbeddingProvider", create=True),
                patch("genesis.runtime.QdrantClient", create=True),
                patch("genesis.runtime.MemoryLinker", create=True),
                patch("genesis.runtime.MemoryStore", create=True),
                patch("genesis.runtime.HybridRetriever", create=True),
                patch("genesis.runtime.ContextInjector", create=True),
            ):
                await rt._init_memory()

            assert rt.hybrid_retriever is not None
            assert rt.context_injector is not None

        GenesisRuntime.reset()
