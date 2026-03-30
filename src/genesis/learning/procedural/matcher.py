"""Procedural memory matching — find best procedure for a task."""

from __future__ import annotations

import json

import aiosqlite

from genesis.db.crud import procedural
from genesis.learning.types import ProcedureMatch


def _row_to_match(row: dict) -> ProcedureMatch:
    """Convert a CRUD row dict to a ProcedureMatch."""
    failure_modes = json.loads(row["failure_modes"]) if row["failure_modes"] else []
    workarounds = json.loads(row["attempted_workarounds"]) if row["attempted_workarounds"] else []
    steps = json.loads(row["steps"]) if isinstance(row["steps"], str) else row["steps"]
    tool_trigger = json.loads(row["tool_trigger"]) if row.get("tool_trigger") else None
    return ProcedureMatch(
        procedure_id=row["id"],
        task_type=row["task_type"],
        confidence=row["confidence"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        failure_modes=failure_modes,
        workarounds=workarounds,
        activation_tier=row.get("activation_tier", "L4"),
        tool_trigger=tool_trigger,
        steps=steps,
        principle=row.get("principle"),
    )


async def find_best_match(
    db: aiosqlite.Connection,
    task_type: str,
    context_tags: list[str],
) -> ProcedureMatch | None:
    """Find the best matching procedure by task_type + context tag overlap.

    Ranks by confidence * context_overlap_score where
    overlap = len(intersection) / len(union) of context_tags (Jaccard).
    Returns None if no non-deprecated procedures match.
    """
    rows = await procedural.list_by_task_type(db, task_type)
    if not rows:
        return None

    query_tags = set(context_tags)
    best_score = -1.0
    best_row = None

    for row in rows:
        if row.get("deprecated") or row.get("quarantined"):
            continue
        row_tags = set(json.loads(row["context_tags"]) if isinstance(row["context_tags"], str) else row["context_tags"])
        union = query_tags | row_tags
        overlap = 0.0 if not union else len(query_tags & row_tags) / len(union)
        score = row["confidence"] * overlap
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None or best_score <= 0.0:
        return None

    return _row_to_match(best_row)


async def find_relevant(
    db: aiosqlite.Connection,
    context_tags: list[str],
    *,
    min_confidence: float = 0.3,
    limit: int = 5,
) -> list[ProcedureMatch]:
    """Find procedures relevant to the given context tags across all task types.

    Broader than find_best_match — searches all active procedures, not just one
    task_type. Used by SessionStart injection and procedure_recall MCP tool.
    """
    rows = await procedural.list_active(db, limit=200)
    if not rows:
        return []

    query_tags = set(context_tags)
    scored: list[tuple[float, dict]] = []

    for row in rows:
        if row["confidence"] < min_confidence:
            continue
        row_tags = set(json.loads(row["context_tags"]) if isinstance(row["context_tags"], str) else row["context_tags"])
        union = query_tags | row_tags
        if not union:
            continue
        overlap = len(query_tags & row_tags) / len(union)
        if overlap <= 0.0:
            continue
        score = row["confidence"] * overlap
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [_row_to_match(row) for _, row in scored[:limit]]
