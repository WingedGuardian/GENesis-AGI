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
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.timeutil import canonical_iso

# Provenance weights used by traversal path-confidence (mirrored by the
# session-awareness entity lane; keep in sync deliberately, not by import,
# so the read lane stays dependency-light).
PROVENANCE_WEIGHTS = {"EXTRACTED": 1.0, "INFERRED": 0.8, "AMBIGUOUS": 0.5}

_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_MAX_LINK_TYPE_LEN = 40

# The entity read set, SELECTed by NAME everywhere in this module — never
# `SELECT *`. The tuple-decode fallback in `_row_to_dict` zips against this
# same list, so decode order == SELECT order BY CONSTRUCTION, independent of
# the table's physical column layout (Codex R5-C: a partial upgrade can leave
# the card columns ALTER-appended at the END while a rebuilt table has them
# mid-list — name-driven reads are correct under both).
_ENTITY_COLUMNS = (
    "entity_id",
    "name",
    "norm_name",
    "entity_type",
    "summary",
    "summary_updated_at",
    "summary_dirty",
    "source",
    "status",
    "merged_into",
    "created_at",
    "updated_at",
)
_SELECT_COLS = ", ".join(_ENTITY_COLUMNS)


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
            f"SELECT {_SELECT_COLS} FROM entities "  # noqa: S608 — constant col list
            "WHERE norm_name = ? AND entity_type = ?",
            (norm_name, entity_type),
        )
    else:
        # Deterministic when a norm_name exists under >1 type: prefer an
        # active row (so we return a live entity when one exists), then the
        # oldest — never an arbitrary rows[0]. A merged top row still redirects
        # below; ordering only decides WHICH row we start from.
        rows = await db.execute_fetchall(
            f"SELECT {_SELECT_COLS} FROM entities WHERE norm_name = ? "  # noqa: S608
            "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'merged' THEN 1 "
            "ELSE 2 END, created_at ASC, entity_id ASC",
            (norm_name,),
        )
    if not rows:
        return None
    entity = _row_to_dict(db, rows[0])
    if entity["status"] == "merged" and entity["merged_into"]:
        # Chain-safe: a single hop returned a STILL-MERGED row once chains
        # formed (A→B→C), and mentions then attached to a tombstone. Walk to
        # the active survivor; keep the old fallback-to-the-merged-row when
        # the chain dead-ends (callers treat that row as read-only identity).
        survivor = await resolve_active(db, entity["entity_id"])
        return survivor or entity
    return entity


async def get_by_norm_name_in_types(
    db: aiosqlite.Connection,
    *,
    norm_name: str,
    types: frozenset[str] | set[str],
) -> dict | None:
    """Exact norm_name lookup restricted to a TYPE SET, merge-following.

    The Tier-2 cross-type fold asked the untyped lookup for its single top row
    and rejected when that row was non-cluster — so a person/org sharing the
    norm SHADOWED a legitimate cluster fold, and an avoidable shard was minted
    (adjudication reconciles it later, at LLM cost). Querying the cluster
    explicitly makes the shadow impossible (MW-3 PR-2b, review NOTE N2)."""
    if not types:
        return None
    placeholders = ",".join("?" for _ in types)
    rows = await db.execute_fetchall(
        f"SELECT {_SELECT_COLS} FROM entities "  # noqa: S608 — constant col list
        f"WHERE norm_name = ? AND entity_type IN ({placeholders}) "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'merged' THEN 1 "
        "ELSE 2 END, created_at ASC, entity_id ASC",
        (norm_name, *sorted(types)),
    )
    if not rows:
        return None
    entity = _row_to_dict(db, rows[0])
    if entity["status"] == "merged" and entity["merged_into"]:
        survivor = await resolve_active(db, entity["entity_id"])
        return survivor or entity
    return entity


async def resolve_active(db: aiosqlite.Connection, entity_id: str) -> dict | None:
    """Follow ``merged_into`` redirects to the ACTIVE survivor, or None when the
    chain dead-ends (gone / merged-with-no-target / missing / a cycle).

    Promoted from the adjudicator's private walk (MW-3 PR-2b, which now
    aliases this): once merges apply, chains form (A→B→C), and every
    single-hop follower returns a row that is itself merged — mentions then
    attach to a tombstone. One shared, cycle-safe walk."""
    seen: set[str] = set()
    current = entity_id
    while current and current not in seen:
        seen.add(current)
        ent = await get_entity(db, current)
        if ent is None:
            return None
        if ent["status"] == "active":
            return ent
        if ent["status"] == "merged" and ent["merged_into"]:
            current = ent["merged_into"]
            continue
        return None  # gone, or merged with no target
    return None


async def get_entity(db: aiosqlite.Connection, entity_id: str) -> dict | None:
    rows = await db.execute_fetchall(
        f"SELECT {_SELECT_COLS} FROM entities WHERE entity_id = ?",  # noqa: S608
        (entity_id,),
    )
    return _row_to_dict(db, rows[0]) if rows else None


async def count_entity_mentions(db: aiosqlite.Connection, entity_id: str) -> int:
    """Number of memory↔entity mentions for one entity (attestation strength)."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?", (entity_id,)
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


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
            "SELECT norm_name, entity_id, entity_type FROM entities WHERE status = 'active'",
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
            memory_id,
            entity_id,
            provenance,
            confidence,
            source,
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
            source_id,
            target_id,
            slugify_link_type(link_type),
            provenance,
            confidence,
            evidence_memory_id,
            canonical_iso(valid_at),
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
                path_conf = frontier[here] * confidence * PROVENANCE_WEIGHTS.get(provenance, 0.5)
                prior = reached.get(there)
                if prior is None or path_conf > prior["path_confidence"]:
                    reached[there] = {
                        "depth": depth,
                        "path_confidence": path_conf,
                        "via_link_type": link_type,
                    }
                    next_frontier[there] = max(next_frontier.get(there, 0.0), path_conf)
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


async def _journal_merge(
    db: aiosqlite.Connection, *, loser_id: str, survivor_id: str, now: str
) -> None:
    """Write the pre-delete reversibility snapshot of the loser to entity_merge_journal.

    Captures the loser's identity plus its mention and link rows exactly as they
    stand before ``merge_entity`` copies-then-DELETEs them, so a later
    ``unmerge_entity`` can reconstruct the loser node. No commit — runs inside the
    caller's merge transaction.
    """
    cur = await db.execute(
        "SELECT name, norm_name, entity_type FROM entities WHERE entity_id = ?",
        (loser_id,),
    )
    row = await cur.fetchone()
    loser_name, loser_norm, loser_type = (row[0], row[1], row[2]) if row else (None, None, None)
    mcur = await db.execute(
        "SELECT memory_id, provenance, confidence, source, created_at "
        "FROM entity_mentions WHERE entity_id = ?",
        (loser_id,),
    )
    mentions = [
        {
            "memory_id": r[0],
            "provenance": r[1],
            "confidence": r[2],
            "source": r[3],
            "created_at": r[4],
        }
        for r in await mcur.fetchall()
    ]
    lcur = await db.execute(
        "SELECT source_id, target_id, link_type, provenance, confidence, "
        "evidence_memory_id, valid_at, invalid_at, invalidated_by, created_at "
        "FROM entity_links WHERE source_id = ? OR target_id = ?",
        (loser_id, loser_id),
    )
    links = [
        {
            "source_id": r[0],
            "target_id": r[1],
            "link_type": r[2],
            "provenance": r[3],
            "confidence": r[4],
            "evidence_memory_id": r[5],
            "valid_at": r[6],
            "invalid_at": r[7],
            "invalidated_by": r[8],
            "created_at": r[9],
        }
        for r in await lcur.fetchall()
    ]
    await db.execute(
        "INSERT INTO entity_merge_journal "
        "(id, loser_id, survivor_id, loser_name, loser_norm, loser_type, "
        "mentions_json, links_json, merged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            loser_id,
            survivor_id,
            loser_name,
            loser_norm,
            loser_type,
            json.dumps(mentions),
            json.dumps(links),
            now,
        ),
    )


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

    Human-gated primitive: this is the irreversible entity tombstone the whole
    adjudication approval gate exists to protect, and the app-level FLOOR of that gate
    (below this is raw SQL on the DB — out of scope, tracked separately). Refuse a
    dispatched/unsupervised session here too, so importing merge_entity directly can't
    bypass the approve/apply guards one layer up. Both legitimate callers run in-server
    (the live-mode drainer and the gated apply path, neither dispatched), so this is a
    no-op for them; it only blocks a dispatched-session direct import. See
    ``guard_human_gate``.
    """
    from genesis.security.immunity_shadow import guard_human_gate

    guard_human_gate("merge_entity")
    # Self-merge guard: writing ``merged_into = self`` makes the entity
    # unresolvable (the _resolve_active seen-set walk dead-ends), so a caller
    # that resolved both sides to the same row (easy via merge-following
    # get_by_norm_name) must fail loud, not silently corrupt the row.
    if loser_id == survivor_id:
        raise ValueError(f"merge_entity: self-merge refused (loser == survivor == {loser_id!r})")
    now = datetime.now(UTC).isoformat()
    # Reversibility journal: snapshot the loser BEFORE the destructive DELETEs
    # below. merge_entity physically removes the loser's mention/link rows, so
    # without this an applied merge cannot be undone (see entity_merge_journal +
    # the unmerge_entity follow-up). Same transaction as the merge itself.
    await _journal_merge(db, loser_id=loser_id, survivor_id=survivor_id, now=now)
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
        "DELETE FROM entity_mentions WHERE entity_id = ?",
        (loser_id,),
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
    # Chain compaction (union-find re-point): rows that pointed at the loser
    # now point straight at the survivor, so redirect chains stay one hop on
    # the WRITE side; the read-side walk (resolve_active) covers any row this
    # misses. The survivor itself is excluded defensively — it is active, but
    # a corrupt merged_into=loser on it must not become a self-loop.
    await db.execute(
        "UPDATE entities SET merged_into = ?, updated_at = ? "
        "WHERE merged_into = ? AND entity_id != ?",
        (survivor_id, now, loser_id, survivor_id),
    )
    if _commit:
        await db.commit()


async def prune_merge_journal(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 180,
    now: str,
    _commit: bool = True,
) -> int:
    """Delete ``entity_merge_journal`` rows older than *older_than_days*.

    Retention for the unbounded reversibility journal (wired into
    ``scripts/disk_hygiene.sh``). The window is generous (180d default) because the
    journal is a safety net for ``unmerge_entity`` — it must outlive the "did we
    mis-merge?" discovery window, not just an audit horizon. ``now`` is injected
    (never wall-clock here) for deterministic tests. No-ops before the
    approval-gate migration (table-existence guard). Returns rows deleted.

    Rejects a sub-1-day retention window: with ``older_than_days <= 0`` the cutoff
    lands at or in the FUTURE relative to ``now`` (a negative subtracts a negative,
    pushing it forward), so ``merged_at < cutoff`` would match EVERY row and wipe
    the entire reversibility journal — the one store ``unmerge_entity`` depends on.
    A retention window that destroys the whole safety net is always a bug, so fail
    loud rather than silently deleting it.
    """
    if older_than_days < 1:
        raise ValueError(
            f"prune_merge_journal: retention window must be >= 1 day, got "
            f"{older_than_days!r}; a sub-1 window sets the cutoff at/after now and "
            f"would delete the ENTIRE entity_merge_journal (reversibility safety net)."
        )
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_merge_journal'"
    )
    if not await cur.fetchone():
        return 0
    cutoff = (datetime.fromisoformat(now) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute("DELETE FROM entity_merge_journal WHERE merged_at < ?", (cutoff,))
    if _commit:
        await db.commit()
    return cursor.rowcount or 0


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


# Enabled: the entity_adjudication drainer (genesis.memory.entity_adjudication)
# is the consumer. `enqueue_adjudication` writes `entity_adjudication` rows to
# `deferred_work_queue`; the hourly drainer LLM-judges each fuzzy pair merge-vs-
# distinct (propose_only by default). The whole feature is gated at runtime by
# the entity_adjudication settings lever (mode=off no-ops the drainer); this
# module-level flag stays as the producer kill switch of last resort. When the
# drainer's mode is off, rows still enqueue but simply never drain — set this
# False (or the lever off) if you want to stop enqueuing entirely.
_ADJUDICATION_ENQUEUE_ENABLED = True


async def merged_norm_redirects(db: aiosqlite.Connection) -> dict[str, list[str]]:
    """Map each merged-away surface form to its ACTIVE survivors' entity_ids.

    The query lane builds its name map from active norms only, so the moment a
    merge applies, every loser's surface form goes dark — a user asking by the
    OLD name gets nothing (the gap entity_query's docstring recorded from day
    one; MW-3 PR-2b closes it). Chains are resolved in-process over one scan;
    dead-ended chains (gone / no target / cycle) are dropped. A norm ALSO
    owned by an active row (legal across types under UNIQUE(norm_name,
    entity_type)) still gets its redirect — the survivor rides ALONGSIDE the
    live row, because suppressing it makes the merged entity unfindable by
    its old surface form; the consumer's map is list-valued and dedups.
    LIST-valued because UNIQUE(norm_name, entity_type) allows one norm on two
    merged rows of different types with different survivors — a single pick
    from an unordered scan would be nondeterministic; both are carried and the
    consumer's map is already list-valued."""
    rows = await db.execute_fetchall(
        "SELECT entity_id, norm_name, status, merged_into FROM entities"
    )
    by_id: dict[str, tuple[str, str | None]] = {}
    merged: list[tuple[str, str]] = []  # (norm_name, merged_into)
    for entity_id, norm_name, status, merged_into in rows:
        by_id[entity_id] = (status, merged_into)
        if status == "merged" and merged_into:
            merged.append((norm_name, merged_into))
    out: dict[str, list[str]] = {}
    for norm_name, target in merged:
        seen: set[str] = set()
        current: str | None = target
        while current and current not in seen:
            seen.add(current)
            status, nxt = by_id.get(current, (None, None))
            if status == "active":
                bucket = out.setdefault(norm_name, [])
                if current not in bucket:
                    bucket.append(current)
                break
            if status == "merged" and nxt:
                current = nxt
                continue
            break  # dead end — drop
    return out


async def enqueue_adjudication(
    db: aiosqlite.Connection,
    *,
    entity_id: str,
    similar_entity_id: str,
    _commit: bool = True,
) -> bool:
    """Queue a fuzzy-match pair for the entity_adjudication drainer.

    Returns True iff a queue row was actually inserted — False on the two
    silent no-op paths (kill switch off, pending-row dedup) so callers can
    count real enqueues instead of attempts.

    Inline INSERT rather than ``deferred_work.create`` — that helper
    commits unconditionally, which would break callers batching under
    ``_commit=False`` (extraction transaction discipline).

    No-op while ``_ADJUDICATION_ENQUEUE_ENABLED`` is False. Deduped: if a pending
    row already exists for this pair in EITHER orientation, no new row is written
    (the producer has no natural dedup key, so a repeated fuzzy collision would
    otherwise pile up duplicate rows — the exact leak that motivated the gate).
    The caller's entity create + AMBIGUOUS status are unaffected.
    """
    if not _ADJUDICATION_ENQUEUE_ENABLED:
        return False
    now = datetime.now(UTC).isoformat()
    payload_fwd = json.dumps({"entity_id": entity_id, "similar_entity_id": similar_entity_id})
    payload_rev = json.dumps({"entity_id": similar_entity_id, "similar_entity_id": entity_id})
    cursor = await db.execute(
        """INSERT INTO deferred_work_queue
           (id, work_type, call_site_id, priority, payload_json, deferred_at,
            deferred_reason, created_at)
           SELECT ?, 'entity_adjudication', 'entity_adjudication', 60, ?, ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM deferred_work_queue
               WHERE work_type = 'entity_adjudication' AND status = 'pending'
                 AND payload_json IN (?, ?)
           )""",
        (
            str(uuid.uuid4()),
            payload_fwd,
            now,
            "fuzzy norm_name match at entity creation",
            now,
            payload_fwd,
            payload_rev,
        ),
    )
    inserted = cursor.rowcount > 0
    if _commit:
        await db.commit()
    return inserted


def _row_to_dict(db: aiosqlite.Connection, row) -> dict:
    """Decode a row from a `SELECT {_SELECT_COLS}` query (module-top constant).

    Tuple rows zip against _ENTITY_COLUMNS — correct by construction because
    every SELECT in this module names its columns in that same order; physical
    table layout is irrelevant. strict=True fails loud on any length mismatch
    (a SELECT that doesn't use _SELECT_COLS) rather than silently mis-aligning.
    """
    if isinstance(row, aiosqlite.Row) or hasattr(row, "keys"):
        return dict(row)
    return dict(zip(_ENTITY_COLUMNS, row, strict=True))
