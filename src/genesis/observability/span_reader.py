"""Read-side queries over ``otel_spans`` — trace reconstruction.

The foundation the trace-waterfall dashboard (a later track) consumes. Three
shapes: a recent-traces list (root spans), a single trace reassembled into a
parent/child tree, and a session's flat span list. All read-only and best-effort
— a malformed ``attributes_json`` degrades to a raw string, never raises.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


def _hydrate(row: dict) -> dict:
    """Parse attributes_json into an ``attributes`` dict (best-effort)."""
    raw = row.get("attributes_json")
    if raw:
        try:
            row["attributes"] = json.loads(raw)
        except (TypeError, ValueError):
            row["attributes"] = {"_raw": raw}
    else:
        row["attributes"] = {}
    return row


async def list_recent_traces(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    """Most recent traces (by root span), newest first, with a span count.

    Uses the partial root index (parent_span_id IS NULL).
    """
    import aiosqlite as _sql

    db.row_factory = _sql.Row
    cur = await db.execute(
        "SELECT span_id, trace_id, name, kind, status, start_unix_us, "
        "duration_us, session_id FROM otel_spans WHERE parent_span_id IS NULL "
        "ORDER BY start_unix_us DESC LIMIT ?",
        (limit,),
    )
    roots = [dict(r) for r in await cur.fetchall()]
    if not roots:
        return []

    trace_ids = [r["trace_id"] for r in roots]
    placeholders = ", ".join("?" * len(trace_ids))
    cur = await db.execute(
        f"SELECT trace_id, COUNT(*) FROM otel_spans "  # noqa: S608 - literal SQL fragments; values bound as parameters
        f"WHERE trace_id IN ({placeholders}) GROUP BY trace_id",
        trace_ids,
    )
    counts = {row[0]: row[1] for row in await cur.fetchall()}
    for r in roots:
        r["span_count"] = counts.get(r["trace_id"], 1)
    return roots


async def get_trace(db: aiosqlite.Connection, trace_id: str) -> dict | None:
    """Reassemble one trace into a parent/child tree, or None if unknown.

    Spans whose parent isn't in this trace (rare — e.g. a cc.session parent that
    failed to persist) surface as additional roots rather than being dropped.
    """
    import aiosqlite as _sql

    db.row_factory = _sql.Row
    cur = await db.execute(
        "SELECT * FROM otel_spans WHERE trace_id = ? ORDER BY start_unix_us",
        (trace_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return None

    nodes: dict[str, dict] = {}
    for r in rows:
        node = _hydrate(r)
        node["children"] = []
        nodes[r["span_id"]] = node

    roots: list[dict] = []
    for r in rows:
        node = nodes[r["span_id"]]
        pid = r.get("parent_span_id")
        parent = nodes.get(pid) if pid else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)  # true root, or orphan (parent absent from trace)

    return {"trace_id": trace_id, "span_count": len(rows), "roots": roots}


async def get_session_spans(
    db: aiosqlite.Connection, session_id: str, *, limit: int = 500
) -> list[dict]:
    """All spans correlated to a Genesis session (flat, chronological).

    Useful when cross-process traces aren't stitched (session_id is always a
    reliable correlation key even when trace_id linkage is best-effort).
    """
    import aiosqlite as _sql

    db.row_factory = _sql.Row
    cur = await db.execute(
        "SELECT * FROM otel_spans WHERE session_id = ? "
        "ORDER BY start_unix_us LIMIT ?",
        (session_id, limit),
    )
    return [_hydrate(dict(r)) for r in await cur.fetchall()]


def flatten_tree(trace: dict) -> list[dict[str, Any]]:
    """Depth-first flatten of get_trace() output (for a waterfall render order)."""
    out: list[dict] = []

    def _walk(node: dict, depth: int) -> None:
        row = {k: v for k, v in node.items() if k != "children"}
        row["depth"] = depth
        out.append(row)
        for child in node.get("children", []):
            _walk(child, depth + 1)

    for root in trace.get("roots", []):
        _walk(root, 0)
    return out
