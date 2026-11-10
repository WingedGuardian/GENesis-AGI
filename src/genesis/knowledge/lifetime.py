"""Coordinate knowledge reference publication and ingest-owned vector cleanup.

All four ingest publishers hold writer from before dedup until cleanup settles.
This is not a lock around explicit user deletes or arbitrary MemoryStore calls.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakValueDictionary

from genesis.db.crud import knowledge

logger = logging.getLogger(__name__)

_MEMORY_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def writer(db=None, *, path: Path | None = None):
    """Hold the actual database's stable lock inode without a SQLite writer slot.

    PRAGMA identity covers raw, serialized and request-owned connections. Named
    databases coordinate across processes; private in-memory databases share a
    lock for their single connection facade. Never unlink the lock file.
    """
    if path is None:
        records = await db.execute_fetchall("PRAGMA database_list")
        main = [row[2] for row in records if row[1] == "main"]
        if len(main) != 1 or not isinstance(main[0], str):
            raise RuntimeError("Cannot identify knowledge database for write coordination")
        if not main[0]:
            key = str(id(db))
            lock = _MEMORY_LOCKS.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _MEMORY_LOCKS[key] = lock
            async with lock:
                yield
            return
        path = Path(main[0])
    lock_path = Path(str(path.resolve()) + ".knowledge.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Same cooperative polling cadence as the checkout lock.
                await asyncio.sleep(0.25)
        yield
    finally:
        os.close(fd)  # Releases a won lock, or a cancelled wait's descriptor.


async def unreferenced(db, ids: list[str]) -> list[str]:
    """Select globally unreferenced candidates while the caller holds writer."""
    candidates = list(dict.fromkeys(ids))
    referenced = set()
    try:
        for offset in range(0, len(candidates), 400):
            referenced.update(await knowledge.get_by_qdrant_ids(db, candidates[offset:offset + 400]))
    except Exception:
        logger.warning("Knowledge reference lookup failed; cleanup skipped", exc_info=True)
        return []
    return [qid for qid in candidates if qid not in referenced]
