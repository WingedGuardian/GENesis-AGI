"""Shared fixtures for memory-integrity tests.

Install-agnostic: a real file-backed SQLite schema in ``tmp_path`` (the checker
opens its own ``mode=ro`` connection to a path, so it needs a file, not
``:memory:``) and a ``FakeQdrantClient`` with just the ``scroll``/``retrieve``
surface the checker uses — no network, no live Qdrant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiosqlite


@dataclass
class _Point:
    id: str
    payload: dict[str, Any] = field(default_factory=dict)


class FakeQdrantClient:
    """Minimal in-memory stand-in for QdrantClient.

    ``points``: ``{collection: {point_id: payload_dict}}``. Set ``raise_on`` to
    a method name (``"scroll"``/``"retrieve"``) to simulate an unreachable
    Qdrant — the method raises ``ConnectionError`` (caught by the checker's
    ``_QDRANT_ERRORS`` and mapped to status='unknown').
    """

    def __init__(
        self,
        points: dict[str, dict[str, dict]] | None = None,
        *,
        raise_on: str | None = None,
    ) -> None:
        self.points = points or {}
        self.raise_on = raise_on

    def scroll(
        self,
        *,
        collection_name: str,
        limit: int = 1000,
        offset: str | None = None,
        scroll_filter=None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ):
        if self.raise_on == "scroll":
            raise ConnectionError("fake qdrant down")
        coll = self.points.get(collection_name, {})
        ids = sorted(coll.keys())
        start = 0
        if offset is not None:
            # offset is the id to resume *at* (Qdrant returns the offset point as
            # the first of the next page); find its index.
            start = ids.index(offset) if offset in ids else len(ids)
        page = ids[start : start + limit]
        pts = [_Point(pid, coll[pid]) for pid in page]
        next_offset = None
        end = start + limit
        if end < len(ids):
            next_offset = ids[end]
        return pts, next_offset

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        with_payload: bool = False,
        with_vectors: bool = False,
    ):
        if self.raise_on == "retrieve":
            raise ConnectionError("fake qdrant down")
        coll = self.points.get(collection_name, {})
        return [_Point(pid, coll[pid]) for pid in ids if pid in coll]


async def build_db(path: str) -> None:
    """Create a full-schema file DB at *path* (fresh-install tables)."""
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(path)
    await create_all_tables(conn)
    await conn.commit()
    await conn.close()


async def insert_memory(
    path: str,
    memory_id: str,
    *,
    status: str = "embedded",
    collection: str = "episodic_memory",
    deprecated: int = 0,
    in_fts: bool = True,
) -> None:
    """Insert one memory into memory_metadata (+ optionally memory_fts)."""
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection, embedding_status, deprecated) "
        "VALUES (?, datetime('now'), ?, ?, ?)",
        (memory_id, collection, status, deprecated),
    )
    if in_fts:
        await conn.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'test', '', ?)",
            (memory_id, f"content for {memory_id}", collection),
        )
    await conn.commit()
    await conn.close()
