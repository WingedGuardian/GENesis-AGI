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

from genesis.db.crud.memory_namespaces import (
    _connection as _connection,
)
from genesis.db.crud.memory_namespaces import (
    complete as complete,
)
from genesis.db.crud.memory_namespaces import (
    mark_deleted as mark_deleted,
)
from genesis.db.crud.memory_namespaces import (
    recovery_allowed,
)
from genesis.db.crud.memory_namespaces import (
    reserve as reserve,
)
from genesis.env import db_busy_timeout_ms


def is_namespaced_id(memory_id: str) -> bool:
    """Same reserved UUIDv8 marker used by ordinary SQL dedup exclusion."""
    return len(memory_id) == 36 and memory_id[14] == "8"


# An owner deletion waits for a writer that holds this ID across its whole
# pipeline (embedding chain, vector upsert, SQLite writes), so the SQLite busy
# timeout is the wrong bound for it. Five minutes covers the embedding fallback
# chain at its per-request timeouts; on expiry the deletion still records its
# permanent identity tombstone and an open delete intent that the reconcile lane
# completes, so expiry delays removal and loses nothing.
DELETE_LOCK_WAIT_S = 300.0


class NamespaceLockBusy(TimeoutError):
    """Raised only while ACQUIRING a per-ID lock, never from the locked body."""


@asynccontextmanager
async def write_lock(path: Path, memory_id: str, *, timeout: float | None = None):
    """Kernel-owned per-ID lock; cancellation/crash cannot strand a lease.

    ``timeout`` bounds acquisition only (default: the SQLite busy timeout).
    """
    path = Path(path)
    wait = db_busy_timeout_ms() / 1000 if timeout is None else timeout
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
        try:
            async with asyncio.timeout(wait):
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise NamespaceLockBusy(f"namespaced memory {memory_id} is busy") from exc
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


async def defer_busy_deletion(db, path: Path, memory_id: str) -> dict:
    """Owner deletion that could not get the writer lock in time.

    The identity tombstone lands now, so no re-offer or in-flight retry can
    complete this ID; the open delete intent makes the reconcile lane finish the
    removal, exactly like a delete deferred by an unreachable vector store.
    """
    tombstoned = await drain_operation(prepare_deletion(db, path, memory_id))
    return {"deferred": True, "tombstoned": tombstoned, "metadata": False, "fts5": False}


async def prepare_deletion(db, path: Path, memory_id: str) -> bool:
    """Require the canonical durable intent before permanently denying retry."""
    from genesis.memory.delete_tombstones import enqueue_tombstone

    if not await enqueue_tombstone(db, memory_id=memory_id, reason="namespace_delete"):
        return False
    await mark_deleted(path, memory_id)
    return True


async def lifecycle_write(db, memory_id: str, operation):
    """Serialize a named owner/requeue SQL commit against peer retry/recovery."""
    if not is_namespaced_id(memory_id):
        return await operation()
    if db.in_transaction:
        raise ValueError("namespace lifecycle cannot join an open writer transaction")
    async with write_lock(await database_path(db), memory_id):
        return await drain_operation(operation())


async def database_path(db) -> Path:
    """The file behind a connection; namespaced effects need a lockable path."""
    rows = await (await db.execute("PRAGMA database_list")).fetchall()
    path = next((row[2] for row in rows if row[1] == "main" and row[2]), None)
    if path is None:
        raise ValueError("namespaced memory requires a file-backed database")
    return Path(path)


@asynccontextmanager
async def recovery_guard(db, memory_id: str):
    """Ordinary recovery is unchanged; namespaced recovery joins writer locks."""
    if not is_namespaced_id(memory_id):
        yield True
        return
    async with write_lock(await database_path(db), memory_id):
        from genesis.memory.delete_tombstones import has_open_tombstone

        yield await recovery_allowed(db, memory_id) and not await has_open_tombstone(
            db, memory_id=memory_id
        )
