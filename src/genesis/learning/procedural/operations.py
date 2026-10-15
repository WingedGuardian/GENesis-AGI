"""Higher-level procedural memory operations wrapping CRUD."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import procedural
from genesis.learning.procedural.embedding import cosine_similarity, unpack_embedding
from genesis.learning.procedural.lesson_shape import (
    HELD_REASON,
    is_live_lesson,
    validate_lesson,
    validate_trigger_spec,
)
from genesis.learning.procedural.tool_names import names_a_tool, normalize_tool_trigger
from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED

logger = logging.getLogger(__name__)

# Cosine threshold above which an incoming principle is treated as the SAME
# procedure as an existing same-task_type row (→ refine-in-place) rather than a
# distinct lesson (→ create a new row). Deliberately conservative: `task_type`
# is a coarse free-text topic bucket, NOT an identity, so two genuinely distinct
# lessons that happen to share a slug MUST NOT overwrite each other — sameness
# requires positive embedding evidence. Reuses the production-validated value of
# ``extractor.NOVELTY_THRESHOLD`` (kept in sync by intent, not import, to avoid
# an operations→extractor layering cycle; that value is hand-calibrated, pending
# a retune on accumulated data). Near-duplicates below this bar
# accumulate as siblings (cleanable by an offline consolidation pass) instead of
# destroying the prior lesson — the data-loss bug this guards against.
SAME_PROCEDURE_THRESHOLD = 0.85

# Reads-as-signal: a deliberate procedure_recall ("read") is a soft positive
# signal. Every READ_CONFIDENCE_DISCOUNT reads count as one *effective* success.
# Recorded failures are the counterweight. This derived value is used ONLY for
# ranking + tier decisions — the stored `confidence` column stays real Laplace
# (success/failure only), so the j9 metric, quarantine, and demotion stay honest.
READ_CONFIDENCE_DISCOUNT = 5


def effective_confidence(
    success_count: int, failure_count: int, invocation_count: int
) -> float:
    """Laplace-smoothed confidence that folds reads in as fractional successes.

    ``eff_success = success_count + invocation_count // READ_CONFIDENCE_DISCOUNT``;
    returns ``(eff_success + 1) / (eff_success + failure_count + 2)``. With zero
    reads this is identical to the real Laplace confidence.
    """
    eff_success = success_count + invocation_count // READ_CONFIDENCE_DISCOUNT
    return (eff_success + 1) / (eff_success + failure_count + 2)


@dataclass
class StoreResult:
    """Result of a conflict-checked procedure store."""

    procedure_id: str
    action: str  # "created" | "updated" | "skipped"
    warnings: list[str] = field(default_factory=list)
    conflicting_ids: list[str] = field(default_factory=list)
    # Best same-task_type cosine similarity considered for the create-vs-update
    # decision (None when no embedding was available to compare). Observability +
    # test assertions; does not affect the stored row.
    matched_similarity: float | None = None
    # For a lesson write or a refine of a cached earned row: whether the trigger
    # cache was rebuilt (None when no rebuild was needed).
    cache_rebuilt: bool | None = None


async def store_procedure(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    scenario: str | None = None,
    activation_tier: str = "DORMANT",
    tool_trigger: list[str] | None = None,
    draft: int = 1,
    success_count: int = 0,
    confidence: float = 0.0,
    source: dict | None = None,
    principle_embedding: bytes | None = None,
    extraction_context: str | None = None,
    first_mover: int = 0,
    trigger_spec: dict | None = None,
    lesson: dict | None = None,
    deprecated_reason: str | None = None,
) -> str:
    """Create a new procedure and return its ID.

    Defaults match the extractor path (draft=1, success_count=0,
    confidence=0.0). Callers that represent explicit confirmations — e.g.,
    user-driven `procedure_store` MCP writes — should pass non-default
    values to seed the procedure as already-trusted (draft=0,
    success_count>=1, confidence via Laplace).

    `principle_embedding` is the packed BLOB returned by
    `procedural.embedding.pack_embedding`. Optional — when None, the
    proactive procedure hook skips this row.

    `extraction_context` is a JSON blob from the validation gate recording
    the gate's decision flags and modifiers (audit trail).

    `first_mover` is 1 when this is the first extraction for this task_type.
    """
    proc_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    tool_trigger, commands, dropped = normalize_tool_trigger(tool_trigger)
    if dropped:
        logger.info("Procedure %s: dropped non-tool trigger values %s", task_type, dropped)
    if commands and trigger_spec is None:
        trigger_spec, tool_trigger, spec_warnings = commands_spec(tool_trigger, commands)
        if spec_warnings:
            logger.info("Procedure %s: %s", task_type, "; ".join(spec_warnings))
    await procedural.create(
        db,
        id=proc_id,
        task_type=task_type,
        principle=principle,
        scenario=scenario,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        created_at=now,
        activation_tier=activation_tier,
        tool_trigger=tool_trigger or None,
        draft=draft,
        success_count=success_count,
        confidence=confidence,
        source=json.dumps(source) if source else None,
        principle_embedding=principle_embedding,
        extraction_context=extraction_context,
        first_mover=first_mover,
        trigger_spec=trigger_spec,
        lesson=lesson,
        deprecated_reason=deprecated_reason,
    )
    return proc_id


def commands_spec(tools: list[str], commands: list[str]) -> tuple[dict | None, list[str], list[str]]:
    """``(spec, stored_tools, warnings)`` for an earned row named by shell commands.

    The one rule every writer and the trigger backfill apply. A spec makes the
    row fire on those commands only, never on any Bash call its tags match. It
    replaces the row's tags for every tool, so it is written only when Bash is
    the row's one tool and the commands fit the validator's cap. Otherwise the
    command words are stored as written, inert (they name no tool), so the
    row's other tools keep their tags and no Bash is widened.
    """
    from genesis.learning.procedural.lesson_shape import MAX_PATTERNS

    if tools == ["Bash"] and len(commands) <= MAX_PATTERNS:
        spec = validate_trigger_spec({"patterns": [], "commands": commands, "repeat": "every"})
        return spec, tools, []
    stored = [t for t in tools if t != "Bash"] + [c for c in commands if c not in tools]
    return None, stored, [f"shell commands kept as written, not stored as a command spec: {commands}"]


def _best_same_procedure_match(
    candidates: list[dict], incoming_blob: bytes | None
) -> tuple[dict | None, float]:
    """Best same-task_type row whose principle embedding is ``>=
    SAME_PROCEDURE_THRESHOLD`` similar to the incoming one.

    Returns ``(row, similarity)`` when a same-procedure match is found, else
    ``(None, best_sim)`` (best_sim = the highest score seen, for observability).
    Sameness requires POSITIVE evidence — a missing incoming embedding, a
    candidate with no usable embedding, or a best score below the threshold all
    resolve to "not the same procedure", so the caller CREATES a distinct row
    rather than overwriting (a duplicate is cleanable; an overwrite is not).
    """
    incoming_vec = unpack_embedding(incoming_blob)
    if incoming_vec is None:
        return None, 0.0
    best_row: dict | None = None
    best_sim = 0.0
    for row in candidates:
        cand_vec = unpack_embedding(row.get("principle_embedding"))
        if cand_vec is None:
            continue
        sim = cosine_similarity(incoming_vec, cand_vec)
        if sim > best_sim:
            best_sim, best_row = sim, row
    if best_row is not None and best_sim >= SAME_PROCEDURE_THRESHOLD:
        return best_row, best_sim
    return None, best_sim


async def store_procedure_checked(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    scenario: str | None = None,
    activation_tier: str = "LIBRARY",
    tool_trigger: list[str] | None = None,
    draft: int = 0,
    success_count: int = 1,
    confidence: float = 2 / 3,
    source: dict | None = None,
    principle_embedding: bytes | None = None,
    trigger_spec: dict | None = None,
    lesson: dict | None = None,
    caller_origin: str | None = None,
) -> StoreResult:
    """Store a procedure with conflict detection.

    Defaults match the explicit-teach path (draft=0, success_count=1).

    Conflict resolution — identity is the PRINCIPLE (by embedding), not the
    ``task_type`` slug (a coarse free-text topic bucket):
    - A same-slug row whose principle is ``>= SAME_PROCEDURE_THRESHOLD`` similar
      → the SAME procedure → refine-in-place (bump version, preserve counts).
      An auto-extraction (draft=1) never overwrites a matched explicit-teach
      (draft=0) → skip. An explicit teach refining an auto row promotes it.
    - No same-procedure match (or no embedding to compare) → a DISTINCT lesson →
      CREATE a new row. This is the fix for the serial-overwrite data loss:
      distinct lessons sharing a slug coexist instead of destroying each other.
    - High context_tag overlap with a different task_type → warn but still create.

    A LESSON (``lesson`` and ``trigger_spec`` given) is authored, not earned:
    the normalizer's command phrases join the author's commands, both records are
    validated (``ValueError`` refuses the call), its tier comes from its
    origin (live ADVISORY, held LIBRARY) and the trigger cache is rebuilt.
    A lesson call refines only a lesson and a plain call only an earned row, so
    neither kind is ever rewritten as the other. A caller of
    ``external_untrusted`` origin never refines a lesson: it creates a held
    sibling, written deprecated with ``HELD_REASON`` so no reader sees it until
    the owner reviews it. A trusted author of the same lesson therefore creates
    a live lesson of its own rather than refining the held one.
    """
    trigger, commands, dropped = normalize_tool_trigger(tool_trigger)
    held = False
    if lesson is not None:
        if not trigger:
            raise ValueError("a lesson needs at least one tool name in tool_trigger")
        spec = dict(trigger_spec or {})
        authored = list(spec.get("commands") or [])
        if commands or authored:
            spec["commands"] = authored + [c for c in commands if c not in authored]
        try:
            trigger_spec = validate_trigger_spec(spec)
        except ValueError as exc:
            derived = len(spec.get("commands") or []) - len(authored)
            if derived and "commands" in str(exc):
                raise ValueError(
                    f"{exc} (counting {derived} command(s) derived from tool_trigger)"
                ) from exc
            raise
        lesson = validate_lesson(lesson)
        held = not is_live_lesson(lesson)
        activation_tier = "LIBRARY" if held else "ADVISORY"
    spec_warnings: list[str] = []
    if lesson is None and commands:
        # An earned row named by shell commands fires on those commands only.
        trigger_spec, trigger, spec_warnings = commands_spec(trigger, commands)
    trigger_warnings = (
        [f"tool_trigger values dropped as not tool names: {dropped}"] if dropped else []
    ) + spec_warnings
    # A lesson refines only a lesson, a plain teach only an earned row; the kind
    # is chosen in SQL so rows of the other kind cannot fill the limit.
    candidates = await procedural.list_by_task_type(
        db, task_type, lessons=lesson is not None, limit=None
    )
    if held or caller_origin == ORIGIN_EXTERNAL_UNTRUSTED:
        # Never let outside text rewrite a lesson; it becomes a sibling instead.
        # `held` is checked too, so a held record can never update a live row
        # whatever the caller passed as its origin.
        candidates = [c for c in candidates if c.get("lesson") is None]
    matched, best_sim = _best_same_procedure_match(candidates, principle_embedding)

    if matched is not None:
        # Auto-extracted should never overwrite an explicit-teach of the SAME
        # procedure (embedding-confirmed match, not just a shared slug).
        if draft == 1 and matched.get("draft") == 0:
            logger.info(
                "Skipped auto-extracted procedure for %s: matches explicit-teach %s "
                "(cosine=%.3f)",
                task_type, matched["id"], best_sim,
            )
            return StoreResult(
                procedure_id=matched["id"],
                action="skipped",
                warnings=["Auto-extracted procedure skipped: matches explicit-teach"],
                conflicting_ids=[matched["id"]],
                matched_similarity=best_sim,
            )

        # Refine in place: update content, preserve operational history
        # (counts, confidence). Overwrite is CORRECT here — it is the same
        # procedure, and `version` records the change.
        new_version = matched.get("version", 1) + 1
        update_fields: dict = dict(
            principle=principle,
            scenario=scenario,
            steps=steps,
            tools_used=tools_used,
            context_tags=context_tags,
            version=new_version,
            source=json.dumps(source) if source else matched.get("source"),
        )
        # Keep the stored embedding in sync with the refined principle. A match
        # implies the incoming embedding was usable (unpackable), so it is
        # non-None here — but guard anyway so we never blank a good vector with a
        # missing one. Leaving the old BLOB would make future matching and
        # proactive surfacing trust a vector describing the PREVIOUS text.
        if principle_embedding is not None:
            update_fields["principle_embedding"] = principle_embedding
        # A refine with no usable trigger keeps the stored one. Writing None
        # through took a live CORE row out of the trigger cache, and command
        # words kept as written name no tool, so they never replace one.
        if names_a_tool(trigger):
            update_fields["tool_trigger"] = trigger
            try:
                stored = json.loads(matched["tool_trigger"]) if matched.get("tool_trigger") else None
            except (ValueError, TypeError, RecursionError):
                stored = None  # unreadable: the new trigger replaces it, as a change
            if lesson is None and trigger_spec is not None:
                update_fields["trigger_spec"] = trigger_spec  # the commands it now names
            elif lesson is None and (trigger != stored or "Bash" in trigger):
                # An earned row's spec was written for its old trigger, or the
                # teach names Bash itself (every Bash call): drop the spec rather
                # than gate the row with commands it no longer names.
                update_fields["trigger_spec"] = None
        elif trigger:
            trigger_warnings.append(
                f"stored trigger kept: {trigger} names no tool the advisor can match"
            )
        elif tool_trigger == [] and draft == 0:
            # An explicit teach that passes an empty list means "no trigger".
            # Auto-extraction (draft=1) emitting [] keeps the stored one: an
            # LLM's empty list is how the CORE-row wipe happened.
            update_fields["tool_trigger"] = None
        # An explicit teach (draft=0) refining a matched auto-extracted row
        # (draft=1) promotes it. Recall gates on `confidence`/`success_count`,
        # NOT `draft` (matcher.find_best_match returns None at score<=0;
        # find_relevant filters confidence<min) — so flipping draft alone would
        # leave the "promoted" row at its auto-lifecycle confidence (often 0.0)
        # and thus unrecallable. Raise to the explicit-teach seed, never lowering
        # a value the auto row already earned through successful use.
        if draft == 0 and matched.get("draft") == 1:
            update_fields["draft"] = 0
            update_fields["confidence"] = max(matched.get("confidence") or 0.0, confidence)
            update_fields["success_count"] = max(
                matched.get("success_count") or 0, success_count
            )
            # Promote the auto row's DORMANT tier to the explicit-teach's
            # requested tier so recall stops applying the stricter dormant
            # threshold; never demote a row the promoter already advanced.
            if matched.get("activation_tier") == "DORMANT":
                update_fields["activation_tier"] = activation_tier
        if lesson is not None:
            update_fields.update(trigger_spec=trigger_spec, lesson=lesson, activation_tier=activation_tier)
        await procedural.update(db, matched["id"], **update_fields)
        # A lesson, or any refine of a cached earned row (its principle, steps
        # and tags are cached too; tags are its patterns), must reach the
        # advisor now, not at the promoter's next tier change.
        earned_cached = lesson is None and matched.get("activation_tier") in ("CORE", "ADVISORY")
        rebuilt = await _regenerate_cache(db) if lesson is not None or earned_cached else None
        logger.info(
            "Refined procedure %s (v%d, cosine=%.3f): %s",
            matched["id"], new_version, best_sim, task_type,
        )
        return StoreResult(
            procedure_id=matched["id"],
            action="updated",
            warnings=trigger_warnings,
            matched_similarity=best_sim,
            cache_rebuilt=rebuilt,
        )

    # No same-procedure match → this is a DISTINCT lesson (or we had no embedding
    # to prove sameness). Check for high context_tag overlap with different
    # task_types (informational warning only).
    warnings: list[str] = list(trigger_warnings)
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
        scenario=scenario,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        activation_tier=activation_tier,
        tool_trigger=trigger,
        draft=draft,
        success_count=success_count,
        confidence=confidence,
        source=source,
        principle_embedding=principle_embedding,
        trigger_spec=trigger_spec,
        lesson=lesson,
        # A held lesson is written out of every reader's sight until reviewed,
        # and it cannot change the trigger cache, so the cache is left alone.
        deprecated_reason=HELD_REASON if held else None,
    )
    rebuilt = await _regenerate_cache(db) if lesson is not None and not held else None

    if warnings:
        logger.info("Procedure %s created with %d warnings", proc_id, len(warnings))

    return StoreResult(
        procedure_id=proc_id,
        action="created",
        warnings=warnings,
        conflicting_ids=conflicting_ids,
        matched_similarity=best_sim or None,
        cache_rebuilt=rebuilt,
    )


async def _regenerate_cache(db: aiosqlite.Connection) -> bool:
    """Rebuild the trigger cache after a write it carries; a failure is logged and returned."""
    from genesis.learning.procedural.trigger_cache import regenerate

    try:
        await regenerate(db)
        return True
    except Exception:
        logger.error("Trigger cache regeneration after a procedure write failed", exc_info=True)
        return False


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
