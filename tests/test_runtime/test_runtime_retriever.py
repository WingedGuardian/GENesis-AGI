"""Tests for hybrid_retriever and context_injector on GenesisRuntime."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.runtime import GenesisRuntime


def _qdrant_available() -> bool:
    """Check if a Qdrant instance is reachable. The bootstrap test's patches
    target ``genesis.runtime.QdrantClient`` etc., but the real imports in
    ``init/memory.py`` are lazy (imported inside the function body) and thus
    not intercepted. As a result the real ``_init_memory`` runs and requires
    Qdrant to be up. Rather than rewrite the patching strategy (which
    requires a deeper refactor), skip the test when Qdrant is unreachable."""
    try:
        from qdrant_client import QdrantClient

        from genesis.env import qdrant_url

        QdrantClient(url=qdrant_url(), timeout=2).get_collections()
        return True
    except Exception:
        return False


class TestRuntimeRetrieverProperties:
    def test_retriever_none_before_bootstrap(self):
        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()
        assert rt.hybrid_retriever is None
        assert rt.context_injector is None
        GenesisRuntime.reset()

    @pytest.mark.skipif(
        not _qdrant_available(),
        reason="Qdrant not reachable; bootstrap test requires real Qdrant "
               "because its mocks don't patch the lazy imports in "
               "genesis.runtime.init.memory",
    )
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
