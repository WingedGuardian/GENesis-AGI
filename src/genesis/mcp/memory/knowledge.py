"""Knowledge base tools: recall, ingest, status."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


@mcp.tool()
async def knowledge_recall(
    query: str,
    project: str | None = None,
    domain: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Hybrid search scoped by project/domain, authority-tagged."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None
    assert memory_mod._db is not None

    vector_results = await memory_mod._retriever.recall(query, source="knowledge", limit=limit)

    fts_results: list[dict] = []
    try:
        fts_results = await memory_mod.knowledge.search_fts(
            memory_mod._db, query, project=project, domain=domain, limit=limit,
        )
    except Exception:
        logger.warning("knowledge_fts search failed", exc_info=True)

    seen_ids: set[str] = set()
    merged: list[dict] = []

    for r in vector_results:
        seen_ids.add(r.memory_id)
        merged.append({
            "unit_id": r.memory_id,
            "content": r.content,
            "source": r.source,
            "score": r.score,
            "origin": "vector",
        })

    for fts_row in fts_results:
        uid = fts_row["unit_id"]
        if uid not in seen_ids:
            seen_ids.add(uid)
            merged.append({
                "unit_id": uid,
                "content": fts_row.get("body", ""),
                "concept": fts_row.get("concept", ""),
                "domain": fts_row.get("domain", ""),
                "project_type": fts_row.get("project_type", ""),
                "score": 0.0,
                "origin": "fts",
            })

    return merged[:limit]


async def _ingest_knowledge_unit(
    *,
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    memory_class: str | None = None,
    concept: str | None = None,
    tags_json: str | None = None,
) -> str:
    """MCP-side wrapper around :func:`ingest_knowledge_unit`.

    Resolves the MCP server's live ``_store`` and ``_db`` globals and
    delegates to the pure-Python helper in ``genesis.memory.knowledge_ingest``.
    Keeps the MCP dispatch surface thin so the same ingestion pipeline can
    run from non-MCP contexts (e.g. the extraction_job reference extractor).
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    return await ingest_knowledge_unit(
        store=memory_mod._store,
        db=memory_mod._db,
        content=content,
        project=project,
        domain=domain,
        authority=authority,
        provenance=provenance,
        memory_class=memory_class,
        concept=concept,
        tags_json=tags_json,
    )


@mcp.tool()
async def knowledge_ingest(
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    memory_class: str | None = None,
    concept: str | None = None,
) -> str:
    """Store distilled knowledge unit with provenance. Returns unit ID.

    Idempotent on ``(project, domain, concept)``: re-ingesting the same logical
    entry updates the existing row in place and replaces the Qdrant point,
    preserving the original unit ID.

    ``memory_class`` (optional): override auto-classification of the underlying
    episodic/knowledge memory. Accepts ``"fact"`` (1.0x activation weight, the
    default), ``"rule"`` (1.3x), or ``"reference"`` (0.7x). Callers storing
    persistent reference data that must be findable in proactive retrieval
    should pass ``"fact"`` explicitly — the auto-classifier treats URL-bearing
    content as ``"reference"`` and applies a 0.7x penalty, which is the wrong
    semantic for a lookup store.

    ``concept`` (optional): override the derived ``concept`` field. Defaults to
    ``content[:200]``. The reference store passes a structured identifier here
    (e.g. ``"ScarletAndRage forum login"``) so that the unique key
    ``(project_type, domain, concept)`` behaves like a dedup key on logical
    identity rather than raw content prefix.
    """
    return await _ingest_knowledge_unit(
        content=content,
        project=project,
        domain=domain,
        authority=authority,
        provenance=provenance,
        memory_class=memory_class,
        concept=concept,
    )


@mcp.tool()
async def knowledge_status(
    project: str | None = None,
) -> dict:
    """Collection stats, staleness report, project index."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    db_stats = await memory_mod.knowledge.stats(memory_mod._db, project=project)

    qdrant_info: dict | None = None
    try:
        qdrant_info = memory_mod.get_collection_info(memory_mod._qdrant, "knowledge_base")
    except Exception:
        logger.warning("Failed to query knowledge_base collection", exc_info=True)

    return {
        "total_units": db_stats["total"],
        "oldest_ingested": db_stats["oldest_ingested"],
        "newest_ingested": db_stats["newest_ingested"],
        "by_domain": db_stats["by_domain"],
        "qdrant_vectors": qdrant_info.get("points_count", 0) if qdrant_info else None,
    }


# ─── Reference Store ─────────────────────────────────────────────────────────
#
# The reference store is a convention layered on top of knowledge_units:
# persistent lookup data (credentials, URLs, IPs, account handles, persona
# pointers, arbitrary "unique facts the user will need again") lives in
# knowledge_units with project_type="reference" and domain="reference.{kind}".
#
# Semantic search and proactive injection already work end-to-end for these
# entries via the existing knowledge_recall / memory_proactive paths — adding
# reference_store is just a thin wrapper that normalizes the body shape and
# forces memory_class="fact" so references avoid the 0.7x auto-classification
# penalty designed for generic "see also" pointers.
#
# ─────────────────────────────────────────────────────────────────────────────

_REFERENCE_KINDS = frozenset({
    "credentials",
    "url",
    "network",
    "persona_pointer",
    "account",
    "fact",
})

_REFERENCE_PROJECT = "reference"


def _format_reference_body(
    *,
    kind: str,
    identifier: str,
    description: str,
    value: str,
    tags: list[str] | None,
    source: dict | None,
) -> str:
    """Format a reference entry body for storage.

    Structured enough to be greppable, plain enough to embed well. The
    leading header line ``[reference.{kind}] {identifier}`` guarantees that
    two entries with different (kind, identifier) tuples never collapse to
    byte-identical content — which would otherwise get deduped by
    ``MemoryStore.store()``'s ``find_exact_duplicate`` pass and leave two
    SQLite rows pointing at the same Qdrant point.

    The description appears next because semantic retrieval weights the
    start of the content most heavily.
    """
    lines = [
        f"[reference.{kind}] {identifier}",
        "",
        description.strip(),
        "",
        f"Value: {value}",
    ]
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if source:
        source_bits = []
        if source.get("session_id"):
            source_bits.append(f"session={source['session_id']}")
        if source.get("captured_via"):
            source_bits.append(f"via={source['captured_via']}")
        if source.get("captured_at"):
            source_bits.append(f"at={source['captured_at']}")
        if source_bits:
            lines.append(f"Captured: {' '.join(source_bits)}")
    return "\n".join(lines)


@mcp.tool()
async def reference_store(
    kind: str,
    identifier: str,
    value: str,
    description: str,
    tags: list[str] | None = None,
    source: dict | None = None,
) -> str:
    """Store a persistent reference entry (credential, URL, IP, persona pointer, etc).

    Persistent reference data that must be findable from future sessions
    without the user having to remind Genesis it exists. Dedup/upsert on
    ``(kind, identifier)`` — re-storing the same logical entry updates in
    place, preserving the stable unit ID.

    ``kind`` must be one of: credentials, url, network, persona_pointer,
    account, fact.

    ``identifier`` is the human-readable name (e.g. "ScarletAndRage forum
    login"). It becomes the unique key ``concept`` field — use the same
    identifier across re-stores to get upsert semantics.

    ``value`` is the raw value (password, URL string, IP address, file path,
    etc.). Stored plaintext in the knowledge_units table.

    ``description`` is REQUIRED. An IP without "what is this" or a credential
    without "what service" is useless 10 sessions later. Describes the
    context, purpose, or relationship — not just the value itself.

    ``tags`` are optional extra tags for retrieval (e.g. ``["forum",
    "persona:614buckeye"]``). The ``reference`` tag and the ``kind`` tag are
    added automatically.

    ``source`` is optional provenance (``session_id``, ``captured_via``,
    ``captured_at``). Surfaced in the body for audit trails.

    Returns the unit_id of the stored row.
    """
    if kind not in _REFERENCE_KINDS:
        raise ValueError(
            f"reference_store: unknown kind '{kind}', must be one of "
            f"{sorted(_REFERENCE_KINDS)}"
        )
    if not description or not description.strip():
        raise ValueError(
            "reference_store: description is required — every reference "
            "entry needs context for future lookups"
        )
    if not identifier or not identifier.strip():
        raise ValueError("reference_store: identifier is required")
    if not value or not value.strip():
        raise ValueError("reference_store: value is required")

    body = _format_reference_body(
        kind=kind,
        identifier=identifier,
        description=description,
        value=value,
        tags=tags,
        source=source,
    )

    # Tags JSON array for the SQLite row. Include reference markers up front
    # so FTS5 keyword filtering works, then the user-supplied tags.
    all_tags = ["reference", kind, *(tags or [])]
    tags_json = json.dumps(all_tags)

    captured_via = (source or {}).get("captured_via", "manual")
    captured_at = (source or {}).get("captured_at")
    session_id = (source or {}).get("session_id")
    provenance: dict = {
        "source_doc": f"reference_store:{captured_via}",
        "source_pipeline": "reference_store",
        "platform": captured_via,
    }
    if captured_at:
        provenance["source_date"] = captured_at
    if session_id:
        provenance["session_id"] = session_id

    unit_id = await _ingest_knowledge_unit(
        content=body,
        project=_REFERENCE_PROJECT,
        domain=f"reference.{kind}",
        authority=captured_via,
        provenance=provenance,
        memory_class="fact",  # bypass 0.7x auto-reference penalty
        concept=identifier,
        tags_json=tags_json,
    )
    return unit_id


async def _log_credential_access(
    unit_ids: list[str],
    accessor_context: str | None,
    query_match_score: float | None = None,
) -> None:
    """Append credential_access_log rows for each unit ID surfaced by a lookup.

    No-op on empty input. Never raises — audit log failures must not break
    lookups. Logs a warning on failure for postmortem triage.
    """
    if not unit_ids:
        return
    memory_mod = _memory_mod()
    assert memory_mod._db is not None
    now_iso = datetime.now(UTC).isoformat()
    try:
        await memory_mod._db.executemany(
            "INSERT INTO credential_access_log "
            "(unit_id, accessor_context, accessed_at, query_match_score) "
            "VALUES (?, ?, ?, ?)",
            [
                (uid, accessor_context, now_iso, query_match_score)
                for uid in unit_ids
            ],
        )
        await memory_mod._db.commit()
    except Exception:
        logger.warning(
            "Failed to append credential_access_log rows for %d units",
            len(unit_ids), exc_info=True,
        )


@mcp.tool()
async def reference_lookup(
    query: str,
    kind: str | None = None,
    limit: int = 5,
    accessor_context: str | None = None,
) -> list[dict]:
    """Hybrid retrieve reference entries matching a query.

    Combines two paths, same pattern as ``knowledge_recall``:
    1. Vector search via ``HybridRetriever.recall(source="knowledge")`` over
       the ``knowledge_base`` Qdrant collection — catches semantic matches
       ("that forum for Ohio State fans" → "ScarletAndRage forum login")
       that keyword search misses.
    2. FTS5 keyword search over ``knowledge_fts`` — catches exact token
       matches and structured identifiers.

    Results are merged and deduped by unit_id, filtered to entries with
    ``project_type='reference'`` and the optional kind.

    ``kind`` (optional): filter results to a single domain, e.g.
    ``"credentials"``, ``"url"``, ``"network"``. If omitted, searches all
    reference entries.

    ``limit`` (default 5): max results returned after dedup + filter.

    ``accessor_context`` (optional): free-text marker of who/what is asking.
    Recorded in ``credential_access_log`` for any credentials-kind entry
    that matches, so we keep an audit trail of sensitive lookups.

    Returns a list of dicts: ``{unit_id, concept, body, domain, tags,
    confidence, source_doc, ingested_at, origin}`` where ``origin`` is
    ``"vector"``, ``"fts"``, or ``"both"``. When a credentials-kind entry
    surfaces, an audit row is written to ``credential_access_log``.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    assert memory_mod._retriever is not None

    if kind is not None and kind not in _REFERENCE_KINDS:
        raise ValueError(
            f"reference_lookup: unknown kind '{kind}', must be one of "
            f"{sorted(_REFERENCE_KINDS)}"
        )
    domain_filter: str | None = f"reference.{kind}" if kind else None

    # 1. Vector path — semantic retrieval via HybridRetriever over knowledge_base.
    # Pull extra candidates so the post-filter to project_type=reference
    # doesn't leave us short of the requested limit.
    vector_limit = max(limit * 3, 10)
    vector_hits: list[dict] = []
    try:
        vector_results = await memory_mod._retriever.recall(
            query, source="knowledge", limit=vector_limit,
        )
        for r in vector_results:
            vector_hits.append({
                "unit_id": r.memory_id,
                "score": getattr(r, "score", 0.0),
                "origin": "vector",
            })
    except Exception:
        logger.warning(
            "reference_lookup: vector path failed, falling back to FTS only",
            exc_info=True,
        )

    # 2. FTS path — keyword retrieval with project/domain filter pushed into SQL.
    fts_results: list[dict] = []
    try:
        fts_results = await memory_mod.knowledge.search_fts(
            memory_mod._db,
            query,
            project=_REFERENCE_PROJECT,
            domain=domain_filter,
            limit=vector_limit,
        )
    except Exception:
        logger.warning(
            "reference_lookup: FTS path failed", exc_info=True,
        )

    # Merge + dedup by unit_id, tracking origin.
    seen: dict[str, str] = {}  # unit_id -> origin
    for v in vector_hits:
        seen[v["unit_id"]] = "vector"
    for f in fts_results:
        uid = f["unit_id"]
        seen[uid] = "both" if uid in seen else "fts"

    # Hydrate each candidate from knowledge_units, filtering post-hoc by
    # project_type / domain (vector hits weren't filtered at retrieval time).
    full_rows: list[dict] = []
    for uid, origin in seen.items():
        row = await memory_mod.knowledge.get(memory_mod._db, uid)
        if row is None:
            continue
        if row.get("project_type") != _REFERENCE_PROJECT:
            continue
        if domain_filter and row.get("domain") != domain_filter:
            continue
        full_rows.append({
            "unit_id": row["id"],
            "concept": row["concept"],
            "body": row["body"],
            "domain": row["domain"],
            "tags": row["tags"],
            "confidence": row["confidence"],
            "source_doc": row["source_doc"],
            "ingested_at": row["ingested_at"],
            "origin": origin,
        })
        if len(full_rows) >= limit:
            break

    # Audit trail: log access for any credentials-kind entry that surfaces,
    # regardless of whether it came from the vector or FTS path.
    credential_hits = [
        row["unit_id"] for row in full_rows
        if row["domain"] == "reference.credentials"
    ]
    if credential_hits:
        await _log_credential_access(
            credential_hits,
            accessor_context=accessor_context or f"reference_lookup:{query[:80]}",
        )

    return full_rows


@mcp.tool()
async def reference_delete(unit_id: str) -> bool:
    """Delete a reference entry by unit_id.

    Removes the row from knowledge_units + knowledge_fts AND the associated
    Qdrant vector point (both collections). Does NOT cascade delete the
    credential_access_log history — audit trails survive the deletion of
    the entry they describe.

    Returns True if a row was deleted, False if no row existed with that id.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    assert memory_mod._db is not None

    # Fetch the row first so we have the qdrant_id for cleanup.
    row = await memory_mod.knowledge.get(memory_mod._db, unit_id)
    if row is None:
        return False

    # Only delete reference entries via this tool — refuse to use it as a
    # generic knowledge_unit delete path, which could break external callers.
    if row.get("project_type") != _REFERENCE_PROJECT:
        raise ValueError(
            f"reference_delete: unit {unit_id} is not a reference entry "
            f"(project_type={row.get('project_type')!r})"
        )

    qdrant_id = row.get("qdrant_id")
    if qdrant_id:
        try:
            await memory_mod._store.delete(qdrant_id)
        except Exception:
            logger.error(
                "reference_delete: Qdrant cleanup failed for unit %s "
                "(qdrant_id=%s)", unit_id, qdrant_id, exc_info=True,
            )

    deleted = await memory_mod.knowledge.delete(memory_mod._db, unit_id)
    logger.info("Reference entry %s deleted: %s", unit_id, deleted)
    return deleted


@mcp.tool()
async def reference_export() -> dict:
    """Export a summary of the reference store, grouped by kind.

    Returns counts per domain plus the total entry count. Used for the
    markdown mirror generator and for manual inspection from a CC session.
    Does NOT return values/bodies — use ``reference_lookup`` or
    ``knowledge_recall`` for that.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None

    stats_result = await memory_mod.knowledge.stats(
        memory_mod._db, project=_REFERENCE_PROJECT,
    )
    return {
        "project_type": _REFERENCE_PROJECT,
        "total": stats_result["total"],
        "by_domain": stats_result["by_domain"],
        "oldest": stats_result["oldest_ingested"],
        "newest": stats_result["newest_ingested"],
    }
