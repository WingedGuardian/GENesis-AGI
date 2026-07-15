"""Tests for the GENESIS_MEMORY_WRITEBACKS_OFF eval-harness seam.

Recall is read-mostly, not read-only: it bumps retrieved_count /
last_retrieved_at in Qdrant (shared prod instance!) and SQLite on every hit.
The bench harness suppresses these write-backs so frozen-snapshot recalls
neither pollute prod Qdrant payloads nor re-rank memories mid-run. These
tests pin the seam at all three recall-path Qdrant writers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from genesis.env import memory_writebacks_off


class TestEnvHelper:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("GENESIS_MEMORY_WRITEBACKS_OFF", raising=False)
        assert memory_writebacks_off() is False

    def test_truthy_values(self, monkeypatch):
        for val in ("1", "true", "yes"):
            monkeypatch.setenv("GENESIS_MEMORY_WRITEBACKS_OFF", val)
            assert memory_writebacks_off() is True, val

    def test_falsy_values(self, monkeypatch):
        for val in ("", "0", "false", "off"):
            monkeypatch.setenv("GENESIS_MEMORY_WRITEBACKS_OFF", val)
            assert memory_writebacks_off() is False, val


class TestIncrementRetrievedGuard:
    """mcp/memory/core._increment_retrieved — the drift/fallback bump."""

    def _result(self):
        r = MagicMock()
        r.memory_id = "m1"
        return r

    def test_suppressed_when_flag_set(self, monkeypatch):
        from genesis.mcp.memory.core import _increment_retrieved

        monkeypatch.setenv("GENESIS_MEMORY_WRITEBACKS_OFF", "1")
        qdrant = MagicMock()
        _increment_retrieved(qdrant, [self._result()])
        qdrant.retrieve.assert_not_called()

    def test_writes_when_flag_unset(self, monkeypatch):
        from genesis.mcp.memory.core import _increment_retrieved

        monkeypatch.delenv("GENESIS_MEMORY_WRITEBACKS_OFF", raising=False)
        qdrant = MagicMock()
        qdrant.retrieve.return_value = []  # point not found → no update call
        _increment_retrieved(qdrant, [self._result()])
        qdrant.retrieve.assert_called()


class TestRecordRetrievalsGuard:
    """HybridRetriever._record_retrievals — the main stage-11 write-back.

    The guard is a top-of-function early return, so a bare instance
    (``__new__``, no init) proves it: with the flag set, NO attribute on the
    instance is touched; with it unset, the first Qdrant write is attempted.
    """

    def _bare_retriever(self):
        from genesis.memory.retrieval import HybridRetriever

        rt = HybridRetriever.__new__(HybridRetriever)
        rt._qdrant = MagicMock()
        rt._db = MagicMock()
        return rt

    async def test_suppressed_when_flag_set(self, monkeypatch):
        monkeypatch.setenv("GENESIS_MEMORY_WRITEBACKS_OFF", "1")
        rt = self._bare_retriever()
        await rt._record_retrievals(
            top=["m1"],
            qdrant_by_id={"m1": {"payload": {"retrieved_count": 3}}},
            fts_by_id={},
            now_str="2026-01-01T00:00:00+00:00",
        )
        rt._qdrant.assert_not_called()

    async def test_writes_when_flag_unset(self, monkeypatch):
        from genesis.memory import retrieval as retrieval_mod

        monkeypatch.delenv("GENESIS_MEMORY_WRITEBACKS_OFF", raising=False)
        update = MagicMock()
        monkeypatch.setattr(retrieval_mod.qdrant_ops, "update_payload", update)
        rt = self._bare_retriever()
        await rt._record_retrievals(
            top=["m1"],
            qdrant_by_id={
                "m1": {"payload": {"retrieved_count": 3}, "_collection": "episodic_memory"},
            },
            fts_by_id={},
            now_str="2026-01-01T00:00:00+00:00",
        )
        update.assert_called_once()
        payload = update.call_args.kwargs["payload"]
        assert payload["retrieved_count"] == 4


class TestSkipWritebackSeam:
    """recall(skip_writeback=...) — the enforce-drop write-back exclusion.

    An item the caller will enforce-drop must not gain retrieved_count /
    activation credit from the very recall that blocks it (Codex #1048 —
    blocked external content would otherwise farm ranking energy from every
    dispatched session that matches it). The write-backs were moved AFTER
    the 12b stored-origin backfill precisely so the predicate sees the
    STORED origin, not a stale/absent payload value — pinned here by giving
    the rows no origin at all and backfilling from the (mocked) SQLite side.
    """

    def _harness(self):
        from unittest.mock import AsyncMock

        from genesis.memory.embeddings import EmbeddingUnavailableError
        from genesis.memory.retrieval import HybridRetriever

        embed_provider = MagicMock()
        embed_provider.embed = AsyncMock(side_effect=EmbeddingUnavailableError("down"))
        retriever = HybridRetriever(
            embedding_provider=embed_provider,
            qdrant_client=MagicMock(),
            db=MagicMock(spec_set=["execute", "commit"]),
        )
        return retriever

    @staticmethod
    def _fts_row(mid: str, rank: float) -> dict:
        return {
            "memory_id": mid,
            "content": f"fts content for {mid}",
            "source_type": "memory",
            "collection": "episodic_memory",
            "rank": rank,
        }

    def _patch_crud(self, monkeypatch, origin_by_id: dict[str, str]):
        from unittest.mock import AsyncMock

        from genesis.memory import retrieval as retrieval_mod

        monkeypatch.setattr(
            retrieval_mod,
            "expand_query",
            AsyncMock(return_value="q"),
        )
        crud = retrieval_mod.memory_crud
        monkeypatch.setattr(
            crud,
            "search_ranked",
            AsyncMock(return_value=[self._fts_row("ext-1", -5.0), self._fts_row("fp-1", -3.0)]),
        )
        monkeypatch.setattr(crud, "batch_created_at", AsyncMock(return_value={}))
        monkeypatch.setattr(crud, "origin_class_by_ids", AsyncMock(return_value=origin_by_id))
        links = retrieval_mod.memory_links
        monkeypatch.setattr(links, "count_links", AsyncMock(return_value=0))
        monkeypatch.setattr(links, "batch_link_counts", AsyncMock(return_value={}))
        monkeypatch.setattr(links, "inter_candidate_links", AsyncMock(return_value=[]))

    async def test_skipped_item_excluded_and_predicate_sees_stored_origin(self, monkeypatch):
        from unittest.mock import AsyncMock

        retriever = self._harness()
        # Rows carry NO origin — only the 12b SQLite backfill provides it.
        self._patch_crud(
            monkeypatch,
            {"ext-1": "external_untrusted", "fp-1": "first_party"},
        )
        spy = AsyncMock()
        retriever._record_retrievals = spy

        results = await retriever.recall(
            "q",
            limit=10,
            skip_writeback=lambda r: r.origin_class == "external_untrusted",
        )

        assert {r.memory_id for r in results} == {"ext-1", "fp-1"}
        spy.assert_awaited_once()
        assert spy.call_args.args[0] == ["fp-1"], (
            "enforce-dropped item must not receive retrieval credit"
        )

    async def test_no_predicate_keeps_full_writeback_set(self, monkeypatch):
        from unittest.mock import AsyncMock

        retriever = self._harness()
        self._patch_crud(monkeypatch, {})
        spy = AsyncMock()
        retriever._record_retrievals = spy

        await retriever.recall("q", limit=10)

        spy.assert_awaited_once()
        assert set(spy.call_args.args[0]) == {"ext-1", "fp-1"}

    async def test_raising_predicate_fails_open_to_full_writebacks(self, monkeypatch):
        from unittest.mock import AsyncMock

        retriever = self._harness()
        self._patch_crud(monkeypatch, {})
        spy = AsyncMock()
        retriever._record_retrievals = spy

        def _boom(_r):
            raise RuntimeError("predicate exploded")

        await retriever.recall("q", limit=10, skip_writeback=_boom)

        spy.assert_awaited_once()
        assert set(spy.call_args.args[0]) == {"ext-1", "fp-1"}
