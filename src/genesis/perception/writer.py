"""ResultWriter — stores reflection outputs as observations and user model deltas."""

from __future__ import annotations

import hashlib
import json
import logging
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

    async def write(
        self,
        output: MicroOutput | LightOutput,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
    ) -> None:
        if isinstance(output, MicroOutput):
            await self._write_micro(output, tick, db)
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

    async def _write_micro(
        self,
        output: MicroOutput,
        tick: TickResult,
        db: aiosqlite.Connection,
    ) -> None:
        # Salience gate: skip low-salience non-anomaly observations
        if output.salience < 0.45 and not output.anomaly:
            logger.debug("Micro observation below salience threshold (%.2f), skipping", output.salience)
            return

        content = json.dumps({
            "tags": output.tags,
            "salience": output.salience,
            "anomaly": output.anomaly,
            "summary": output.summary,
            "signals_examined": output.signals_examined,
        }, sort_keys=True)
        category = "anomaly" if output.anomaly else "routine"
        chash = self._content_hash(content)

        if await observations.exists_by_hash(db, source="reflection", content_hash=chash, unresolved_only=True):
            logger.debug("Micro observation dedup: skipping duplicate (hash=%s)", chash[:12])
            return

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

        content = json.dumps({
            "assessment": output.assessment,
            "patterns": output.patterns,
            "recommendations": output.recommendations,
            "confidence": output.confidence,
            "focus_area": output.focus_area,
        }, sort_keys=True)
        chash = self._content_hash(content)

        if not await observations.exists_by_hash(db, source="reflection", content_hash=chash, unresolved_only=True):
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="reflection",
                type="light_reflection",
                content=content,
                priority="medium",
                created_at=tick.timestamp,
                content_hash=chash,
            )
        else:
            logger.debug("Light observation dedup: skipping duplicate (hash=%s)", chash[:12])

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
                content=delta_content,
                priority="medium",
                created_at=tick.timestamp,
                content_hash=delta_hash,
            )
