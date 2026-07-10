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
