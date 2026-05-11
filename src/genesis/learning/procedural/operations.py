"""Higher-level procedural memory operations wrapping CRUD."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import procedural

logger = logging.getLogger(__name__)


@dataclass
class StoreResult:
    """Result of a conflict-checked procedure store."""

    procedure_id: str
    action: str  # "created" | "updated" | "skipped"
    warnings: list[str] = field(default_factory=list)
    conflicting_ids: list[str] = field(default_factory=list)


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
    source: dict | None = None,
    principle_embedding: bytes | None = None,
) -> str:
    """Create a new procedure and return its ID.

    Defaults match the extractor path (speculative=1, success_count=0,
    confidence=0.0). Callers that represent explicit confirmations — e.g.,
    user-driven `procedure_store` MCP writes — should pass non-default
    values to seed the procedure as already-trusted (speculative=0,
    success_count>=1, confidence via Laplace).

    `principle_embedding` is the packed BLOB returned by
    `procedural.embedding.pack_embedding`. Optional — when None, the
    proactive procedure hook skips this row.
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
        source=json.dumps(source) if source else None,
        principle_embedding=principle_embedding,
    )
    return proc_id


async def store_procedure_checked(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    activation_tier: str = "L3",
    tool_trigger: list[str] | None = None,
    speculative: int = 0,
    success_count: int = 1,
    confidence: float = 2 / 3,
    source: dict | None = None,
    principle_embedding: bytes | None = None,
) -> StoreResult:
    """Store a procedure with conflict detection.

    Defaults match the explicit-teach path (speculative=0, success_count=1).

    Conflict resolution:
    - Exact task_type match, incoming is auto-extracted but existing is
      explicit-teach → skip (don't overwrite human-taught).
    - Exact task_type match, same source type → upsert (update content,
      preserve operational history, bump version).
    - High context_tag overlap with different task_type → warn but still create.
    """
    existing = await procedural.find_by_task_type(db, task_type)

    if existing:
        # Auto-extracted should never overwrite explicit-teach
        if speculative == 1 and existing.get("speculative") == 0:
            logger.info(
                "Skipped auto-extracted procedure for %s: explicit-teach %s exists",
                task_type, existing["id"],
            )
            return StoreResult(
                procedure_id=existing["id"],
                action="skipped",
                warnings=["Auto-extracted procedure skipped: explicit-teach already exists"],
                conflicting_ids=[existing["id"]],
            )

        # Upsert: update content, preserve operational history (counts, confidence)
        new_version = existing.get("version", 1) + 1
        await procedural.update(
            db,
            existing["id"],
            principle=principle,
            steps=steps,
            tools_used=tools_used,
            context_tags=context_tags,
            tool_trigger=tool_trigger,
            version=new_version,
            source=json.dumps(source) if source else existing.get("source"),
        )
        logger.info("Updated procedure %s (v%d): %s", existing["id"], new_version, task_type)
        return StoreResult(
            procedure_id=existing["id"],
            action="updated",
        )

    # Check for high context_tag overlap with different task_types
    warnings: list[str] = []
    conflicting_ids: list[str] = []
    overlapping = await procedural.find_by_context_overlap(db, context_tags)
    for row in overlapping:
        warnings.append(
            f"High context overlap with '{row['task_type']}' (id={row['id']})"
        )
        conflicting_ids.append(row["id"])

    # Create new procedure
    proc_id = await store_procedure(
        db,
        task_type=task_type,
        principle=principle,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        activation_tier=activation_tier,
        tool_trigger=tool_trigger,
        speculative=speculative,
        success_count=success_count,
        confidence=confidence,
        source=source,
        principle_embedding=principle_embedding,
    )

    if warnings:
        logger.info("Procedure %s created with %d overlap warnings", proc_id, len(warnings))

    return StoreResult(
        procedure_id=proc_id,
        action="created",
        warnings=warnings,
        conflicting_ids=conflicting_ids,
    )


async def record_success(db: aiosqlite.Connection, procedure_id: str) -> bool:
    """Increment success_count, update confidence via Laplace smoothing."""
    row = await procedural.get_by_id(db, procedure_id)
    if row is None:
        return False
    s = row["success_count"] + 1
    f = row["failure_count"]
    confidence = (s + 1) / (s + f + 2)
    now = datetime.now(UTC).isoformat()
    result = await procedural.update(
        db, procedure_id,
        success_count=s,
        confidence=confidence,
        last_used=now,
    )
    # J-9 eval: log procedure outcome
    if result:
        from genesis.eval.j9_hooks import emit_procedure_outcome
        await emit_procedure_outcome(
            db, procedure_id=procedure_id, success=True,
            confidence_after=confidence,
        )
    return result


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

    result = await procedural.update(
        db, procedure_id,
        failure_count=f,
        failure_modes=modes,
        confidence=confidence,
        last_used=datetime.now(UTC).isoformat(),
    )
    # J-9 eval: log procedure outcome
    if result:
        from genesis.eval.j9_hooks import emit_procedure_outcome
        await emit_procedure_outcome(
            db, procedure_id=procedure_id, success=False,
            confidence_after=confidence,
        )
    return result


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
