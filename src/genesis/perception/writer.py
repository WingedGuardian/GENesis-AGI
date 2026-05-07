"""ResultWriter — stores reflection outputs as observations and user model deltas."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.db.crud import observations
from genesis.db.crud import surplus as surplus_crud
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.perception.confidence import load_config, should_gate
from genesis.perception.types import MIN_DELTA_CONFIDENCE, LightOutput, MicroOutput

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Signals that track user activity/outcomes (vs Genesis infrastructure).
# Used to determine relevance tagging on micro reflections.
_USER_FACING_SIGNALS = frozenset({
    "conversations_since_reflection",
    "task_completion_quality",
    "recon_findings_pending",
    "stale_pending_items",
    "user_goal_staleness",
    "user_session_pattern",
})

# Light reflection focus_area → relevance mapping
_LIGHT_FOCUS_RELEVANCE: dict[str, str] = {
    "user_impact": "user",
    "situation": "both",
    "anomaly": "genesis",
}

# Low-information patterns: observations matching these contribute no value.
# Applied after salience + cooldown gates in _write_micro().
_LOW_INFO_PATTERNS = re.compile(
    r"no significant (changes?|activity|events?|issues?)"
    r"|system (is )?(operating|running) (normally|as expected)"
    r"|nothing (notable|significant|unusual|new)"
    r"|all (systems?|metrics?) (are )?(within|normal|stable|healthy)"
    r"|no action (required|needed|necessary)",
    re.IGNORECASE,
)


class ResultWriter:
    """Stores reflection outputs to the database and emits events.

    Micro: creates observation, tags anomalies.
    Light: creates observation + stores user model deltas as observations
           (Phase 5 synthesizes these into the user model cache).
    """

    def __init__(
        self,
        *,
        event_bus: GenesisEventBus | None = None,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._memory_store = memory_store

    @staticmethod
    def _content_hash(content: str) -> str:
        """SHA-256 hash for observation dedup."""
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _relevance_from_signals(tick: TickResult) -> str:
        """Determine relevance tag from tick signals: 'user', 'genesis', or 'both'."""
        has_user = any(s.name in _USER_FACING_SIGNALS for s in tick.signals)
        has_genesis = any(s.name not in _USER_FACING_SIGNALS for s in tick.signals)
        if has_user and has_genesis:
            return "both"
        if has_user:
            return "user"
        return "genesis"

    async def write(
        self,
        output: MicroOutput | LightOutput,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
    ) -> bool:
        """Write reflection output.  Returns True if stored, False if gated."""
        stored = True
        if isinstance(output, MicroOutput):
            stored = await self._write_micro(output, tick, db)
        elif isinstance(output, LightOutput):
            await self._write_light(output, tick, db)

        if self._event_bus:
            await self._event_bus.emit(
                Subsystem.PERCEPTION,
                Severity.INFO,
                "reflection.completed",
                f"{depth.value} reflection completed",
                depth=depth.value,
                tick_id=tick.tick_id,
            )
        return stored

    async def _write_micro(
        self,
        output: MicroOutput,
        tick: TickResult,
        db: aiosqlite.Connection,
    ) -> bool:
        """Returns True if stored, False if gated."""
        # Salience gate: skip low-salience non-anomaly observations
        if output.salience < 0.45 and not output.anomaly:
            logger.debug("Micro observation below salience threshold (%.2f), skipping", output.salience)
            return False

        # Cooldown gate: skip if a micro_reflection was created within the last
        # 20 minutes. Anomalies bypass — same pattern as light reflections (30 min).
        if not output.anomaly and await observations.exists_recent_by_type(
            db, source="reflection", type="micro_reflection", window_minutes=20,
        ):
            logger.debug("Micro reflection cooldown: skipping (recent exists within 20m)")
            return False

        # Low-information gate: skip observations with no actionable content.
        # The normalization fix (observation_writer) is the primary dedup fix;
        # this catches genuinely empty observations that pass salience + cooldown.
        # Anomalies bypass — same as salience and cooldown gates.
        if not output.anomaly and _LOW_INFO_PATTERNS.search(output.summary):
            logger.debug("Micro-reflection skipped: low-information content")
            return False

        content = json.dumps({
            "tags": output.tags,
            "salience": output.salience,
            "anomaly": output.anomaly,
            "summary": output.summary,
            "signals_examined": output.signals_examined,
        }, sort_keys=True)
        base_category = "anomaly" if output.anomaly else "routine"
        relevance = self._relevance_from_signals(tick)
        category = f"{base_category}:{relevance}"

        # Structural dedup: hash on tags + salience band + anomaly flag +
        # signal names.  LLM-generated summaries vary too much per tick to
        # be useful as dedup keys — even after number normalization, the
        # sentence structure changes and the hash never collides.
        signal_names = ",".join(sorted(s.name for s in tick.signals))
        salience_band = round(output.salience, 1)
        norm_key = f"micro:{','.join(sorted(output.tags))}|{salience_band}|{output.anomaly}|{signal_names}"
        chash = self._content_hash(norm_key)

        if await observations.exists_by_hash(db, source="reflection", content_hash=chash, unresolved_only=True):
            logger.debug("Micro observation dedup: skipping near-duplicate (hash=%s)", chash[:12])
            return False

        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="reflection",
            type="micro_reflection",
            category=category,
            content=content,
            priority="high" if output.anomaly else "low",
            created_at=tick.timestamp,
            content_hash=chash,
        )

        if self._memory_store:
            await self._memory_store.store(
                output.summary,
                "reflection",
                memory_type="episodic",
                tags=output.tags,
                confidence=output.salience,
                source_pipeline="reflection",
            )
        return True

    async def _write_light(
        self,
        output: LightOutput,
        tick: TickResult,
        db: aiosqlite.Connection,
    ) -> None:
        # Confidence gate: skip low-confidence light observations
        cfg = load_config()
        gated, gate_msg = should_gate(output.confidence, cfg.observation_write)
        if gate_msg:
            logger.info("Light observation confidence gate: %s", gate_msg)
        if gated:
            return

        # Cooldown gate: skip if a light_reflection from this source was
        # created within the last 30 minutes.  Prevents near-duplicate
        # observations where the LLM generates different wording for the
        # same system state.
        if await observations.exists_recent_by_type(
            db, source="reflection", type="light_reflection", window_minutes=30,
        ):
            logger.debug("Light reflection cooldown: skipping (recent exists within 30m)")
            return

        content = json.dumps({
            "assessment": output.assessment,
            "patterns": output.patterns,
            "recommendations": output.recommendations,
            "confidence": output.confidence,
            "focus_area": output.focus_area,
        }, sort_keys=True)
        chash = self._content_hash(content)

        light_relevance = _LIGHT_FOCUS_RELEVANCE.get(output.focus_area, "both")
        light_category = f"{output.focus_area}:{light_relevance}"

        if not await observations.exists_by_hash(db, source="reflection", content_hash=chash, unresolved_only=True):
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="reflection",
                type="light_reflection",
                category=light_category,
                content=content,
                priority="medium",
                created_at=tick.timestamp,
                content_hash=chash,
            )
        else:
            logger.debug("Light observation dedup: skipping duplicate (hash=%s)", chash[:12])

        # Escalation: if Light requests deep escalation, create a pending
        # observation that the awareness loop will pick up to force Deep depth.
        # Mirrors the CC bridge path (cc/reflection_bridge/_output.py).
        if output.escalate_to_deep:
            esc_reason = output.escalation_reason or output.assessment[:200]
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
                    except (ValueError, TypeError):
                        pass

            if not suppress:
                esc_hash = self._content_hash(esc_reason)
                if not await observations.exists_by_hash(
                    db, source="reflection", content_hash=esc_hash, unresolved_only=True,
                ):
                    await observations.create(
                        db,
                        id=str(uuid.uuid4()),
                        source="reflection",
                        type="light_escalation_pending",
                        content=esc_reason,
                        priority="high",
                        created_at=tick.timestamp,
                        content_hash=esc_hash,
                    )
                    logger.info("Light escalation pending created: %s", esc_reason[:80])

        # Update cognitive state from situation focus (Prong 2)
        # Mirrors the CC bridge path (cc/reflection_bridge/_output.py).
        if (
            output.context_update
            and output.focus_area == "situation"
            and len(output.context_update.strip()) > 20
        ):
            try:
                from genesis.db.crud import awareness_ticks, cognitive_state

                _DEEP_PRESERVE_HOURS = 4
                hours_since_deep = 999.0
                last_deep = await awareness_ticks.last_at_depth(db, "Deep")
                if last_deep:
                    try:
                        last_deep_dt = datetime.fromisoformat(last_deep["created_at"])
                        hours_since_deep = (datetime.now(UTC) - last_deep_dt).total_seconds() / 3600
                    except (ValueError, TypeError):
                        pass
                if hours_since_deep >= _DEEP_PRESERVE_HOURS:
                    await cognitive_state.replace_section(
                        db,
                        section="active_context",
                        id=str(uuid.uuid4()),
                        content=output.context_update.strip(),
                        generated_by="light_reflection",
                        created_at=tick.timestamp,
                    )
                    logger.info(
                        "Light reflection updated active_context (%.1fh since deep)",
                        hours_since_deep,
                    )
            except Exception:
                logger.warning("Failed to update active_context from light reflection", exc_info=True)

        if self._memory_store:
            await self._memory_store.store(
                output.assessment,
                "reflection",
                memory_type="episodic",
                tags=output.patterns if output.patterns else [],
                confidence=output.confidence,
                source_pipeline="reflection",
            )

        # Store surplus candidates directly in surplus_insights table.
        # Previously wrote to observations (type="surplus_candidate") but nothing
        # consumed that type. surplus_insights is what Deep's context_gatherer reads.
        for candidate in output.surplus_candidates:
            stripped = candidate.strip() if candidate else ""
            if not stripped:
                continue
            sc_id = hashlib.sha256(stripped.encode()).hexdigest()[:16]
            try:
                await surplus_crud.upsert(
                    db,
                    id=f"light-{sc_id}",
                    content=stripped,
                    source_task_type="light_reflection_candidate",
                    generating_model="perception_engine",
                    drive_alignment="curiosity",
                    confidence=output.confidence,
                    created_at=tick.timestamp,
                    ttl=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
                )
            except Exception:
                logger.error("Failed to upsert surplus candidate", exc_info=True)

        # Store user model deltas as observations for Phase 5 synthesis
        # GROUNDWORK(user-model-synthesis): Phase 5 reads these and updates user_model_cache
        for delta in output.user_model_updates:
            # Confidence gate: LLM clusters at 0.80/0.85/0.90/0.95; gate at 0.90
            # filters ~60% of overconfident filler deltas.
            if delta.confidence < MIN_DELTA_CONFIDENCE:
                logger.debug(
                    "Skipping delta below confidence gate: %s (%.2f < %.2f)",
                    delta.field, delta.confidence, MIN_DELTA_CONFIDENCE,
                )
                continue
            # Dedup on (field, value) only — catches near-duplicates where
            # evidence wording or confidence differs across reflection cycles.
            dedup_key = json.dumps({"field": delta.field, "value": delta.value}, sort_keys=True)
            delta_hash = self._content_hash(dedup_key)
            if await observations.exists_by_hash(db, source="reflection", content_hash=delta_hash):
                logger.debug("User model delta dedup: skipping duplicate (field=%s)", delta.field)
                continue
            delta_content = json.dumps({
                "field": delta.field,
                "value": delta.value,
                "evidence": delta.evidence,
                "confidence": delta.confidence,
            }, sort_keys=True)
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="reflection",
                type="user_model_delta",
                category="user_model_delta",
                content=delta_content,
                priority="medium",
                created_at=tick.timestamp,
                content_hash=delta_hash,
            )
