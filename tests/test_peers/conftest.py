"""Scratch peer database; no live runtime or configuration."""

import aiosqlite
import pytest

from genesis.db.schema import TABLES
from genesis.peers.registry import PeerRegistry


@pytest.fixture
async def registry(tmp_path):
    path = tmp_path / "peers.db"
    async with aiosqlite.connect(path) as db:
        for name in ("peer_settings", "peers", "peer_grants"):
            await db.execute(TABLES[name])
        await db.commit()
    return PeerRegistry(path)
