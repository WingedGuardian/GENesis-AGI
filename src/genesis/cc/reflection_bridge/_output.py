"""Output routing and storage for reflection sessions.

Standalone async functions — called by CCReflectionBridge after CC
invocation completes. Handles deep structured routing, legacy observation
storage, light-output parsing, and topic forwarding.
"""

from __future__ import annotations

import hashlib
import html as _html
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


def _extract_fenced_json(text: str) -> str:
    """Strip a markdown ```json fence if present — CC output often wraps JSON.

    Returns the fenced payload when a fence is found, otherwise the text
    unchanged. Shared by every parse site in this module so fenced and bare
    output take the same path.
    """
    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    return json_match.group(1) if json_match else text


async def route_deep_output(
    raw_text: str,
    *,
    db,
    output_router,
    gathered_obs_ids: tuple[str, ...] = (),
    gathered_surplus_ids: tuple[str, ...] = (),
) -> dict:
    """Parse and route deep reflection output via OutputRouter.

    Returns the routing summary dict. Callers can check for
    ``parse_failed`` or ``empty_output`` keys to detect failures
    that the old code silently swallowed.

    ``gathered_obs_ids`` are observation IDs that were fed into the
    reflection context. They are marked as "influenced" only if routing
    succeeds (non-empty, non-failed output).

    ``gathered_surplus_ids`` are promoted surplus insight IDs fed into
    context. Marked as consumed after successful routing.
    """
    from genesis.reflection.output_router import parse_deep_reflection_output
    parsed = parse_deep_reflection_output(raw_text)
    return await output_router.route(
        parsed, db,
        gathered_obs_ids=gathered_obs_ids,
        gathered_surplus_ids=gathered_surplus_ids,
    )


async def store_reflection_output(depth, tick, output, *, db) -> None:
    """Legacy: store CC reflection output as a single observation."""
    from genesis.db.crud import observations

    now = datetime.now(UTC).isoformat()
    source = f"cc_reflection_{depth.value.lower()}"

    # Extract focus_area from Light output JSON for category metadata.
    focus_area = None
    if depth == Depth.LIGHT:
        try:
            parsed = json.loads(_extract_fenced_json(output.text))
            focus_area = (parsed.get("focus_area") or "").strip() or None
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # Light: skip primary observation — reflection_summary (stored below)
    # is the canonical record.  The primary is a raw CC dump that duplicates
    # summary content and has no downstream consumers.
    # Deep uses OutputRouter; Strategic keeps primary (runs rarely).
    if depth == Depth.LIGHT:
        skip_primary = True
    else:
        # Cooldown gate: skip primary observation if one of the same
        # type+source was created within the last 30 minutes.  Still process
        # sub-outputs (light escalations, deltas, surplus) and strategic focus.
        skip_primary = await observations.exists_recent_by_type(
            db, source=source, type="reflection_output", window_minutes=30,
        )
    if skip_primary:
        logger.debug("Reflection output cooldown: skipping (recent %s exists within 30m)", source)
    else:
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
                category=focus_area,
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
        await _store_reflection_summary(
            output, source=source, now=now, db=db, category=focus_area,
        )


async def _process_light_output(output, *, source: str, now: str, db) -> None:
    """Parse light reflection JSON and extract escalations, deltas, surplus."""
    from genesis.db.crud import observations

    try:
        data = json.loads(_extract_fenced_json(output.text))

        # Confidence gate — check before extracting downstream data.
        # Same rule as the deep parser: absent confidence gets a 0.5
        # sentinel, never a silent 0.7 indistinguishable from a reported one.
        cfg = load_confidence_config()
        raw_confidence = data.get("confidence")
        cc_confidence = float(raw_confidence) if raw_confidence is not None else 0.5
        if raw_confidence is None:
            logger.info("Light reflection omitted confidence — 0.5 sentinel")
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

        # Extract user_model_deltas. WS-3 gate-2 substrate: stamp each delta
        # with the reflection run's provenance aggregate — external iff any
        # external-origin session was active in the material window (run-level
        # granularity; the reflection context carries no per-session refs, so
        # per-delta tracing is not honestly buildable). Conservative
        # over-tagging at run granularity, measured in shadow.
        from genesis.db.crud import cc_sessions as cc_sessions_crud

        run_origin = await cc_sessions_crud.reflection_window_origin(db, end_iso=now)
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
                    origin_class=run_origin,
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

        # Update cognitive state from situation focus (Prong 2)
        context_update = data.get("context_update")
        focus = data.get("focus_area", "")
        if context_update and focus == "situation" and len(context_update.strip()) > 20:
            try:
                from genesis.db.crud import awareness_ticks, cognitive_state

                # Preserve deep reflection's comprehensive update for 4h
                _DEEP_PRESERVE_HOURS = 4
                hours_since_deep = 999.0
                last_deep = await awareness_ticks.last_at_depth(db, "Deep")
                if last_deep:
                    try:
                        last_deep_dt = datetime.fromisoformat(last_deep["created_at"])
                        hours_since_deep = (datetime.now(UTC) - last_deep_dt).total_seconds() / 3600
                    except (ValueError, TypeError):
                        pass  # unparseable — don't suppress (fail-open)

                if hours_since_deep >= _DEEP_PRESERVE_HOURS:
                    await cognitive_state.replace_section(
                        db,
                        section="active_context",
                        id=str(uuid.uuid4()),
                        content=context_update.strip(),
                        generated_by="light_reflection",
                        created_at=now,
                    )
                    logger.info(
                        "Light reflection updated active_context (%.1fh since deep)",
                        hours_since_deep,
                    )
                else:
                    logger.debug(
                        "Skipping light context_update (deep ran %.1fh ago < %dh preserve window)",
                        hours_since_deep, _DEEP_PRESERVE_HOURS,
                    )
            except Exception:
                logger.warning("Failed to update active_context from light reflection", exc_info=True)

    except (json.JSONDecodeError, AttributeError):
        logger.debug("Could not parse JSON from light CC output")


async def _extract_strategic_focus(output, *, now: str, db) -> None:
    """Extract focus_next_week from strategic reflection output."""
    try:
        data = json.loads(_extract_fenced_json(output.text))
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


async def _store_reflection_summary(
    output, *, source: str, now: str, db, category: str | None = None,
) -> None:
    """Store a consolidated reflection summary for embedding."""
    from genesis.db.crud import observations

    summary_parts = []
    try:
        # Fence-strip before parsing — the sibling parse sites in this module
        # already do this; without it, fenced output fell through to the raw
        # text fallback and the structured summary fields were lost.
        data = json.loads(_extract_fenced_json(output.text))
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
        # Unparseable output is NOT a reflection summary — storing the raw
        # text creates a reflection_summary observation full of tool-call
        # chatter that later re-surfaces in reflection context (the
        # phantom-claim feedback loop). Log and skip.
        logger.warning(
            "Reflection summary skipped: %s output was not parseable JSON",
            source,
        )
        return
    if summary_parts:
        # Cooldown: skip if a reflection_summary from this source exists
        # within the last 30 minutes.
        if await observations.exists_recent_by_type(
            db, source=source, type="reflection_summary", window_minutes=30,
        ):
            logger.debug("Reflection summary cooldown: skipping (recent exists within 30m)")
            return
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
                category=category,
                priority="medium",
                created_at=now,
                content_hash=summary_hash,
                skip_if_duplicate=True,
            )


def format_topic_summary(depth, output) -> str:
    """Build the Telegram topic message from PARSED reflection fields only.

    Raw ``output.text`` must never reach the topic: when the model output is
    unparseable (tool-call chatter, truncation, partial JSON) the raw text is
    internal noise, not a summary — it has leaked mid-session artifacts to
    the user verbatim. The topic stays a liveness surface: parse failure
    still produces a one-liner, never silence and never raw text.
    """
    header = f"<b>{depth.value} Reflection</b>"
    fallback = (
        f"{header}\n\nReflection completed — output was not parseable; "
        "stored for review."
    )
    try:
        data = json.loads(_extract_fenced_json(output.text))
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(data, dict):
        return fallback

    parts = [header]
    assessment = data.get("assessment") or data.get("cognitive_state_update")
    if isinstance(assessment, str) and assessment.strip():
        parts.append(_html.escape(assessment[:1500]))
    obs_lines = []
    for obs in (data.get("observations") or [])[:3]:
        text = obs if isinstance(obs, str) else ""
        if text.strip():
            obs_lines.append(f"• {_html.escape(text[:300])}")
    if obs_lines:
        parts.append("\n".join(obs_lines))
    focus = data.get("focus_next_week") or data.get("focus_next")
    if isinstance(focus, str) and focus.strip():
        parts.append(f"<b>Focus:</b> {_html.escape(focus[:500])}")

    if len(parts) == 1:
        return (
            f"{header}\n\nReflection completed — no summary fields in "
            "output; stored for review."
        )
    return "\n\n".join(parts)


async def send_to_topic(session_id: str, depth, text: str, *, topic_manager) -> None:
    """Send pre-built topic text (see ``format_topic_summary``) to the
    depth-specific topic. Never receives raw model output."""
    if not topic_manager:
        logger.warning(
            "send_to_topic: topic_manager not set — skipping %s topic send",
            depth.value,
        )
        return
    category = f"reflection_{depth.value.lower()}"
    try:
        await topic_manager.send_to_category(category, text)
    except Exception:
        logger.warning("Failed to send reflection to topic %s", category, exc_info=True)
