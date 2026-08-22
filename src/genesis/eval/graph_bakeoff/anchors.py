"""Resolve concrete per-query anchors FROM a snapshot (reproducible per sha256).

The anchor SELECTION method is frozen in ``queries.py`` (``anchor_sql`` + the docs
here); the RESOLVED ids are snapshot-specific and recorded in ``anchors-<sha>.json``
next to the snapshot so a re-run against the same snapshot is bit-reproducible.

S1 resolves the PRIMARY anchor per query (enough to prove parity). The second
(median-degree) anchor for latency fairness is an S3 addition — flagged inline.
An anchor may resolve to ``None`` when the snapshot has no qualifying row; the
query then returns the empty set, which is itself a findings-doc datum (e.g. the
4-edge ``contradicts`` scarcity leaving q2 empty).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> tuple | None:
    row = conn.execute(sql, params).fetchone()
    return tuple(row) if row else None


def _median_created_at(conn: sqlite3.Connection) -> str | None:
    """The median memory_metadata.created_at — the pinned as-of date D for q3."""
    n = conn.execute(
        "SELECT count(*) FROM memory_metadata WHERE created_at IS NOT NULL"
    ).fetchone()[0]
    if not n:
        return None
    row = conn.execute(
        "SELECT created_at FROM memory_metadata WHERE created_at IS NOT NULL "
        "ORDER BY created_at LIMIT 1 OFFSET ?",
        (n // 2,),
    ).fetchone()
    return row[0] if row else None


def resolve_anchors(snapshot_path: str) -> dict:
    """Resolve every query's anchors against the snapshot. Read-only."""
    conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    try:
        # q1: hub entity (most mentions)
        r = _one(
            conn,
            "SELECT entity_id, count(*) c FROM entity_mentions GROUP BY entity_id ORDER BY c DESC LIMIT 1",
        )
        q1 = {"entity_id": r[0] if r else None, "mention_count": r[1] if r else 0}

        # q3: a memory with >=3 out-neighbors carrying validity data + pinned date D.
        # The HAVING count(*) >= 3 is the FROZEN qualification from queries.py's
        # q3 anchor_sql — without it a snapshot with no >=3-neighbor source would
        # anchor on a 1-or-2-link source, silently benchmarking an easier workload
        # than the preregistered query (anchor must resolve to None instead).
        r = _one(
            conn,
            "SELECT ml.source_id, count(*) c FROM memory_links ml JOIN memory_metadata mm "
            "ON mm.memory_id = ml.target_id WHERE mm.valid_at IS NOT NULL "
            "GROUP BY ml.source_id HAVING count(*) >= 3 ORDER BY c DESC LIMIT 1",
        )
        q3 = {"memory_id": r[0] if r else None, "as_of": _median_created_at(conn)}

        # q4: a chain root (source of succeeded_by that is not itself a succeeded_by target)
        r = _one(
            conn,
            "SELECT source_id FROM memory_links WHERE link_type='succeeded_by' "
            "AND source_id NOT IN (SELECT target_id FROM memory_links WHERE link_type='succeeded_by') LIMIT 1",
        )
        q4 = {"root": r[0] if r else None}

        # q5: two entities that co-occur on >=1 memory
        r = _one(
            conn,
            "SELECT a.entity_id, b.entity_id, count(*) c FROM entity_mentions a "
            "JOIN entity_mentions b ON a.memory_id = b.memory_id AND a.entity_id < b.entity_id "
            "GROUP BY a.entity_id, b.entity_id ORDER BY c DESC LIMIT 1",
        )
        q5 = {
            "entity_a": r[0] if r else None,
            "entity_b": r[1] if r else None,
            "co_count": r[2] if r else 0,
        }

        # q2/q6: whole-graph over the (tiny) contradicts set — record how many exist
        n_contra = conn.execute(
            "SELECT count(*) FROM memory_links WHERE link_type='contradicts'"
        ).fetchone()[0]

        return {
            "q1_entity_dossier": q1,
            "q2_decision_chain": {"contradicts_edges": n_contra},
            "q3_as_of_neighborhood": q3,
            "q4_chain_closure": q4,
            "q5_cross_entity": q5,
            "q6_contradiction_sweep": {"contradicts_edges": n_contra},
            "q7_centrality": {},
            "q8_cold_start": {},
        }
    finally:
        conn.close()


def write_anchors(snapshot_path: str, sha256: str, out_dir: Path) -> Path:
    anchors = resolve_anchors(snapshot_path)
    dest = out_dir / f"anchors-{sha256[:16]}.json"
    dest.write_text(json.dumps({"sha256": sha256, "anchors": anchors}, indent=2))
    return dest
