"""Pure-SQL parity oracle — the ground truth every engine must match.

The oracle is NOT a contestant: it defines the correct reachable node-id SET for
each ``exact_set`` query via recursive CTEs over the snapshot. Engines (NX control,
ladybug, falkor) are correct iff their node-id set equals the oracle's. ``bench
time`` refuses to run without a green parity report.

Canonical semantics per query are documented in ``queries.py``. Comparison is on
frozensets of memory_ids, so BFS/CTE/Cypher path-multiplicity differences wash out.
q7 (centrality) has no SQL oracle — betweenness is compared engine-vs-NX-reference
in S2/S3. q8 is timing-only.
"""

from __future__ import annotations

import sqlite3


def _set(rows) -> frozenset[str]:
    return frozenset(r[0] for r in rows if r[0] is not None)


# ── exact_set oracles ────────────────────────────────────────────────────────


def oracle_q1(
    conn: sqlite3.Connection, entity_id: str | None, *, max_depth: int, min_strength: float
) -> frozenset[str]:
    if not entity_id:
        return frozenset()
    rows = conn.execute(
        """
        WITH RECURSIVE seeds(id) AS (
            SELECT memory_id FROM entity_mentions WHERE entity_id = ?
        ),
        nb(id, depth) AS (
            SELECT id, 0 FROM seeds
            UNION
            SELECT ml.target_id, nb.depth + 1
            FROM memory_links ml JOIN nb ON ml.source_id = nb.id
            WHERE nb.depth < ? AND ml.strength >= ?
        )
        SELECT DISTINCT id FROM nb
        """,
        (entity_id, max_depth, min_strength),
    ).fetchall()
    return _set(rows)


def oracle_q3(
    conn: sqlite3.Connection, memory_id: str | None, as_of: str | None, *, max_depth: int
) -> frozenset[str]:
    if not memory_id or not as_of:
        return frozenset()
    rows = conn.execute(
        """
        WITH RECURSIVE nb(id, depth) AS (
            SELECT ?, 0
            UNION
            SELECT ml.target_id, nb.depth + 1
            FROM memory_links ml
            JOIN nb ON ml.source_id = nb.id
            JOIN memory_metadata mm ON mm.memory_id = ml.target_id
            WHERE nb.depth < ?
              AND mm.valid_at IS NOT NULL AND mm.valid_at <= ?
              AND (mm.invalid_at IS NULL OR mm.invalid_at > ?)
              AND NOT (mm.deprecated = 1 AND mm.deprecated_at IS NOT NULL AND mm.deprecated_at <= ?)
        )
        SELECT DISTINCT id FROM nb WHERE id != ?
        """,
        (memory_id, max_depth, as_of, as_of, as_of, memory_id),
    ).fetchall()
    return _set(rows)


def oracle_q4(
    conn: sqlite3.Connection, root: str | None, *, link_type: str, max_depth: int
) -> frozenset[str]:
    if not root:
        return frozenset()
    # Depth-bounded closure (honors the frozen max_depth): a node is included iff
    # its min-distance from root via link_type edges is <= max_depth. The depth
    # column also bounds cycle expansion (a cycle re-emits an id at rising depths
    # only until the bound), matching the control's depth-counter BFS.
    rows = conn.execute(
        """
        WITH RECURSIVE closure(id, depth) AS (
            SELECT ?, 0
            UNION
            SELECT ml.target_id, closure.depth + 1
            FROM memory_links ml JOIN closure ON ml.source_id = closure.id
            WHERE ml.link_type = ? AND closure.depth < ?
        )
        SELECT DISTINCT id FROM closure
        """,
        (root, link_type, max_depth),
    ).fetchall()
    return _set(rows)


def oracle_q5(
    conn: sqlite3.Connection, entity_a: str | None, entity_b: str | None
) -> frozenset[str]:
    if not entity_a or not entity_b:
        return frozenset()
    rows = conn.execute(
        """
        WITH co(memory_id) AS (
            SELECT memory_id FROM entity_mentions WHERE entity_id = ?
            INTERSECT
            SELECT memory_id FROM entity_mentions WHERE entity_id = ?
        )
        SELECT memory_id AS id FROM co
        UNION
        SELECT ml.target_id FROM memory_links ml JOIN co ON ml.source_id = co.memory_id
        UNION
        SELECT ml.source_id FROM memory_links ml JOIN co ON ml.target_id = co.memory_id
        """,
        (entity_a, entity_b),
    ).fetchall()
    return _set(rows)


def oracle_q6(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute(
        """
        SELECT ml.source_id
        FROM memory_links ml
        JOIN memory_metadata s ON s.memory_id = ml.source_id
        JOIN memory_metadata t ON t.memory_id = ml.target_id
        WHERE ml.link_type = 'contradicts' AND s.created_at > t.created_at
        """
    ).fetchall()
    return _set(rows)


def oracle_q2(conn: sqlite3.Connection, *, max_depth: int) -> frozenset[str]:
    """Decisions from which a contradicts-edge endpoint is reachable via supports.

    Expressiveness-scored; on today's data (contradicts=4) this is expected to be
    near-empty. Bounded by ``max_depth`` supports hops.
    """
    rows = conn.execute(
        """
        WITH contradicts_nodes(id) AS (
            SELECT source_id FROM memory_links WHERE link_type = 'contradicts'
            UNION SELECT target_id FROM memory_links WHERE link_type = 'contradicts'
        ),
        decisions(d) AS (
            SELECT DISTINCT source_id FROM memory_links WHERE link_type = 'decided'
        ),
        reach(d, id, depth) AS (
            SELECT d, d, 0 FROM decisions
            UNION
            SELECT reach.d, ml.target_id, reach.depth + 1
            FROM memory_links ml JOIN reach ON ml.source_id = reach.id
            WHERE ml.link_type = 'supports' AND reach.depth < ?
        )
        SELECT DISTINCT reach.d
        FROM reach JOIN contradicts_nodes cn ON reach.id = cn.id
        """,
        (max_depth,),
    ).fetchall()
    return _set(rows)


# ── dispatch ─────────────────────────────────────────────────────────────────


def oracle_for(
    query_id: str, conn: sqlite3.Connection, anchors: dict, params: dict
) -> frozenset[str] | None:
    """Return the oracle result SET for an exact_set query, or None if the query
    has no SQL oracle (q7 centrality reference / q8 timing)."""
    a = anchors.get(query_id, {})
    if query_id == "q1_entity_dossier":
        return oracle_q1(
            conn,
            a.get("entity_id"),
            max_depth=params["max_depth"],
            min_strength=params["min_strength"],
        )
    if query_id == "q2_decision_chain":
        return oracle_q2(conn, max_depth=params["max_depth"])
    if query_id == "q3_as_of_neighborhood":
        return oracle_q3(conn, a.get("memory_id"), a.get("as_of"), max_depth=params["max_depth"])
    if query_id == "q4_chain_closure":
        return oracle_q4(
            conn, a.get("root"), link_type=params["link_type"], max_depth=params["max_depth"]
        )
    if query_id == "q5_cross_entity":
        return oracle_q5(conn, a.get("entity_a"), a.get("entity_b"))
    if query_id == "q6_contradiction_sweep":
        return oracle_q6(conn)
    return None  # q7, q8


def compare_exact_set(oracle: frozenset[str], engine: frozenset[str]) -> dict:
    """Structured parity verdict for an exact_set query."""
    missing = oracle - engine  # in oracle, engine failed to return
    extra = engine - oracle  # engine returned, not in oracle
    return {
        "match": not missing and not extra,
        "oracle_n": len(oracle),
        "engine_n": len(engine),
        "missing_n": len(missing),
        "extra_n": len(extra),
        "sample_missing": sorted(missing)[:5],
        "sample_extra": sorted(extra)[:5],
    }
