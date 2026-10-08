"""Reserved IDs isolate explicit external namespaces from ordinary exact dedup."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from genesis.db.connection import connect_aiosqlite_rw
from genesis.env import db_busy_timeout_ms


def is_namespaced_id(memory_id: str) -> bool:
    """Same reserved UUIDv8 marker used by ordinary SQL dedup exclusion."""
    return len(memory_id) == 36 and memory_id[14] == "8"


@asynccontextmanager
async def write_lock(path: Path, memory_id: str):
    """Kernel-owned per-ID lock; cancellation/crash cannot strand a lease."""
    path = Path(path)
    if str(uuid.UUID(memory_id)) != memory_id or uuid.UUID(memory_id).version != 8:
        raise ValueError("invalid namespaced memory ID")
    directory = path.parent / ".memory-namespace-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(
        directory / (memory_id + ".lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        async with asyncio.timeout(db_busy_timeout_ms() / 1000):
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
        yield
    finally:
        # Never unlink: another process may already have opened the same inode.
        os.close(fd)


async def drain_operation(operation):
    """Drain an in-flight effect before a canceled holder releases its lock."""
    task = asyncio.create_task(operation)
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            if not canceled:
                raise
    if canceled:
        # Retrieve a failed thread's exception without replacing cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


@dataclass(frozen=True)
class DedupNamespace:
    peer_id: str
    collection: str
    origin_class: str = "external_untrusted"

    def __post_init__(self):
        if (
            not isinstance(self.peer_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.peer_id)
            or not isinstance(self.collection, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.collection)
            or self.origin_class != "external_untrusted"
        ):
            raise ValueError("invalid external memory namespace")

    def key(self, content: str) -> tuple[str, str, str, str]:
        return (
            self.peer_id,
            self.origin_class,
            self.collection,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def memory_id(self, content: str) -> str:
        encoded = json.dumps(
            ["genesis-memory-namespace-v1", *self.key(content)], separators=(",", ":")
        ).encode("utf-8")
        raw = bytearray(hashlib.sha256(encoded).digest()[:16])
        # RFC9562 UUIDv8: SHA256-derived names, distinct from legacy UUIDv4.
        raw[6] = (raw[6] & 15) | 128
        raw[8] = (raw[8] & 63) | 128
        return str(uuid.UUID(bytes=bytes(raw)))


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
        if row["status"] == "deleted":
            raise ValueError("deleted peer knowledge requires owner restoration")
        fts = await (
            await db.execute("SELECT content FROM memory_fts WHERE memory_id=?", (memory_id,))
        ).fetchall()
        metadata = await (
            await db.execute(
                "SELECT collection,origin_class FROM memory_metadata WHERE memory_id=?",
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


@asynccontextmanager
async def recovery_guard(db, memory_id: str):
    """Ordinary recovery is unchanged; namespaced recovery joins writer locks."""
    if not is_namespaced_id(memory_id):
        yield True
        return
    rows = await (await db.execute("PRAGMA database_list")).fetchall()
    path = next((row[2] for row in rows if row[1] == "main" and row[2]), None)
    if path is None:
        raise ValueError("namespaced recovery requires a file-backed database")
    async with write_lock(Path(path), memory_id):
        row = await (
            await db.execute("SELECT status FROM memory_namespaces WHERE memory_id=?", (memory_id,))
        ).fetchone()
        yield row is not None and row[0] != "deleted"
