"""CRUD operations for episodic/semantic memory metadata in SQLite.

Note: Vector embeddings live in Qdrant. This module handles the FTS5 text index
that supports hybrid search (Qdrant vectors + FTS5 + RRF fusion).
"""

from __future__ import annotations

import re

import aiosqlite

from genesis.db.timeutil import canonical_iso


def _prepare_fts5(query: str, *, boolean: bool = False) -> str | None:
    """Prepare a query string for FTS5 MATCH.

    Args:
        query: Raw query text.
        boolean: If True, preserve FTS5 boolean operators (OR, AND) and
            parentheses. Use ONLY for queries constructed by expand_query
            (controlled vocabulary). Never for raw user input.

    Default path (boolean=False): lowercases the query to neutralize
    accidental FTS5 boolean operators — uppercase OR/AND are interpreted
    as operators by FTS5, but lowercase or/and are plain search terms.
    FTS5 content matching is case-insensitive, so lowercasing doesn't
    affect result quality.

    Returns None if the query is empty after escaping (caller should return []).
    """
    if boolean:
        # Preserve OR/AND keywords and parentheses for structured queries.
        # Strip everything else that could cause FTS5 syntax errors.
        cleaned = re.sub(r"[^\w\s()]", " ", query, flags=re.UNICODE).strip()
        # Safety: strip unbalanced parentheses rather than crash FTS5
        if cleaned.count("(") != cleaned.count(")"):
            cleaned = cleaned.replace("(", " ").replace(")", " ").strip()
    else:
        # Lowercase neutralizes accidental boolean operators (OR/AND).
        # Strip all non-alphanumeric to prevent FTS5 syntax errors.
        cleaned = re.sub(r"[^\w\s]", " ", query.lower(), flags=re.UNICODE).strip()
    return cleaned or None


async def create(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    content: str,
    source_type: str = "memory",
    tags: str = "",
    collection: str = "episodic_memory",
) -> str:
    """Insert a memory entry into the FTS5 index. Returns memory_id."""
    await db.execute(
        "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, content, source_type, tags, collection),
    )
    await db.commit()
    return memory_id


async def upsert(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    content: str,
    source_type: str = "memory",
    tags: str = "",
    collection: str = "episodic_memory",
) -> str:
    """Idempotent write: delete-then-insert for FTS5 (no ON CONFLICT support)."""
    await db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
    await db.execute(
        "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, content, source_type, tags, collection),
    )
    await db.commit()
    return memory_id


async def find_exact_duplicate(
    db: aiosqlite.Connection,
    *,
    content: str,
) -> str | None:
    """Return memory_id if exact content already exists (any collection).

    FTS5 does not support equality (=) on content columns, so we use a
    length + substr pre-filter followed by Python exact match.

    Collection-agnostic: the FTS ``collection`` column is unreliable
    (uniformly ``episodic_memory`` regardless of actual Qdrant placement).
    """
    if not content:
        return None

    # Pre-filter: match on length and first 200 chars to narrow candidates
    prefix = content[:200]
    rows = await db.execute_fetchall(
        "SELECT memory_id, content FROM memory_fts "
        "WHERE length(content) = ? "
        "AND substr(content, 1, 200) = ? "
        "LIMIT 200",
        (len(content), prefix),
    )
    for row in rows:
        if row[1] == content:
            return row[0]

    return None


async def search(
    db: aiosqlite.Connection,
    *,
    query: str,
    source_type: str | None = None,
    collection: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search on memory content. Returns matching rows."""
    escaped = _prepare_fts5(query)
    if not escaped:
        return []
    sql = "SELECT memory_id, content, source_type, collection FROM memory_fts WHERE memory_fts MATCH ?"
    params: list = [escaped]
    if source_type:
        sql += " AND source_type = ?"
        params.append(source_type)
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    sql += " LIMIT ?"
    params.append(limit)
    rows = await db.execute_fetchall(sql, params)
    return [
        {"memory_id": r[0], "content": r[1], "source_type": r[2], "collection": r[3]} for r in rows
    ]


async def search_ranked(
    db: aiosqlite.Connection,
    *,
    query: str,
    collection: str | None = None,
    limit: int = 30,
    boolean: bool = False,
    exclude_subsystems: list[str] | None = None,
    include_only_subsystems: list[str] | None = None,
    as_of: str | None = None,
    include_deprecated: bool = False,
) -> list[dict]:
    """FTS5 search returning rank scores for RRF fusion.

    ``exclude_subsystems`` / ``include_only_subsystems`` filter on
    ``memory_metadata.source_subsystem``. Excludes preserve NULL
    (user-sourced) rows; includes drop them.

    The bitemporal ``invalid_at`` filter is ALWAYS applied — rows past
    their expiry never surface in recall. ``as_of`` defaults to
    ``datetime.now(UTC).isoformat()``. NULL ``invalid_at`` (= valid
    forever) always passes.
    """
    escaped = _prepare_fts5(query, boolean=boolean)
    if not escaped:
        return []

    if as_of is None:
        from datetime import UTC
        from datetime import datetime as _dt

        as_of = _dt.now(UTC).isoformat()

    # The JOIN with memory_metadata is now always required for invalid_at
    # filtering. Keeping the column-qualified SELECT format consistent.
    sql = (
        "SELECT memory_fts.memory_id, memory_fts.content, "
        "memory_fts.source_type, memory_fts.collection, memory_fts.rank, "
        "memory_metadata.origin_class, "
        "memory_metadata.wing, memory_metadata.room, "
        "memory_fts.tags "
        "FROM memory_fts LEFT JOIN memory_metadata "
        "ON memory_fts.memory_id = memory_metadata.memory_id "
        "WHERE memory_fts MATCH ?"
    )
    params: list = [escaped]
    if collection:
        sql += " AND memory_fts.collection = ?"
        params.append(collection)
    # Always-on bitemporal filter: NULL invalid_at = valid forever; otherwise
    # the fact must still be valid at as_of.
    sql += " AND (memory_metadata.invalid_at IS NULL OR memory_metadata.invalid_at > ?)"
    params.append(as_of)
    # Dream cycle deprecation filter: exclude consolidated memories by default.
    # NULL deprecated (legacy rows pre-migration) = not deprecated.
    # Pass include_deprecated=True for audit/history queries.
    if not include_deprecated:
        sql += " AND (memory_metadata.deprecated IS NULL OR memory_metadata.deprecated = 0)"
    if exclude_subsystems:
        placeholders = ",".join("?" * len(exclude_subsystems))
        sql += (
            f" AND (memory_metadata.source_subsystem IS NULL "
            f"OR memory_metadata.source_subsystem NOT IN ({placeholders}))"
        )
        params.extend(exclude_subsystems)
    elif include_only_subsystems:
        placeholders = ",".join("?" * len(include_only_subsystems))
        sql += f" AND memory_metadata.source_subsystem IN ({placeholders})"
        params.extend(include_only_subsystems)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    rows = await db.execute_fetchall(sql, params)
    return [
        {
            "memory_id": r[0],
            "content": r[1],
            "source_type": r[2],
            "collection": r[3],
            "rank": r[4],
            # WS-3 stored provenance — from the (already-joined)
            # memory_metadata row; NULL for pre-0054 rows.
            "origin_class": r[5],
            # Authoritative structural taxonomy from the already-joined
            # memory_metadata row — lets the FTS path scope-filter FTS-only
            # candidates by wing/room without depending on the denormalized
            # FTS tag token (which can drift from metadata). NULL for rows
            # never classified.
            "wing": r[6],
            "room": r[7],
            # FTS tag string (space-separated). Carries the explicit
            # ``life_domain:``/``project_type:`` tokens set at store time, so
            # the scope filter can honor an explicit life_domain override
            # rather than inferring solely from wing.
            "tags": r[8],
        }
        for r in rows
    ]


async def origin_class_by_ids(db: aiosqlite.Connection, ids: list[str]) -> dict[str, str | None]:
    """memory_id → stored ``origin_class`` for the given ids (missing ids omitted).

    Used by surfaces that read Qdrant payloads directly (memory_core_facts,
    memory_expand, the retriever backfill) to recover the backfilled SQLite
    value when a point's payload predates the origin_class payload backfill —
    a stale payload must not bypass the WS-3 injection gate.

    Chunked like ``batch_created_at``: memory_expand accepts arbitrary-length
    id lists and core_facts scrolls ``limit*3`` points, so a single IN-clause
    could breach SQLite's 999 bind-variable cap and raise ``too many SQL
    variables`` — whereupon the callers fail open to ``origin_class=None`` and
    a stale external row would slip the gate. Chunking keeps the recovery
    reliable at any scale.
    """
    if not ids:
        return {}
    _CHUNK = 900  # single-column query, stays under SQLite's 999 limit
    out: dict[str, str | None] = {}
    for offset in range(0, len(ids), _CHUNK):
        chunk = ids[offset : offset + _CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = await db.execute_fetchall(
            f"SELECT memory_id, origin_class FROM memory_metadata WHERE memory_id IN ({marks})",  # noqa: S608 -- placeholders bound
            chunk,
        )
        for r in rows:
            out[r[0]] = r[1]
    return out


async def hydrate_for_expansion(db: aiosqlite.Connection, ids: list[str]) -> dict[str, dict]:
    """Batch memory_id → {content, source_type, tags, collection, origin_class,
    invalid_at, deprecated} for graph-expansion neighbor hydration.

    ONE FTS⋈metadata query replaces the recall-time expansion's former
    per-neighbor ``get_by_id`` loop plus its separate ``origin_class_by_ids``
    and visibility-filter passes (the N+1 that dominated expansion latency).
    Neighbor lists are cap-bounded (≤25 by the settings validator), so a
    single IN-clause never approaches the 999 bind-variable ceiling — no
    chunking needed. Missing ids are omitted (dangling links). ``collection``
    is read from the FTS row (authoritative at retrieval, matching
    ``get_by_id``); the LEFT JOIN yields NULL metadata fields for a row with
    no ``memory_metadata`` companion, which the caller treats as
    visible/unclassified — identical to the prior separate-query behavior.
    """
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = await db.execute_fetchall(
        f"SELECT f.memory_id, f.content, f.source_type, f.tags, f.collection, "  # noqa: S608 - placeholders bound
        f"       m.origin_class, m.invalid_at, "
        f"       COALESCE(m.deprecated, 0) AS deprecated "
        f"FROM memory_fts f "
        f"LEFT JOIN memory_metadata m ON f.memory_id = m.memory_id "
        f"WHERE f.memory_id IN ({marks})",
        ids,
    )
    return {
        r[0]: {
            "content": r[1],
            "source_type": r[2],
            "tags": r[3],
            "collection": r[4],
            "origin_class": r[5],
            "invalid_at": r[6],
            "deprecated": r[7],
        }
        for r in rows
    }


async def delete(db: aiosqlite.Connection, *, memory_id: str) -> bool:
    """Delete a memory entry from the FTS5 index."""
    cursor = await db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── memory_metadata companion table ─────────────────────────────────


async def create_metadata(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    created_at: str,
    collection: str = "episodic_memory",
    confidence: float | None = None,
    embedding_status: str = "embedded",
    memory_class: str = "fact",
    wing: str | None = None,
    room: str | None = None,
    valid_at: str | None = None,
    invalid_at: str | None = None,
    source_subsystem: str | None = None,
    origin_class: str | None = None,
) -> str:
    """Insert a row into memory_metadata. Returns memory_id.

    ``valid_at`` records when the fact became true in the real world
    (bi-temporal modeling). Defaults to ``created_at`` if not provided.
    ``invalid_at`` records when the fact stopped being true (NULL = still valid).
    ``source_subsystem`` tags writes from automated subsystems (ego,
    triage, reflection) so foreground recall can default-filter them.
    NULL = user-sourced.
    ``origin_class`` is the WS-3 provenance taxonomy
    (owner/first_party/external_untrusted), derived in
    ``MemoryStore.store()``; NULL = legacy/unclassified (gates treat it
    fail-closed at gate time).
    """
    # Bitemporal columns are raw TEXT-compared everywhere — canonicalize
    # at the write gate. Unparseable valid_at (LLM temporal strings like
    # "Friday" or date ranges) falls back to created_at; unparseable
    # invalid_at is dropped (NULL = valid forever) rather than stored as
    # a string that breaks the always-on filter.
    resolved_valid_at = canonical_iso(valid_at) or canonical_iso(created_at) or created_at
    await db.execute(
        "INSERT OR IGNORE INTO memory_metadata "
        "(memory_id, created_at, collection, confidence, embedding_status, "
        "memory_class, wing, room, valid_at, invalid_at, source_subsystem, "
        "origin_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            created_at,
            collection,
            confidence,
            embedding_status,
            memory_class,
            wing,
            room,
            resolved_valid_at,
            canonical_iso(invalid_at),
            source_subsystem,
            origin_class,
        ),
    )
    await db.commit()
    return memory_id


async def invalidate_memory(
    db: aiosqlite.Connection,
    memory_id: str,
    invalid_at: str,
) -> bool:
    """Mark a memory as no longer valid (bi-temporal invalidation).

    Returns True if the memory was found and updated. Raises
    ``ValueError`` on an unparseable timestamp — an explicit
    invalidation with a garbage cutoff is a programming error, and a
    non-canonical string would silently break the always-on TEXT
    comparison in ``search_ranked``.
    """
    canonical = canonical_iso(invalid_at)
    if canonical is None:
        raise ValueError(f"invalidate_memory: unparseable invalid_at {invalid_at!r}")
    cursor = await db.execute(
        "UPDATE memory_metadata SET invalid_at = ? WHERE memory_id = ?",
        (canonical, memory_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_superseded(
    db: aiosqlite.Connection,
    old_id: str,
    new_id: str,
    timestamp: str,
) -> bool:
    """Mark a memory as superseded by a newer memory.

    Sets ``deprecated=1``, ``superseded_by``, and ``superseded_at``.
    Returns True if the memory was found and updated.
    """
    cursor = await db.execute(
        "UPDATE memory_metadata SET deprecated = 1, "
        "superseded_by = ?, superseded_at = ? "
        "WHERE memory_id = ?",
        (new_id, timestamp, old_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_metadata(
    db: aiosqlite.Connection,
    memory_id: str,
) -> dict | None:
    """Return metadata row for a memory_id, or None if not found."""
    rows = await db.execute_fetchall(
        "SELECT memory_id, collection, embedding_status, deprecated, "
        "superseded_by, superseded_at FROM memory_metadata "
        "WHERE memory_id = ?",
        (memory_id,),
    )
    row = rows[0] if rows else None
    if not row:
        return None
    return {
        "memory_id": row[0],
        "collection": row[1],
        "embedding_status": row[2],
        "deprecated": row[3],
        "superseded_by": row[4],
        "superseded_at": row[5],
    }


async def count_fts_metadata_drift(db: aiosqlite.Connection) -> tuple[int, int]:
    """Return ``(fts_ghosts, fts_invisible)`` cross-store drift counts.

    ``fts_ghosts``: ``memory_fts`` rows with no ``memory_metadata`` row (a
    keyword hit whose provenance/status is unknown). ``fts_invisible``:
    ``memory_metadata`` rows with no ``memory_fts`` row (a memory that can
    never surface in keyword/hybrid search). Both are 0 when ``store()`` has
    written both stores for every memory; a non-zero count flags a store-path
    or migration regression worth surfacing.
    """
    # Set-difference via COUNT + INTERSECT (O(n log n)). A correlated
    # NOT EXISTS in the memory_metadata->memory_fts direction would be O(n^2):
    # ``memory_fts.memory_id`` is UNINDEXED, so each per-row lookup scans the
    # whole FTS content table — pathologically slow at 20k+ rows and run under
    # the shared-connection lock. Comparing distinct-id set sizes avoids it.
    fts_rows = await db.execute_fetchall("SELECT COUNT(DISTINCT memory_id) FROM memory_fts")
    meta_rows = await db.execute_fetchall("SELECT COUNT(DISTINCT memory_id) FROM memory_metadata")
    both_rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM ("
        "SELECT memory_id FROM memory_fts "
        "INTERSECT SELECT memory_id FROM memory_metadata)"
    )
    fts = int(fts_rows[0][0]) if fts_rows else 0
    meta = int(meta_rows[0][0]) if meta_rows else 0
    both = int(both_rows[0][0]) if both_rows else 0
    ghosts = fts - both  # memory_fts rows with no memory_metadata row
    invisible = meta - both  # memory_metadata rows with no memory_fts row
    return ghosts, invisible


async def embedding_status_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Return a ``{embedding_status: count}`` map over ``memory_metadata``.

    A single grouped aggregate — the one source of truth shared by both the
    dashboard memory-health snapshot and the awareness embedding-backlog probe.
    Statuses with zero rows are simply absent from the map (callers use
    ``.get(status, 0)``). ``embedding_status`` is ``NOT NULL`` in the schema, so
    no NULL/None key can occur.

    Status meanings: ``embedded`` (has a vector — the healthy default);
    ``pending`` (embed failed and is queued — self-heals via the recovery
    worker); ``fts5_only`` (a deliberate, permanent keyword-only write, NOT a
    failure); ``failed`` (recovery gave up — permanently non-vector-searchable).
    """
    rows = await db.execute_fetchall(
        "SELECT embedding_status, COUNT(*) FROM memory_metadata GROUP BY embedding_status"
    )
    return {str(status): int(cnt) for status, cnt in rows}


async def set_embedding_status(db: aiosqlite.Connection, memory_id: str, status: str) -> bool:
    """Set ``memory_metadata.embedding_status`` for a memory. Returns True if a row changed."""
    cursor = await db.execute(
        "UPDATE memory_metadata SET embedding_status = ? WHERE memory_id = ?",
        (status, memory_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_taxonomy(
    db: aiosqlite.Connection,
    memory_id: str,
) -> dict[str, str | None] | None:
    """Return ``{"wing", "room", "origin_class"}`` for a memory_id, or None.

    The embedding-recovery worker uses this to restore metadata fields onto
    the reconstructed Qdrant payload (see ``resilience/embedding_recovery``)
    so a recovered point is not silently dropped from ``wing=``/``room=``
    filtered recall — nor (WS-3) from ``origin_class=`` filtered gates.
    ``origin_class`` is read from ``memory_metadata`` (always written at
    store time, even on the FTS5-only/pending path) rather than the pending
    row, keeping ONE source of truth. ``life_domain`` is recovered from the
    ``life_domain:`` tag and ``project_type`` is not persisted on this path.
    """
    rows = await db.execute_fetchall(
        "SELECT wing, room, origin_class FROM memory_metadata WHERE memory_id = ?",
        (memory_id,),
    )
    row = rows[0] if rows else None
    if not row:
        return None
    return {"wing": row[0], "room": row[1], "origin_class": row[2]}


async def batch_created_at(
    db: aiosqlite.Connection,
    memory_ids: list[str],
) -> dict[str, str]:
    """Batch-fetch ``created_at`` from memory_metadata for *memory_ids*.

    Mirrors ``memory_links.batch_link_counts``: one chunked IN-clause
    query instead of N lookups. ``HybridRetriever._compute_activations``
    uses it to give FTS-only rows (no Qdrant hit) their real creation
    time instead of the ``now_str`` fallback — that fallback yields an
    unearned ``recency = exp(0) = 1.0`` and a phantom age of 0 in the
    MEM-005 entrenchment metric. Ids with no metadata row are omitted;
    the caller falls back to ``now_str`` for those.
    """
    if not memory_ids:
        return {}

    _CHUNK = 900  # single-column query, stays under SQLite's 999 limit
    out: dict[str, str] = {}
    for offset in range(0, len(memory_ids), _CHUNK):
        chunk = memory_ids[offset : offset + _CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = await db.execute_fetchall(
            f"SELECT memory_id, created_at FROM memory_metadata WHERE memory_id IN ({ph})",
            chunk,
        )
        for row in rows:
            if row[1]:
                out[row[0]] = row[1]
    return out


async def match_id_prefix(
    db: aiosqlite.Connection,
    prefix: str,
    *,
    limit: int = 2,
) -> list[str]:
    """Memory IDs starting with *prefix* (e.g. an 8-char ``id:`` handle).

    ``limit=2`` lets callers distinguish unique from ambiguous without
    counting every match. Parameterized LIKE; callers are expected to
    pre-validate the prefix shape (hex/dash).
    """
    rows = await db.execute_fetchall(
        "SELECT memory_id FROM memory_metadata WHERE memory_id LIKE ? || '%' LIMIT ?",
        (prefix, limit),
    )
    return [str(r[0]) for r in rows]


async def delete_metadata(db: aiosqlite.Connection, *, memory_id: str) -> bool:
    """Delete a memory_metadata row. Returns True if deleted."""
    cursor = await db.execute("DELETE FROM memory_metadata WHERE memory_id = ?", (memory_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_by_id(db: aiosqlite.Connection, memory_id: str) -> dict | None:
    """Get a single memory by ID, joining FTS5 content with metadata."""
    rows = await db.execute_fetchall(
        "SELECT f.memory_id, f.content, f.source_type, f.tags, f.collection, "
        "       m.created_at, m.confidence, m.embedding_status, "
        "       m.valid_at, m.invalid_at "
        "FROM memory_fts f "
        "LEFT JOIN memory_metadata m ON f.memory_id = m.memory_id "
        "WHERE f.memory_id = ?",
        (memory_id,),
    )
    row = rows[0] if rows else None
    if not row:
        return None
    return {
        "memory_id": row[0],
        "content": row[1],
        "source_type": row[2],
        "tags": row[3],
        "collection": row[4],
        "created_at": row[5],
        "confidence": row[6],
        "embedding_status": row[7],
        "valid_at": row[8],
        "invalid_at": row[9],
    }


async def list_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    collection: str | None = None,
) -> list[dict]:
    """List memories ordered by created_at descending (newest first)."""
    sql = (
        "SELECT f.memory_id, f.content, f.source_type, f.collection, "
        "       m.created_at, m.confidence, m.embedding_status, "
        "       m.valid_at, m.invalid_at "
        "FROM memory_metadata m "
        "JOIN memory_fts f ON f.memory_id = m.memory_id "
    )
    params: list = []
    if collection:
        sql += "WHERE m.collection = ? "
        params.append(collection)
    sql += "ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = await db.execute_fetchall(sql, params)
    return [
        {
            "memory_id": r[0],
            "content": r[1][:500],  # truncated for list views
            "source_type": r[2],
            "collection": r[3],
            "created_at": r[4],
            "confidence": r[5],
            "embedding_status": r[6],
            "valid_at": r[7],
            "invalid_at": r[8],
        }
        for r in rows
    ]


async def count(
    db: aiosqlite.Connection,
    *,
    collection: str | None = None,
) -> int:
    """Count memories in memory_metadata (optionally by collection)."""
    if collection:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM memory_metadata WHERE collection = ?",
            (collection,),
        )
    else:
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM memory_metadata")
    row = rows[0] if rows else None
    return row[0] if row else 0


# Pool-size keys stamped onto the weekly J-9 memory snapshot. Exported so the
# aggregator's failure-fallback and the efficacy report can reference the exact
# key set without drifting from this function's output.
POOL_COUNT_KEYS: tuple[str, ...] = (
    "pool_episodic_total",
    "pool_episodic_retrievable",
    "pool_episodic_embedded",
    "pool_knowledge_units_total",
    "pool_memory_links_total",
)


async def pool_size_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Return point-in-time pool-size counts for retrieval-efficacy trending.

    Snapshotted alongside the J-9 weekly memory-quality metrics so retrieval
    quality (precision@k, hit_rate, MRR) can be read against the size of the
    pool it was measured over — the pool-growth-vs-quality trend the WS2
    retrieval-drift work needs and that nothing measured before.

    ``pool_episodic_retrievable`` applies the SAME two filters the recall path
    applies (``recall`` :195-203): NOT deprecated by the dream cycle AND still
    bitemporally valid (``invalid_at`` NULL or in the future). That is the
    denominator the drift hypothesis actually cares about — a deprecated/expired
    row is not a candidate any recall dilutes. ``pool_episodic_total`` keeps the
    raw count for reference (total minus retrievable = accumulated dead weight).

    SQLite-side counts only (the weekly aggregator holds no Qdrant client), so
    these may differ slightly from Qdrant ``points_count`` on rows still pending
    embed. ``memory_metadata`` mixes the ``episodic_memory`` and
    ``knowledge_base`` collections, so episodic is counted by collection, not as
    the whole table. Keys match ``POOL_COUNT_KEYS``.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    ep_total = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_metadata WHERE collection = 'episodic_memory'"
    )
    ep_retrievable = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_metadata "
        "WHERE collection = 'episodic_memory' "
        "AND (deprecated IS NULL OR deprecated = 0) "
        "AND (invalid_at IS NULL OR invalid_at > ?)",
        (now,),
    )
    ep_embedded = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_metadata "
        "WHERE collection = 'episodic_memory' AND embedding_status = 'embedded'"
    )
    ku = await db.execute_fetchall("SELECT COUNT(*) FROM knowledge_units")
    links = await db.execute_fetchall("SELECT COUNT(*) FROM memory_links")
    return {
        "pool_episodic_total": int(ep_total[0][0]) if ep_total else 0,
        "pool_episodic_retrievable": int(ep_retrievable[0][0]) if ep_retrievable else 0,
        "pool_episodic_embedded": int(ep_embedded[0][0]) if ep_embedded else 0,
        "pool_knowledge_units_total": int(ku[0][0]) if ku else 0,
        "pool_memory_links_total": int(links[0][0]) if links else 0,
    }


async def episodic_count_created_before(db: aiosqlite.Connection, ts: str) -> int:
    """Count episodic rows created on or before ``ts`` (ISO-8601).

    Used ONLY by the retrieval-efficacy report to reconstruct a pool size for
    pre-WS2-0 weeks that carry no ``pool_*`` snapshot. Deliberately a total-
    created count, not retrievable-as-of: ``deprecated``/``invalid_at`` are
    current-state flags, so "retrievable in the past" cannot be reconstructed —
    a row deprecated today was retrievable then. The report labels this an
    approximation (biased high on older weeks) and never writes it back.
    """
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_metadata "
        "WHERE collection = 'episodic_memory' AND created_at <= ?",
        (ts,),
    )
    return int(rows[0][0]) if rows else 0
