"""Short owned transactions for explicit peer-memory reservations."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.connection import connect_aiosqlite_rw
from genesis.env import db_busy_timeout_ms

if TYPE_CHECKING:
    from genesis.memory.namespace import DedupNamespace


@asynccontextmanager
async def _connection(path: Path):
    async with connect_aiosqlite_rw(path, existing_only=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def reserve(path: Path, namespace: DedupNamespace, content: str) -> tuple[str, bool, bool]:
    """Commit identity before any vector write; lookup failures never fail open."""
    key = namespace.key(content)
    memory_id = namespace.memory_id(content)
    async with _connection(path) as db:
        inserted = await db.execute(
            "INSERT OR IGNORE INTO memory_namespaces(peer_id,origin_class,collection,content_digest,memory_id) VALUES(?,?,?,?,?)",
            (*key, memory_id),
        )
        row = await (
            await db.execute(
                "SELECT memory_id,status FROM memory_namespaces WHERE peer_id=? AND origin_class=? AND collection=? AND content_digest=?",
                key,
            )
        ).fetchone()
        if row is None or row["memory_id"] != memory_id:
            raise ValueError("memory namespace reservation conflict")
        from genesis.memory.delete_tombstones import has_open_tombstone

        if row["status"] == "deleted" or await has_open_tombstone(db, memory_id=memory_id):
            raise ValueError("deleted peer knowledge requires owner restoration")
        fts = await (
            await db.execute("SELECT content FROM memory_fts WHERE memory_id=?", (memory_id,))
        ).fetchall()
        metadata = await (
            await db.execute(
                "SELECT collection,origin_class,deprecated,superseded_by FROM memory_metadata WHERE memory_id=?",
                (memory_id,),
            )
        ).fetchone()
        if any(entry[0] != content for entry in fts) or (
            metadata is not None
            and (
                metadata["collection"] != namespace.collection
                or metadata["origin_class"] != namespace.origin_class
            )
        ):
            raise ValueError("memory namespace storage conflict")
        if metadata is not None and (metadata["deprecated"] or metadata["superseded_by"]):
            raise ValueError("owner-superseded peer knowledge requires owner restoration")
        complete = row["status"] == "complete" and len(fts) == 1 and metadata is not None
        if not complete:
            await db.execute(
                "UPDATE memory_namespaces SET status='pending' WHERE memory_id=?", (memory_id,)
            )
        return memory_id, inserted.rowcount == 1, complete


async def complete(path: Path, namespace: DedupNamespace, content: str) -> None:
    async with _connection(path) as db:
        result = await db.execute(
            "UPDATE memory_namespaces SET status='complete' WHERE peer_id=? AND origin_class=? AND collection=? AND content_digest=? AND memory_id=? AND status='pending'",
            (*namespace.key(content), namespace.memory_id(content)),
        )
        if result.rowcount != 1:
            raise ValueError("memory namespace reservation missing")


async def mark_deleted(path: Path, memory_id: str) -> None:
    """Owner deletion permanently blocks retry/re-offer of this identity."""
    async with _connection(path) as db:
        await db.execute(
            "UPDATE memory_namespaces SET status='deleted' WHERE memory_id=?", (memory_id,)
        )


async def recovery_allowed(db, memory_id: str) -> bool:
    """A missing or permanently deleted reservation cannot write a vector."""
    row = await (
        await db.execute("SELECT status FROM memory_namespaces WHERE memory_id=?", (memory_id,))
    ).fetchone()
    return row is not None and row[0] != "deleted"
