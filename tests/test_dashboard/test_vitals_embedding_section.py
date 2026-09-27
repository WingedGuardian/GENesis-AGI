"""The embedding vitals panel reports the chains the runtime actually built.

It used to re-derive both chains from env and name the Ollama model as the
"active" one unconditionally — so with cloud-first storage it showed the local
fallback's model while the cloud backend was writing every vector.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.dashboard.routes.vitals import _build_embedding_section
from genesis.memory.embeddings import (
    CANONICAL_VECTOR_SPACE,
    DeepInfraBackend,
    EmbeddingProvider,
    OllamaBackend,
)


def _provider(*backends):
    return EmbeddingProvider(backends=list(backends), cache_dir=None)


@pytest.mark.asyncio
async def test_active_model_is_the_storage_chains_first_rung():
    deepinfra = DeepInfraBackend(api_key="k", client=MagicMock())
    ollama = OllamaBackend(url="http://x", model="qwen3-embedding:0.6b-fp16", client=MagicMock())
    rt = SimpleNamespace(
        db=None,
        _storage_embedder=_provider(deepinfra, ollama),
        _recall_embedder=_provider(deepinfra, ollama),
    )

    section = await _build_embedding_section(rt)

    assert section["storage_chain"] == ["deepinfra", "ollama"]
    assert section["recall_chain"] == ["deepinfra", "ollama"]
    assert section["active_model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert section["vector_space"] == CANONICAL_VECTOR_SPACE


@pytest.mark.asyncio
async def test_local_first_chain_reports_the_local_model():
    """Control: the field follows the chain, it is not pinned to the cloud model."""
    ollama = OllamaBackend(url="http://x", model="qwen3-embedding:0.6b-fp16", client=MagicMock())
    rt = SimpleNamespace(db=None, _storage_embedder=_provider(ollama), _recall_embedder=None)

    section = await _build_embedding_section(rt)

    assert section["active_model"] == "qwen3-embedding:0.6b-fp16"
    assert section["recall_chain"] == ["not initialized"]


@pytest.mark.asyncio
async def test_no_embedders_reports_not_initialized():
    section = await _build_embedding_section(SimpleNamespace(db=None))
    assert section["storage_chain"] == ["not initialized"]
    assert section["active_model"] is None
    assert section["vector_space"] is None


@pytest.mark.asyncio
async def test_mock_runtime_never_leaks_mock_values_into_json():
    section = await _build_embedding_section(MagicMock(db=None))
    assert section["active_model"] is None or isinstance(section["active_model"], str)
    assert section["vector_space"] is None or isinstance(section["vector_space"], str)
