"""observation_write stamps the dispatching session's origin_class (WS-3).

Before this fix, observation_write was the one memory writer that dropped the
session origin — an external-origin session (e.g. the inbox judge running over
untrusted content) wrote a NULL origin that every origin-aware reader treated as
first-party "by omission", letting a forged user_model_delta slip past the
user-model consumer gate. This proves the tool now forwards the session origin
like its sibling writers (memory_store / procedure_store / knowledge).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.mcp.memory_mcp import mcp


async def _get_tools():
    return await mcp.get_tools()


async def _write_and_read_origin(monkeypatch, origin_env: str | None) -> str:
    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables

        await create_all_tables(real_db)
        await real_db.commit()

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            if origin_env is None:
                monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
            else:
                monkeypatch.setenv("GENESIS_SESSION_ORIGIN", origin_env)

            tools = await _get_tools()
            obs_id = await tools["observation_write"].fn(
                content="the user prefers Rust",
                source="inbox_evaluation",
                type="user_model_delta",
            )
            assert obs_id and obs_id != "duplicate_skipped"
            cursor = await real_db.execute(
                "SELECT origin_class FROM observations WHERE id = ?", (obs_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            return row["origin_class"]
        finally:
            mod._store, mod._db, mod._retriever = old_store, old_db, old_retriever


@pytest.mark.asyncio
async def test_external_session_stamps_external_origin(monkeypatch):
    """The inbox-judge case: origin env → stored external_untrusted, so the
    consumer gate can bar a forged user_model_delta."""
    assert await _write_and_read_origin(monkeypatch, "external_untrusted") == "external_untrusted"


@pytest.mark.asyncio
async def test_unset_session_defaults_first_party(monkeypatch):
    """Server/foreground writers (no env stamp) coalesce to first_party — NOT
    a raw NULL (which the gate would treat adversarially as untrusted)."""
    assert await _write_and_read_origin(monkeypatch, None) == "first_party"


@pytest.mark.asyncio
async def test_invalid_session_origin_defaults_first_party(monkeypatch):
    """session_origin_from_env returns None on a garbage value → coalesced to
    first_party (the producer side validates loudly; this side is fail-safe)."""
    assert await _write_and_read_origin(monkeypatch, "not_a_real_origin") == "first_party"
