"""ObservationWriter propagates observation TTL to the MemoryStore copy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.learning.observation_writer import ObservationWriter


async def _build_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute("""
        CREATE TABLE observations (
            id TEXT PRIMARY KEY,
            person_id TEXT,
            source TEXT NOT NULL,
            type TEXT NOT NULL,
            category TEXT,
            content TEXT NOT NULL,
            priority TEXT NOT NULL CHECK (priority IN ('low','medium','high','critical')),
            speculative INTEGER NOT NULL DEFAULT 0,
            retrieved_count INTEGER NOT NULL DEFAULT 0,
            influenced_action INTEGER NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0,
            resolved_at TEXT,
            resolution_notes TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            content_hash TEXT
        )
    """)
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_observation_ttl_propagated_to_memory_store(tmp_path) -> None:
    """The observation's computed `expires_at` is passed as `invalid_at`
    to the MemoryStore copy, so the dual-write row expires alongside the
    observation."""
    db = await _build_db(str(tmp_path / "t.db"))
    try:
        store_mock = MagicMock()
        store_mock.store = AsyncMock(return_value="mem-id")
        writer = ObservationWriter(memory_store=store_mock)

        await writer.write(
            db,
            source="deep_reflection",
            type="reflection_observation",
            content="some reflection",
            priority="medium",
        )

        store_mock.store.assert_awaited_once()
        kwargs = store_mock.store.await_args.kwargs
        assert kwargs["invalid_at"] is not None, (
            "ObservationWriter must propagate expires_at as invalid_at"
        )
        assert kwargs["source_subsystem"] == "reflection"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_observation_no_ttl_passes_none(tmp_path) -> None:
    """Observation types with no TTL (rare, e.g. persistent types) pass
    `invalid_at=None` — the memory row stays valid forever."""
    db = await _build_db(str(tmp_path / "t.db"))
    try:
        store_mock = MagicMock()
        store_mock.store = AsyncMock(return_value="mem-id")
        writer = ObservationWriter(memory_store=store_mock)

        # Use a type that's NOT in _TTL_BY_TYPE and not _TTL_PREFIX-matched.
        # But _DEFAULT_TTL = 14 days kicks in via _resolve_ttl, so unless
        # _compute_expires_at returns None, every type gets a TTL.
        # Patch _compute_expires_at to verify the None branch is honored.
        with patch.object(
            ObservationWriter, "_compute_expires_at", return_value=None,
        ):
            await writer.write(
                db,
                source="user_reply",
                type="never_expires_type",
                content="some content",
                priority="medium",
            )

        store_mock.store.assert_awaited_once()
        kwargs = store_mock.store.await_args.kwargs
        assert kwargs["invalid_at"] is None
        # user_reply maps to no subsystem (user-sourced)
        assert kwargs["source_subsystem"] is None
    finally:
        await db.close()
