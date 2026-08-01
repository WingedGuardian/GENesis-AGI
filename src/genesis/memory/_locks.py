"""Per-memory-id async locks — serialize concurrent mutations of ONE memory.

SQLite and Qdrant share no transaction, so operations on the same memory can
interleave and leave the two stores disagreeing:

- ``MemoryStore.delete()`` removes the point then cascades the SQLite rows;
- ``EmbeddingRecoveryWorker`` re-embeds a pending row then upserts the point;
- the reconcile lane re-reads a lying mirror then re-queues it for re-embed.

A delete landing *between* another path's "is this memory still here?" check and
its point write resurrects a vector the user deleted as a metadata-less ghost —
and ghosts DO surface in recall (the retriever assembles raw Qdrant payloads
without a metadata-existence join) until the reconcile lane sweeps them. These
process-local per-id locks make each memory's delete atomic against its
re-embed/requeue, closing that window within one process.

Scope is exactly one ``memory_id`` — different memories never contend, so this
adds no cross-memory serialization. PROCESS-LOCAL ONLY: the recovery worker and
reconcile lane run in genesis-server, but ``MemoryStore.delete()`` ALSO runs in
the separate genesis-memory MCP process (``reference_delete``; ingest stale-point
cleanup), which this lock cannot serialize against — that cross-process window
is closed by the delete-intent tombstones (PR-2), not this lock. A crash
*between* the point write and the SQLite commit is likewise a separate (rare)
window covered by the reconcile lane.

Covers the delete-vs-{re-embed, reconcile-requeue} triad — the paths that can
RESURRECT a deleted vector. ``MemoryStore._mark_superseded`` also writes the same
``memory_id`` across both stores but is deliberately NOT wrapped here: it cannot
resurrect a vector (a ``set_payload`` on a missing point is a no-op), and its one
residual race — a dangling ``succeeded_by`` link for a concurrently-deleted
memory — needs an existence-guard on the link insert, not just this lock (tracked
separately, pre-existing).

Deadlock-free by construction: every holder takes at most ONE id-lock at a time
and never nests, ``store()`` takes no lock, and the slow embed happens OUTSIDE
the worker's locked section — so no lock is ever held across an unbounded wait.
"""

from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

# Weak values: a lock is GC'd once no coroutine holds it, so the registry never
# grows unbounded across a long-lived process. While a critical section runs,
# the ``async with`` expression holds a strong reference, so the entry cannot be
# collected mid-use and two callers for the same id observe the SAME lock.
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def memory_id_lock(memory_id: str) -> asyncio.Lock:
    """Return the process-local ``asyncio.Lock`` for *memory_id* (created on demand).

    Safe without a registry guard: this function performs NO awaits, so on the
    single-threaded event loop the get-or-create is atomic — no two callers can
    race to create competing locks for the same id.
    """
    lock = _locks.get(memory_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[memory_id] = lock
    return lock
