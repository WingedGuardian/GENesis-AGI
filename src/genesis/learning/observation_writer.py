"""Step 2.4 — Dual-write observation writer (DB + MemoryStore)."""

from __future__ import annotations

import hashlib
import logging
import re as _re
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

# Map ObservationWriter `source` values to a subsystem tag. Subsystem-only:
# user-sourced sources (user_reply, direct_message, auto_memory_harvest,
# cc_debrief) intentionally absent so they keep source_subsystem=NULL and
# remain in foreground recall by default. Unknown sources also stay NULL —
# new sources must be explicitly classified before being filtered.
_SUBSYSTEM_FROM_SOURCE: dict[str, str] = {
    "stability_monitor":   "reflection",
    "deep_reflection":     "reflection",
    "light_reflection":    "reflection",
    "micro_reflection":    "reflection",
    "quality_calibration": "reflection",
    "weekly_assessment":   "reflection",
    "surplus_promotion":   "reflection",
    "retrospective":       "reflection",
}

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
        source_subsystem: str | None = ...,
        invalid_at: str | None = ...,
    ) -> str: ...


def _normalize_for_dedup(text: str) -> str:
    """Normalize numeric variation for semantic dedup.

    Targets: metric values (key=N), JSON numeric values ("key": N),
    comma-separated counts, standalone floats/ints in narrative.
    Preserves: hex hashes, UUIDs, IPs, ISO timestamps, version strings.

    Steps are ordered specific->general so structural patterns (JSON values)
    are caught first, and the general standalone-int rule only handles leftovers.
    """
    # 1. Metric-style: key=value (existing pattern)
    out = _re.sub(r"(?<==)\d+\.?\d*", "N", text)
    # 2. JSON numeric values: "key": 0.85 or "key": 42
    out = _re.sub(r'("[\w_]+":\s*)\d+\.?\d*', r"\1N", out)
    # 3. Comma-separated counts: 6,260
    out = _re.sub(r"\b\d{1,3}(?:,\d{3})+\b", "N", out)
    # 4. Standalone floats in narrative: ~31.7h, 0.92
    out = _re.sub(r"(?<![0-9a-fA-F.:\-])\d+\.\d+(?![0-9a-fA-F.:\-])", "N", out)
    # 5. Standalone ints 1-5 digits, not part of hex/UUID/IP/timestamp
    out = _re.sub(r"(?<![0-9a-fA-F.\-:])\b\d{1,5}\b(?![0-9a-fA-F\-:.])", "N", out)
    return out.strip().lower()


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

        # Compute normalized content_hash for dedup if not provided.
        if content_hash is None:
            norm = _normalize_for_dedup(f"{type}:{content}")
            content_hash = hashlib.sha256(norm.encode()).hexdigest()

        result = await observations.create(
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
            skip_if_duplicate=True,
        )
        if result is None:
            logger.debug(
                "Observation dedup: skipped duplicate %s (hash=%s)",
                type, content_hash[:12],
            )
            return obs_id

        if self._memory_store is not None and type not in _SKIP_EMBED_TYPES:
            subsystem = _SUBSYSTEM_FROM_SOURCE.get(source)
            # Phase 1.5e: propagate the observation's TTL to the MemoryStore
            # copy. When the observation expires (`resolve_expired()` sweep
            # at runtime/init/learning.py), the dual-write memory row also
            # drops out of recall via the always-on invalid_at filter.
            # Without this, the memory row would persist forever.
            try:
                await self._memory_store.store(
                    content,
                    source,
                    memory_type="episodic",
                    tags=[type, f"obs:{obs_id}"],
                    confidence=0.6,
                    source_pipeline=source,
                    source_subsystem=subsystem,
                    invalid_at=expires_at,
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
