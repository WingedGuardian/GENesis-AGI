"""CRUD for the entity layer — entities, entity_mentions, entity_links.

WS-H Pillar 2 substrate (Graphiti blueprint): typed entity nodes,
memory↔entity mentions, and bi-temporal entity↔entity relations with
provenance tags. Traversal is a plain recursive CTE — entity tables are
10²–10⁴ rows, so there is deliberately NO NetworkX cache here (that
machinery exists for the 160K-edge memory_links graph).

Naming note: this is NOT ``memory/entity_resolution.py`` (near-duplicate
memory-pair dedup) — these are entity NODES with identity.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.timeutil import canonical_iso

# Provenance weights used by traversal path-confidence (mirrored by the
# session-awareness entity lane; keep in sync deliberately, not by import,
# so the read lane stays dependency-light).
PROVENANCE_WEIGHTS = {"EXTRACTED": 1.0, "INFERRED": 0.8, "AMBIGUOUS": 0.5}

_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_MAX_LINK_TYPE_LEN = 40


def slugify_link_type(raw: str) -> str:
    """Lowercase-snake a (possibly LLM-emitted) relation name.

    Open vocabulary by design — the extraction prompt *suggests* a
    vocabulary but any sane slug is accepted; a dream-cycle report
    surfaces sprawl for humans.
    """
    slug = _SLUG_RE.sub("_", raw.strip().lower()).strip("_")
    return slug[:_MAX_LINK_TYPE_LEN] or "related_to"


async def create_entity(
    db: aiosqlite.Connection,
    *,
    name: str,
    norm_name: str,
    entity_type: str,
    summary: str | None = None,
    source: str = "extracted",
    _commit: bool = True,
) -> str:
    """Insert an entity. Returns entity_id (existing id on norm collision)."""
    now = datetime.now(UTC).isoformat()
    entity_id = str(uuid.uuid4())
    cursor = await db.execute(
        "INSERT OR IGNORE INTO entities "
        "(entity_id, name, norm_name, entity_type, summary, source, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, name, norm_name, entity_type, summary, source, now, now),
    )
    if cursor.rowcount == 0:  # UNIQUE(norm_name, entity_type) collision
        row = await get_by_norm_name(db, norm_name=norm_name, entity_type=entity_type)
        entity_id = row["entity_id"]
    if _commit:
        await db.commit()
    return entity_id


async def get_by_norm_name(
    db: aiosqlite.Connection,
    *,
    norm_name: str,
    entity_type: str | None = None,
) -> dict | None:
    """Exact norm_name lookup, optionally type-filtered. Follows merges."""
    if entity_type is not None:
        rows = await db.execute_fetchall(
            "SELECT * FROM entities WHERE norm_name = ? AND entity_type = ?",
            (norm_name, entity_type),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM entities WHERE norm_name = ?", (norm_name,),
        )
    if not rows:
        return None
    entity = _row_to_dict(db, rows[0])
    if entity["status"] == "merged" and entity["merged_into"]:
        survivor = await get_entity(db, entity["merged_into"])
        return survivor or entity
    return entity


async def get_entity(db: aiosqlite.Connection, entity_id: str) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM entities WHERE entity_id = ?", (entity_id,),
    )
    return _row_to_dict(db, rows[0]) if rows else None


async def list_norm_names(
    db: aiosqlite.Connection,
    *,
    entity_types: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """(norm_name, entity_id, entity_type) for active entities.

    Feeds the registry's fuzzy tier; entity counts are small enough to
    scan in-process.
    """
    if entity_types:
        ph = ",".join("?" * len(entity_types))
        rows = await db.execute_fetchall(
            f"SELECT norm_name, entity_id, entity_type FROM entities "  # noqa: S608
            f"WHERE status = 'active' AND entity_type IN ({ph})",
            entity_types,
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT norm_name, entity_id, entity_type FROM entities "
            "WHERE status = 'active'",
        )
    return [(r[0], r[1], r[2]) for r in rows]


async def upsert_mention(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    entity_id: str,
    provenance: str,
    confidence: float = 0.7,
    source: str | None = None,
    _commit: bool = True,
) -> None:
    """Record memory↔entity mention. Existing rows keep the STRONGER claim."""
    await db.execute(
        "INSERT INTO entity_mentions "
        "(memory_id, entity_id, provenance, confidence, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(memory_id, entity_id) DO UPDATE SET "
        "provenance = excluded.provenance, confidence = excluded.confidence, "
        "source = excluded.source "
        "WHERE excluded.confidence > entity_mentions.confidence",
        (
            memory_id, entity_id, provenance, confidence, source,
            datetime.now(UTC).isoformat(),
        ),
    )
    if _commit:
        await db.commit()


async def delete_mentions_by_memory(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    _commit: bool = True,
) -> int:
    """Delete all entity mentions for a memory. Returns count deleted.

    Mentions are written keyed by ``memory_id`` (see :func:`upsert_mention`,
    called from memory/store.py's write path). ``MemoryStore.delete`` must
    cascade here or a deleted memory leaves dangling mention rows pointing at
    a memory_id that no longer exists.
    """
    cursor = await db.execute(
        "DELETE FROM entity_mentions WHERE memory_id = ?",
        (memory_id,),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount


async def upsert_link(
    db: aiosqlite.Connection,
    *,
    source_id: str,
    target_id: str,
    link_type: str,
    provenance: str,
    confidence: float = 0.7,
    evidence_memory_id: str | None = None,
    valid_at: str | None = None,
    _commit: bool = True,
) -> None:
    """Record a typed entity relation. Bi-temporal columns canonicalized."""
    await db.execute(
        "INSERT INTO entity_links "
        "(source_id, target_id, link_type, provenance, confidence, "
        "evidence_memory_id, valid_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET "
        "provenance = excluded.provenance, confidence = excluded.confidence, "
        "evidence_memory_id = excluded.evidence_memory_id, "
        # A stronger undated claim must not erase a known valid_at; a
        # stronger dated claim must replace NULL (as_of treats NULL as
        # always-valid, which silently widens the validity interval).
        "valid_at = COALESCE(excluded.valid_at, entity_links.valid_at) "
        "WHERE excluded.confidence > entity_links.confidence",
        (
            source_id, target_id, slugify_link_type(link_type), provenance,
            confidence, evidence_memory_id, canonical_iso(valid_at),
            datetime.now(UTC).isoformat(),
        ),
    )
    if _commit:
        await db.commit()


async def invalidate_links_for_entity(
    db: aiosqlite.Connection,
    *,
    entity_id: str,
    invalid_at: str,
    invalidated_by: str,
    _commit: bool = True,
) -> int:
    """Close the validity interval on all live links touching an entity."""
    canonical = canonical_iso(invalid_at)
    if canonical is None:
        raise ValueError(f"unparseable invalid_at {invalid_at!r}")
    cursor = await db.execute(
        "UPDATE entity_links SET invalid_at = ?, invalidated_by = ? "
        "WHERE (source_id = ? OR target_id = ?) AND invalid_at IS NULL",
        (canonical, invalidated_by, entity_id, entity_id),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount


async def connected_entities(
    db: aiosqlite.Connection,
    entity_ids: list[str],
    *,
    max_depth: int = 2,
    as_of: str | None = None,
) -> dict[str, dict]:
    """Entities reachable within *max_depth* undirected valid hops.

    Returns ``{entity_id: {depth, path_confidence, via_link_type}}`` for
    reached entities (seeds excluded), keeping the strongest path per
    entity. Edge validity: ``valid_at <= as_of`` (NULL = always) and
    ``invalid_at`` NULL or ``> as_of``. Path confidence multiplies edge
    ``confidence × provenance_weight`` per hop.
    """
    if not entity_ids:
        return {}
    as_of = canonical_iso(as_of) or datetime.now(UTC).isoformat()
    seeds = set(entity_ids)
    frontier: dict[str, float] = {eid: 1.0 for eid in seeds}
    reached: dict[str, dict] = {}
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        ph = ",".join("?" * len(frontier))
        ids = list(frontier)
        rows = await db.execute_fetchall(
            f"SELECT source_id, target_id, link_type, provenance, confidence "  # noqa: S608
            f"FROM entity_links "
            f"WHERE (source_id IN ({ph}) OR target_id IN ({ph})) "
            f"AND (valid_at IS NULL OR valid_at <= ?) "
            f"AND (invalid_at IS NULL OR invalid_at > ?)",
            ids + ids + [as_of, as_of],
        )
        next_frontier: dict[str, float] = {}
        for source_id, target_id, link_type, provenance, confidence in rows:
            for here, there in ((source_id, target_id), (target_id, source_id)):
                if here not in frontier or there in seeds:
                    continue
                path_conf = (
                    frontier[here]
                    * confidence
                    * PROVENANCE_WEIGHTS.get(provenance, 0.5)
                )
                prior = reached.get(there)
                if prior is None or path_conf > prior["path_confidence"]:
                    reached[there] = {
                        "depth": depth,
                        "path_confidence": path_conf,
                        "via_link_type": link_type,
                    }
                    next_frontier[there] = max(
                        next_frontier.get(there, 0.0), path_conf
                    )
        frontier = next_frontier
    return reached


async def memories_mentioning(
    db: aiosqlite.Connection,
    entity_ids: list[str],
    *,
    limit_per_entity: int = 20,
) -> list[dict]:
    """Mention rows for *entity_ids*, strongest first per entity."""
    if not entity_ids:
        return []
    out: list[dict] = []
    for entity_id in entity_ids:
        rows = await db.execute_fetchall(
            "SELECT memory_id, entity_id, provenance, confidence, source "
            "FROM entity_mentions WHERE entity_id = ? "
            "ORDER BY confidence DESC LIMIT ?",
            (entity_id, limit_per_entity),
        )
        out.extend(
            {
                "memory_id": r[0],
                "entity_id": r[1],
                "provenance": r[2],
                "confidence": r[3],
                "source": r[4],
            }
            for r in rows
        )
    return out


async def merge_entity(
    db: aiosqlite.Connection,
    *,
    loser_id: str,
    survivor_id: str,
    _commit: bool = True,
) -> None:
    """Adjudicated merge: rewrite loser's mentions/links to the survivor.

    Keep-stronger discipline mirrors ``upsert_mention``/``upsert_link``:
    when the survivor already holds the same mention/relation, the
    higher-confidence row wins — a merge must never discard the
    strongest evidence (the loser is often the better-attested record).
    """
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO entity_mentions "
        "(memory_id, entity_id, provenance, confidence, source, created_at) "
        "SELECT memory_id, ?, provenance, confidence, source, created_at "
        # The SELECT's own WHERE disambiguates the upsert-from-SELECT
        # parse (SQLite needs one before ON CONFLICT).
        "FROM entity_mentions WHERE entity_id = ? "
        "ON CONFLICT(memory_id, entity_id) DO UPDATE SET "
        "provenance = excluded.provenance, confidence = excluded.confidence, "
        "source = excluded.source "
        "WHERE excluded.confidence > entity_mentions.confidence",
        (survivor_id, loser_id),
    )
    await db.execute(
        "DELETE FROM entity_mentions WHERE entity_id = ?", (loser_id,),
    )
    for src_col, dst_col in (("source_id", "target_id"), ("target_id", "source_id")):
        # dst != survivor guard: a pre-existing loser↔survivor link
        # (e.g. an LLM-emitted supersedes) must not become a self-loop.
        await db.execute(
            f"INSERT INTO entity_links "  # noqa: S608
            f"({src_col}, {dst_col}, link_type, provenance, confidence, "
            f"evidence_memory_id, valid_at, invalid_at, invalidated_by, created_at) "
            f"SELECT ?, {dst_col}, link_type, provenance, confidence, "
            f"evidence_memory_id, valid_at, invalid_at, invalidated_by, created_at "
            f"FROM entity_links WHERE {src_col} = ? AND {dst_col} != ? "
            f"ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET "
            f"provenance = excluded.provenance, "
            f"confidence = excluded.confidence, "
            f"evidence_memory_id = excluded.evidence_memory_id, "
            f"valid_at = COALESCE(excluded.valid_at, entity_links.valid_at), "
            # Invalidation state travels with the winning row: a stronger
            # loser link that was already closed must not resurrect as
            # active on the survivor (as-of traversal would follow it).
            f"invalid_at = excluded.invalid_at, "
            f"invalidated_by = excluded.invalidated_by "
            f"WHERE excluded.confidence > entity_links.confidence",
            (survivor_id, loser_id, survivor_id),
        )
        await db.execute(
            f"DELETE FROM entity_links WHERE {src_col} = ?",  # noqa: S608
            (loser_id,),
        )
    await db.execute(
        "UPDATE entities SET status = 'merged', merged_into = ?, updated_at = ? "
        "WHERE entity_id = ?",
        (survivor_id, now, loser_id),
    )
    if _commit:
        await db.commit()


async def delete_entities_cascade(
    db: aiosqlite.Connection,
    entity_ids: list[str],
    *,
    _commit: bool = True,
) -> dict[str, int]:
    """Delete entities plus their mentions and links (cleanup/repair path).

    Batch counterpart to the ledger's write paths for data-repair scripts
    (e.g. purging fake ``commit`` entities minted by the pre-fix SHA
    regex). Returns per-table deleted-row counts. Caller batches under
    ``_commit=False`` when composing with other writes.
    """
    if not entity_ids:
        return {"entities": 0, "mentions": 0, "links": 0}
    ph = ",".join("?" * len(entity_ids))
    cur = await db.execute(
        f"DELETE FROM entity_mentions WHERE entity_id IN ({ph})",  # noqa: S608
        entity_ids,
    )
    mentions = cur.rowcount
    cur = await db.execute(
        f"DELETE FROM entity_links "  # noqa: S608
        f"WHERE source_id IN ({ph}) OR target_id IN ({ph})",
        entity_ids + entity_ids,
    )
    links = cur.rowcount
    cur = await db.execute(
        f"DELETE FROM entities WHERE entity_id IN ({ph})",  # noqa: S608
        entity_ids,
    )
    deleted = cur.rowcount
    if _commit:
        await db.commit()
    return {"entities": deleted, "mentions": mentions, "links": links}


async def enqueue_adjudication(
    db: aiosqlite.Connection,
    *,
    entity_id: str,
    similar_entity_id: str,
    _commit: bool = True,
) -> None:
    """Queue a fuzzy-match pair for LLM adjudication (entity_maintenance job).

    Inline INSERT rather than ``deferred_work.create`` — that helper
    commits unconditionally, which would break callers batching under
    ``_commit=False`` (extraction transaction discipline).
    """
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO deferred_work_queue
           (id, work_type, priority, payload_json, deferred_at,
            deferred_reason, created_at)
           VALUES (?, 'entity_adjudication', 60, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            json.dumps(
                {"entity_id": entity_id, "similar_entity_id": similar_entity_id}
            ),
            now,
            "fuzzy norm_name match at entity creation",
            now,
        ),
    )
    if _commit:
        await db.commit()


def _row_to_dict(db: aiosqlite.Connection, row) -> dict:
    if isinstance(row, aiosqlite.Row) or hasattr(row, "keys"):
        return dict(row)
    return {
        "entity_id": row[0],
        "name": row[1],
        "norm_name": row[2],
        "entity_type": row[3],
        "summary": row[4],
        "source": row[5],
        "status": row[6],
        "merged_into": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }
