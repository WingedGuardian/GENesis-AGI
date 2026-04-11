"""Higher-level procedural memory operations wrapping CRUD."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import procedural


async def store_procedure(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    activation_tier: str = "L4",
    tool_trigger: list[str] | None = None,
    speculative: int = 1,
    success_count: int = 0,
    confidence: float = 0.0,
) -> str:
    """Create a new procedure and return its ID.

    Defaults match the extractor path (speculative=1, success_count=0,
    confidence=0.0). Callers that represent explicit confirmations — e.g.,
    user-driven `procedure_store` MCP writes — should pass non-default
    values to seed the procedure as already-trusted (speculative=0,
    success_count>=1, confidence via Laplace).
    """
    proc_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await procedural.create(
        db,
        id=proc_id,
        task_type=task_type,
        principle=principle,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        created_at=now,
        activation_tier=activation_tier,
        tool_trigger=tool_trigger,
        speculative=speculative,
        success_count=success_count,
        confidence=confidence,
    )
    return proc_id


async def record_success(db: aiosqlite.Connection, procedure_id: str) -> bool:
    """Increment success_count, update confidence via Laplace smoothing."""
    row = await procedural.get_by_id(db, procedure_id)
    if row is None:
        return False
    s = row["success_count"] + 1
    f = row["failure_count"]
    confidence = (s + 1) / (s + f + 2)
    now = datetime.now(UTC).isoformat()
    return await procedural.update(
        db, procedure_id,
        success_count=s,
        confidence=confidence,
        last_used=now,
    )


async def record_failure(
    db: aiosqlite.Connection,
    procedure_id: str,
    *,
    condition: str,
    transient: bool = False,
) -> bool:
    """Increment failure_count, append to failure_modes, update confidence."""
    row = await procedural.get_by_id(db, procedure_id)
    if row is None:
        return False
    s = row["success_count"]
    f = row["failure_count"] + 1
    confidence = (s + 1) / (s + f + 2)

    modes = json.loads(row["failure_modes"]) if row["failure_modes"] else []
    # Check if this condition already exists
    existing = next((m for m in modes if m.get("description") == condition), None)
    if existing:
        existing["times_hit"] = existing.get("times_hit", 1) + 1
    else:
        modes.append({
            "description": condition,
            "conditions": condition,
            "times_hit": 1,
            "transient": transient,
        })

    return await procedural.update(
        db, procedure_id,
        failure_count=f,
        failure_modes=modes,
        confidence=confidence,
        last_used=datetime.now(UTC).isoformat(),
    )


async def record_workaround(
    db: aiosqlite.Connection,
    procedure_id: str,
    *,
    failed_method: str,
    working_method: str,
    context: str,
) -> bool:
    """Append a workaround entry to attempted_workarounds JSON."""
    row = await procedural.get_by_id(db, procedure_id)
    if row is None:
        return False
    workarounds = json.loads(row["attempted_workarounds"]) if row["attempted_workarounds"] else []
    workarounds.append({
        "description": working_method,
        "outcome": f"replaced: {failed_method}",
        "conditions": context,
    })
    return await procedural.update(
        db, procedure_id,
        attempted_workarounds=workarounds,
    )


async def update_confidence(db: aiosqlite.Connection, procedure_id: str) -> float:
    """Recalculate and persist Laplace-smoothed confidence. Returns new value."""
    row = await procedural.get_by_id(db, procedure_id)
    if row is None:
        return 0.0
    s = row["success_count"]
    f = row["failure_count"]
    confidence = (s + 1) / (s + f + 2)
    await procedural.update(db, procedure_id, confidence=confidence)
    return confidence
