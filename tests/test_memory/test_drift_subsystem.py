"""drift_recall subsystem-filter threading."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.drift import drift_recall


def _patches():
    return (
        patch("genesis.memory.drift.qdrant_ops"),
        patch("genesis.memory.drift.memory_crud"),
        patch("genesis.memory.drift._identify_clusters",
              new_callable=AsyncMock, return_value=(None, None)),
    )


@pytest.mark.asyncio
async def test_drift_recall_default_excludes_subsystems() -> None:
    db = MagicMock()
    qdrant_client = MagicMock()
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    qdrant_patch, crud_patch, _ = _patches()
    with qdrant_patch as mock_q, crud_patch as mock_c, _:
        mock_q.search.return_value = []
        mock_c.search_ranked = AsyncMock(return_value=[])

        await drift_recall(
            "test query", db=db, qdrant_client=qdrant_client,
            embedding_provider=embedder, source="both",
        )

        # Both _global_primer and (skipped because empty) calls
        # the filter must be passed through.
        for call in mock_q.search.call_args_list:
            assert call.kwargs.get("exclude_subsystems") == [
                "ego", "triage", "reflection",
            ]
            assert call.kwargs.get("include_only_subsystems") is None
        for call in mock_c.search_ranked.call_args_list:
            assert call.kwargs.get("exclude_subsystems") == [
                "ego", "triage", "reflection",
            ]
            assert call.kwargs.get("include_only_subsystems") is None


@pytest.mark.asyncio
async def test_drift_recall_only_subsystem() -> None:
    db = MagicMock()
    qdrant_client = MagicMock()
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    qdrant_patch, crud_patch, _ = _patches()
    with qdrant_patch as mock_q, crud_patch as mock_c, _:
        mock_q.search.return_value = []
        mock_c.search_ranked = AsyncMock(return_value=[])

        await drift_recall(
            "test query", db=db, qdrant_client=qdrant_client,
            embedding_provider=embedder, source="both",
            only_subsystem="ego",
        )

        for call in mock_q.search.call_args_list:
            assert call.kwargs.get("exclude_subsystems") is None
            assert call.kwargs.get("include_only_subsystems") == ["ego"]


@pytest.mark.asyncio
async def test_drift_recall_include_subsystem_true_no_filter() -> None:
    db = MagicMock()
    qdrant_client = MagicMock()
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    qdrant_patch, crud_patch, _ = _patches()
    with qdrant_patch as mock_q, crud_patch as mock_c, _:
        mock_q.search.return_value = []
        mock_c.search_ranked = AsyncMock(return_value=[])

        await drift_recall(
            "test query", db=db, qdrant_client=qdrant_client,
            embedding_provider=embedder, source="both",
            include_subsystem=True,
        )

        for call in mock_q.search.call_args_list:
            assert call.kwargs.get("exclude_subsystems") is None
            assert call.kwargs.get("include_only_subsystems") is None
