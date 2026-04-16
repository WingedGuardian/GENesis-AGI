"""Step 2.4 — Dual-write observation writer (DB + MemoryStore)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from genesis.db.crud import observations
from genesis.db.crud.observations import _TTL_BY_TYPE, _TTL_PREFIX

logger = logging.getLogger(__name__)

# Observation types where vector embedding adds no retrieval value.
# These are operational metadata stored for DB querying only, not semantic search.
_SKIP_EMBED_TYPES: frozenset[str] = frozenset({
    "memory_operation",      # GROUNDWORK for V4, never retrieved
    "build_state",           # snapshot metadata, never retrieved
    "project_context",       # snapshot metadata, never retrieved
    "version_current",       # written via raw SQL in cc_version.py; listed for completeness
})

if TYPE_CHECKING:
    import aiosqlite


class _MemoryStore(Protocol):
    async def store(
        self,
        content: str,
        source: str,
        *,
        memory_type: str = ...,
        tags: list[str] | None = ...,
        confidence: float = ...,
        auto_link: bool = ...,
        source_pipeline: str | None = ...,
    ) -> str: ...


class ObservationWriter:
    """Write observations to the DB and optionally to the MemoryStore."""

    def __init__(self, memory_store: _MemoryStore | None = None) -> None:
        self._memory_store = memory_store

    async def write(
        self,
        db: aiosqlite.Connection,
        *,
        source: str,
        type: str,
        content: str,
        priority: str,
        category: str | None = None,
        content_hash: str | None = None,
    ) -> str:
        """Dual-write: observations table + MemoryStore (if available)."""
        obs_id = str(uuid.uuid4())
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()

        # Compute TTL-based expiry
        expires_at = self._compute_expires_at(type, now_dt)

        await observations.create(
            db,
            id=obs_id,
            source=source,
            type=type,
            content=content,
            priority=priority,
            category=category,
            created_at=now,
            content_hash=content_hash,
            expires_at=expires_at,
        )

        if self._memory_store is not None and type not in _SKIP_EMBED_TYPES:
            try:
                await self._memory_store.store(
                    content,
                    source,
                    memory_type="episodic",
                    tags=[type, f"obs:{obs_id}"],
                    confidence=0.6,
                    source_pipeline=source,
                )
            except Exception:
                logger.warning("memory store write failed for observation %s", obs_id, exc_info=True)

        return obs_id

    @staticmethod
    def _compute_expires_at(obs_type: str, now: datetime) -> str | None:
        """Return ISO expiry timestamp based on observation type, or None if persistent."""
        ttl = _TTL_BY_TYPE.get(obs_type)
        if ttl is None:
            for prefix, prefix_ttl in _TTL_PREFIX:
                if obs_type.startswith(prefix):
                    ttl = prefix_ttl
                    break
        if ttl is None:
            return None
        return (now + ttl).isoformat()
