"""Output routing and storage for reflection sessions.

Standalone async functions — called by CCReflectionBridge after CC
invocation completes. Handles deep structured routing, legacy observation
storage, light-output parsing, and topic forwarding.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from genesis.awareness.types import Depth
from genesis.perception.confidence import load_config as load_confidence_config
from genesis.perception.confidence import should_gate
from genesis.perception.types import MIN_DELTA_CONFIDENCE

logger = logging.getLogger(__name__)


async def route_deep_output(
    raw_text: str,
    *,
    db,
    output_router,
    gathered_obs_ids: tuple[str, ...] = (),
) -> dict:
    """Parse and route deep reflection output via OutputRouter.

    Returns the routing summary dict. Callers can check for
    ``parse_failed`` or ``empty_output`` keys to detect failures
    that the old code silently swallowed.

    ``gathered_obs_ids`` are observation IDs that were fed into the
    reflection context. They are marked as "influenced" only if routing
    succeeds (non-empty, non-failed output).
    """
    from genesis.reflection.output_router import parse_deep_reflection_output
    parsed = parse_deep_reflection_output(raw_text)
    return await output_router.route(parsed, db, gathered_obs_ids=gathered_obs_ids)


async def store_reflection_output(depth, tick, output, *, db) -> None:
    """Legacy: store CC reflection output as a single observation."""
    from genesis.db.crud import observations

    now = datetime.now(UTC).isoformat()
    source = f"cc_reflection_{depth.value.lower()}"

    # Primary reflection output (with dedup)
    content = json.dumps({
        "tick_id": tick.tick_id,
        "depth": depth.value,
        "cc_output": output.text[:2000],
        "model_used": output.model_used,
        "cost_usd": output.cost_usd,
        "input_tokens": output.input_tokens,
        "output_tokens": output.output_tokens,
    }, sort_keys=True)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if not await observations.exists_by_hash(
        db, source=source, content_hash=content_hash, unresolved_only=True,
    ):
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source=source,
            type="reflection_output",
            content=content,
            priority="high" if depth == Depth.STRATEGIC else "medium",
            created_at=now,
            content_hash=content_hash,
            skip_if_duplicate=True,
        )

    # Light: parse JSON for escalation, user_model_deltas, surplus_candidates
    if depth == Depth.LIGHT:
        await _process_light_output(output, source=source, now=now, db=db)

    # Extract and store focus_next_week from strategic output
    if depth == Depth.STRATEGIC:
        await _extract_strategic_focus(output, now=now, db=db)

    # Store a consolidated reflection summary for embedding.
    # Deep reflections get their summary via OutputRouter.route() — skip here.
    if depth != Depth.DEEP:
        await _store_reflection_summary(output, source=source, now=now, db=db)


async def _process_light_output(output, *, source: str, now: str, db) -> None:
    """Parse light reflection JSON and extract escalations, deltas, surplus."""
    from genesis.db.crud import observations

    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", output.text, re.DOTALL)
        raw_json = json_match.group(1) if json_match else output.text
        data = json.loads(raw_json)

        # Confidence gate — check before extracting downstream data
        cfg = load_confidence_config()
        cc_confidence = float(data.get("confidence", 0.7))
        gated, gate_msg = should_gate(cc_confidence, cfg.observation_write)
        if gate_msg:
            logger.info("CC bridge light confidence gate: %s", gate_msg)

        # Escalation check — runs BEFORE confidence gate (safety mechanism)
        if data.get("escalate_to_deep"):
            esc_reason = data.get("escalation_reason", "light CC reflection requested escalation")
            logger.info("Light CC reflection requested deep escalation: %s", esc_reason)

            # Mechanical suppression: skip non-emergency escalations if deep
            # ran recently.  Deep reflections review ALL signals, so re-escalating
            # persistent issues within this window is redundant noise.
            _SUPPRESSION_HOURS = 12
            _EMERGENCY_KEYWORDS = (
                "critical_failure", "data_loss", "security_breach",
                "all providers", "container memory critical",
            )
            esc_lower = esc_reason.lower()
            is_emergency = any(kw in esc_lower for kw in _EMERGENCY_KEYWORDS)

            suppress = False
            if not is_emergency:
                from genesis.db.crud import awareness_ticks
                last_deep = await awareness_ticks.last_at_depth(db, "Deep")
                if last_deep:
                    try:
                        last_deep_dt = datetime.fromisoformat(last_deep["created_at"])
                        hours_since = (datetime.now(UTC) - last_deep_dt).total_seconds() / 3600
                        if hours_since < _SUPPRESSION_HOURS:
                            suppress = True
                            logger.info(
                                "Suppressing escalation (deep ran %.1fh ago < %dh window): %s",
                                hours_since, _SUPPRESSION_HOURS, esc_reason[:80],
                            )
                    except (ValueError, TypeError):
                        pass  # unparseable timestamp — don't suppress

            if not suppress:
                esc_hash = hashlib.sha256(esc_reason.encode()).hexdigest()
                if not await observations.exists_by_hash(
                    db, source="awareness_loop", content_hash=esc_hash, unresolved_only=True,
                ):
                    await observations.create(
                        db,
                        id=str(uuid.uuid4()),
                        source="awareness_loop",
                        type="light_escalation_pending",
                        content=esc_reason,
                        priority="high",
                        created_at=now,
                        content_hash=esc_hash,
                    )

        # Confidence gate — skip delta/surplus extraction if below threshold
        if gated:
            return

        # Extract user_model_deltas
        for delta in data.get("user_model_updates", []):
            try:
                conf = float(delta.get("confidence", 0))
                if conf < MIN_DELTA_CONFIDENCE:
                    continue
                field_val = str(delta.get("field", "")), str(delta.get("value", ""))
                if not all(field_val):
                    continue
                dedup_key = json.dumps({"field": field_val[0], "value": field_val[1]}, sort_keys=True)
                delta_hash = hashlib.sha256(dedup_key.encode()).hexdigest()
                if await observations.exists_by_hash(db, source="reflection", content_hash=delta_hash):
                    continue
                delta_content = json.dumps({
                    "field": field_val[0], "value": field_val[1],
                    "evidence": str(delta.get("evidence", "")),
                    "confidence": conf,
                }, sort_keys=True)
                await observations.create(
                    db,
                    id=str(uuid.uuid4()),
                    source="reflection",
                    type="user_model_delta",
                    content=delta_content,
                    priority="medium",
                    created_at=now,
                    content_hash=delta_hash,
                )
            except (TypeError, ValueError, AttributeError):
                logger.debug("Skipping malformed user_model_delta from CC output")

        # Extract surplus_candidates → surplus_insights table
        from genesis.db.crud import surplus as surplus_crud

        for candidate in data.get("surplus_candidates", []):
            stripped = str(candidate).strip() if candidate else ""
            if not stripped:
                continue
            try:
                sc_id = hashlib.sha256(stripped.encode()).hexdigest()[:16]
                await surplus_crud.upsert(
                    db,
                    id=f"light-{sc_id}",
                    content=stripped,
                    source_task_type="light_reflection_candidate",
                    generating_model="cc_bridge",
                    drive_alignment="curiosity",
                    confidence=float(data.get("confidence", 0.5)),
                    created_at=now,
                    ttl=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
                )
            except Exception:
                logger.error("Failed to upsert surplus candidate from CC output", exc_info=True)

    except (json.JSONDecodeError, AttributeError):
        logger.debug("Could not parse JSON from light CC output")


async def _extract_strategic_focus(output, *, now: str, db) -> None:
    """Extract focus_next_week from strategic reflection output."""
    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", output.text, re.DOTALL)
        raw_json = json_match.group(1) if json_match else output.text
        data = json.loads(raw_json)
        focus_week = data.get("focus_next_week", "")
        if focus_week:
            from genesis.db.crud import cognitive_state
            await cognitive_state.replace_section(
                db,
                section="pending_actions",
                id=str(uuid.uuid4()),
                content=f"## Strategic Focus (This Week)\n{focus_week}",
                generated_by="strategic_reflection",
                created_at=now,
            )
            logger.info("Strategic focus_next_week stored: %s", focus_week[:100])
    except (json.JSONDecodeError, AttributeError):
        logger.debug("Could not parse focus_next_week from strategic output")


async def _store_reflection_summary(output, *, source: str, now: str, db) -> None:
    """Store a consolidated reflection summary for embedding."""
    from genesis.db.crud import observations

    summary_parts = []
    try:
        data = json.loads(output.text)
        if isinstance(data, dict):
            if data.get("assessment"):
                summary_parts.append(str(data["assessment"])[:1500])
            if data.get("focus_next") or data.get("focus_next_week"):
                focus = data.get("focus_next_week") or data.get("focus_next", "")
                summary_parts.append(f"Focus: {focus}")
            for obs in (data.get("observations") or [])[:3]:
                if obs:
                    summary_parts.append(str(obs))
    except (json.JSONDecodeError, TypeError):
        if output.text.strip():
            summary_parts.append(output.text[:2000])
    if summary_parts:
        summary_text = "\n\n".join(summary_parts)[:4000]
        summary_hash = hashlib.sha256(summary_text.encode()).hexdigest()
        if not await observations.exists_by_hash(
            db, source=source, content_hash=summary_hash, unresolved_only=True,
        ):
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source=source,
                type="reflection_summary",
                content=summary_text,
                priority="medium",
                created_at=now,
                content_hash=summary_hash,
                skip_if_duplicate=True,
            )


async def send_to_topic(session_id: str, depth, output, *, topic_manager) -> None:
    """Send a reflection summary to the depth-specific topic."""
    if not topic_manager:
        logger.warning(
            "send_to_topic: topic_manager not set — skipping %s topic send",
            depth.value,
        )
        return
    category = f"reflection_{depth.value.lower()}"
    summary = (
        f"<b>{depth.value} Reflection</b>\n\n"
        f"{output.text[:3000]}"
    )
    try:
        await topic_manager.send_to_category(category, summary)
    except Exception:
        logger.warning("Failed to send reflection to topic %s", category, exc_info=True)
