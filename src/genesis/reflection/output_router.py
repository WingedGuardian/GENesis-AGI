"""Output routing — parse CC reflection output and route to appropriate stores."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.db.crud import cognitive_state, observations, procedural
from genesis.perception.confidence import load_config as load_confidence_config
from genesis.perception.confidence import should_gate
from genesis.reflection.types import (
    DeepReflectionOutput,
    DimensionScore,
    MemoryOperation,
    QualityCalibrationOutput,
    SurplusTaskRequest,
    UserQuestion,
    WeeklyAssessmentOutput,
)

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.events import GenesisEventBus

logger = logging.getLogger(__name__)


def parse_deep_reflection_output(raw_json: str) -> DeepReflectionOutput:
    """Parse raw CC output JSON into a DeepReflectionOutput.

    Tolerant of missing fields — returns defaults for anything absent.
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        # Try to extract JSON from markdown code block
        if "```" in str(raw_json):
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", str(raw_json), re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to parse extracted JSON from CC output")
                    return DeepReflectionOutput(parse_failed=True)
            else:
                return DeepReflectionOutput(parse_failed=True)
        else:
            logger.warning("Failed to parse CC reflection output as JSON")
            return DeepReflectionOutput(parse_failed=True)

    if not isinstance(data, dict):
        return DeepReflectionOutput(parse_failed=True)

    # Parse memory operations
    mem_ops = []
    for op in data.get("memory_operations", []):
        if isinstance(op, dict):
            mem_ops.append(MemoryOperation(
                operation=op.get("operation", "unknown"),
                target_ids=op.get("target_ids", []),
                reason=op.get("reason", ""),
                merged_content=op.get("merged_content"),
            ))

    # Parse surplus_task_requests
    task_requests = []
    for req in data.get("surplus_task_requests", []):
        if isinstance(req, dict):
            task_requests.append(SurplusTaskRequest(
                task_type=str(req.get("task_type", "")),
                reason=str(req.get("reason", "")),
                priority=float(req.get("priority", 0.5)),
                drive_alignment=str(req.get("drive_alignment", "competence")),
                payload=req.get("payload"),
            ))

    # Parse user_question
    user_question = None
    uq_data = data.get("user_question")
    if isinstance(uq_data, dict) and uq_data.get("text"):
        user_question = UserQuestion(
            text=str(uq_data["text"]),
            context=str(uq_data.get("context", "")),
            options=[str(o) for o in uq_data.get("options", [])],
        )

    return DeepReflectionOutput(
        observations=data.get("observations", []),
        cognitive_state_update=data.get("cognitive_state_update"),
        memory_operations=mem_ops,
        surplus_task_requests=task_requests,
        user_question=user_question,
        skill_triggers=data.get("skill_triggers", []),
        procedure_quarantines=data.get("procedure_quarantines", []),
        contradictions=data.get("contradictions", []),
        learnings=data.get("learnings", []),
        focus_next=data.get("focus_next", ""),
        confidence=float(data.get("confidence", 0.7)),
        separability=float(data["separability"]) if data.get("separability") is not None else None,
        alternative_assessment=data.get("alternative_assessment"),
    )


def parse_weekly_assessment_output(raw_json: str) -> WeeklyAssessmentOutput:
    """Parse raw CC output into WeeklyAssessmentOutput."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", str(raw_json), re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                return WeeklyAssessmentOutput(parse_failed=True)
        else:
            return WeeklyAssessmentOutput(parse_failed=True)

    if not isinstance(data, dict):
        return WeeklyAssessmentOutput(parse_failed=True)

    dims = []
    for d in data.get("dimensions", []):
        if isinstance(d, dict):
            try:
                dim_enum = d.get("dimension", "reflection_quality")
                dims.append(DimensionScore(
                    dimension=dim_enum,
                    score=float(d.get("score", 0.0)),
                    evidence=d.get("evidence", ""),
                    data_available=d.get("data_available", True),
                ))
            except (ValueError, KeyError):
                continue

    return WeeklyAssessmentOutput(
        dimensions=dims,
        overall_score=float(data.get("overall_score", 0.0)),
        observations=data.get("observations", []),
        recommendations=data.get("recommendations", []),
    )


def parse_quality_calibration_output(raw_json: str) -> QualityCalibrationOutput:
    """Parse raw CC output into QualityCalibrationOutput."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", str(raw_json), re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                return QualityCalibrationOutput(parse_failed=True)
        else:
            return QualityCalibrationOutput(parse_failed=True)

    if not isinstance(data, dict):
        return QualityCalibrationOutput(parse_failed=True)

    return QualityCalibrationOutput(
        drift_detected=bool(data.get("drift_detected", False)),
        quarantine_candidates=data.get("quarantine_candidates", []),
        observations=data.get("observations", []),
    )


class OutputRouter:
    """Routes parsed deep reflection output to the appropriate data stores."""

    def __init__(
        self,
        *,
        event_bus: GenesisEventBus | None = None,
        observation_writer=None,
        reflections_dir: Path | None = None,
        surplus_queue=None,
        question_gate=None,
        outreach_pipeline=None,
    ):
        self._event_bus = event_bus
        self._observation_writer = observation_writer
        self._reflections_dir = reflections_dir or (
            Path.home() / "genesis" / "docs" / "reflections"
        )
        self._surplus_queue = surplus_queue
        self._question_gate = question_gate
        self._outreach_pipeline = outreach_pipeline
        # Novelty tracking: what % of observations are genuinely new vs deduped
        self._obs_attempted = 0
        self._obs_deduped = 0

    def set_outreach_pipeline(self, pipeline) -> None:
        """Late-bind outreach pipeline (outreach inits after reflection)."""
        self._outreach_pipeline = pipeline

    async def route(
        self, output: DeepReflectionOutput, db: aiosqlite.Connection,
        *, gathered_obs_ids: tuple[str, ...] = (),
        gathered_surplus_ids: tuple[str, ...] = (),
    ) -> dict:
        """Route all components of a deep reflection output to their stores.

        Returns a summary dict of what was routed.

        ``gathered_obs_ids``: observation IDs fed into the reflection context.
        Marked as "influenced" only when output is substantive.
        ``gathered_surplus_ids``: promoted surplus insight IDs fed into context.
        Marked as consumed after successful routing.
        """
        summary: dict = {
            "observations_written": 0,
            "cognitive_state_updated": False,
            # surplus_decisions removed — intake pipeline handles triage now
            "memory_operations": 0,
            "quarantines": 0,
            "contradictions": 0,
            "surplus_tasks_enqueued": 0,
            "question_surfaced": False,
        }

        # Quality gate — reject parse failures and all-zero output
        if output.parse_failed:
            logger.error("Deep reflection parse failed — output is empty defaults, not real data")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    "deep_reflection.parse_failed",
                    "Deep reflection output could not be parsed — recorded as failure",
                )
            summary["parse_failed"] = True
            return summary

        _has_substance = bool(
            output.observations
            or output.cognitive_state_update
            or output.memory_operations
            or output.learnings
            or output.focus_next
            or output.skill_triggers
            or output.contradictions
            or output.surplus_task_requests
            or output.user_question
            or output.procedure_quarantines
        )
        if not _has_substance:
            logger.error(
                "Deep reflection produced zero output across all fields — treating as failure"
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    "deep_reflection.empty_output",
                    "Deep reflection returned valid JSON but every field is empty — recorded as failure",
                )
            summary["empty_output"] = True
            return summary

        # Confidence gate — quarantine entire output if below threshold
        cfg = load_confidence_config()
        gated, gate_msg = should_gate(output.confidence, cfg.deep_reflection)
        if gate_msg:
            logger.warning("Deep reflection confidence gate: %s (conf=%.2f)", gate_msg, output.confidence)
        if gated:
            await self._write_observation(
                db, source="deep_reflection", type="quarantined_reflection",
                content=f"Low-confidence reflection quarantined (conf={output.confidence:.2f}): "
                + "; ".join(output.observations[:2]),
                priority="low",
            )
            summary["quarantined"] = True
            return summary

        # Separability advisory (log only, doesn't gate)
        if (
            output.separability is not None
            and output.separability < cfg.deep_reflection.min_separability
        ):
            logger.warning(
                "Low separability (%.2f) in deep reflection — alternative: %s",
                output.separability,
                (output.alternative_assessment or "not provided")[:100],
            )

        # 1. Observations (enforce max 5 per REFLECTION_DEEP.md prompt contract)
        _MAX_REFLECTION_OBS = 5
        for obs_text in output.observations[:_MAX_REFLECTION_OBS]:
            if obs_text:
                await self._write_observation(
                    db, source="deep_reflection", type="reflection_observation",
                    content=obs_text, priority="medium",
                )
                summary["observations_written"] += 1
        if len(output.observations) > _MAX_REFLECTION_OBS:
            logger.info(
                "Capped reflection_observation at %d (had %d)",
                _MAX_REFLECTION_OBS, len(output.observations),
            )

        # 2. Learnings (also observations, tagged differently)
        for learning in output.learnings:
            if learning:
                await self._write_observation(
                    db, source="deep_reflection", type="learning",
                    content=learning, priority="medium",
                )
                summary["observations_written"] += 1

        # 3. Cognitive state update
        now = datetime.now(UTC).isoformat()
        if output.cognitive_state_update:
            await cognitive_state.replace_section(
                db,
                section="active_context",
                id=str(uuid.uuid4()),
                content=output.cognitive_state_update,
                generated_by="deep_reflection",
                created_at=now,
            )
            summary["cognitive_state_updated"] = True
            # Clear session patches — fresh narrative supersedes them
            cognitive_state.clear_session_patches()

        # 3b. Focus directive for next cycle
        # NOTE: replace_section is destructive — overwrites all state_flags content.
        # This is a known limitation of the cognitive_state schema (post-phase-9 fix).
        if output.focus_next:
            focus_content = f"## Deep Reflection Focus Directive\n{output.focus_next}"
            await cognitive_state.replace_section(
                db,
                section="state_flags",
                id=str(uuid.uuid4()),
                content=focus_content,
                generated_by="deep_reflection",
                created_at=now,
            )
            summary["focus_next_stored"] = True

        # 4. Surplus task requests (deep reflection dispatching new tasks)
        if self._surplus_queue and output.surplus_task_requests:
            from genesis.surplus.types import ComputeTier, TaskType
            for req in output.surplus_task_requests:
                try:
                    # Validate task_type against enum
                    task_type = TaskType(req.task_type)
                except ValueError:
                    logger.warning("Invalid surplus task_type from reflection: %s", req.task_type)
                    continue
                try:
                    task_id = await self._surplus_queue.enqueue(
                        task_type=task_type,
                        compute_tier=ComputeTier.FREE_API,
                        priority=req.priority,
                        drive_alignment=req.drive_alignment,
                        payload=req.payload,
                    )
                    logger.info("Enqueued surplus task %s (type=%s) from deep reflection", task_id, req.task_type)
                    summary["surplus_tasks_enqueued"] += 1
                except Exception:
                    logger.error("Failed to enqueue surplus task type=%s", req.task_type, exc_info=True)

        # 4c. User question (max 1 pending at any time)
        if output.user_question and self._question_gate and self._outreach_pipeline:
            try:
                can_ask = await self._question_gate.can_ask(db)
                if can_ask:
                    obs_id = await self._question_gate.record_question(
                        db,
                        question_text=output.user_question.text,
                        context=output.user_question.context,
                    )
                    # Format question for outreach
                    options_text = ""
                    if output.user_question.options:
                        options_text = "\n\nOptions:\n" + "\n".join(
                            f"  {i + 1}. {opt}"
                            for i, opt in enumerate(output.user_question.options)
                        )
                    from genesis.outreach.types import OutreachCategory, OutreachRequest

                    await self._outreach_pipeline.submit(OutreachRequest(
                        category=OutreachCategory.SURPLUS,
                        topic=output.user_question.text[:100],
                        context=output.user_question.context + options_text,
                        salience_score=0.8,
                        signal_type="reflection_question",
                        source_id=obs_id,
                    ))
                    summary["question_surfaced"] = True
                    logger.info(
                        "Surfaced user question: %s",
                        output.user_question.text[:100],
                    )
                else:
                    logger.info(
                        "Question gate: pending question exists, skipping new question",
                    )
            except Exception:
                logger.error("Failed to surface user question", exc_info=True)

        # 5. Memory operations — execute consolidation
        _MAX_OPS_PER_CYCLE = 50
        all_influenced_ids: list[str] = []
        ops_executed = 0
        for op in output.memory_operations:
            if ops_executed >= _MAX_OPS_PER_CYCLE:
                logger.info(
                    "Memory consolidation capped at %d operations per cycle",
                    _MAX_OPS_PER_CYCLE,
                )
                break

            executed = await self._execute_memory_operation(db, op)
            if executed:
                ops_executed += 1

            summary["memory_operations"] += 1
            # Track that these observations influenced a reflection decision
            if op.target_ids:
                all_influenced_ids.extend(op.target_ids)

        # Mark all referenced observations as having influenced an action
        if all_influenced_ids:
            try:
                unique_ids = list(set(all_influenced_ids))
                await observations.mark_influenced_batch(db, unique_ids)
            except Exception:
                logger.warning(
                    "Failed to mark influenced observations", exc_info=True,
                )

        # 6. Procedure quarantines
        for q in output.procedure_quarantines:
            proc_id = q.get("procedure_id", "")
            reason = q.get("reason", "Deep reflection identified declining effectiveness")
            await self._write_observation(
                db, source="deep_reflection", type="quarantine_recommendation",
                content=json.dumps({"procedure_id": proc_id, "reason": reason}),
                priority="high",
            )
            if proc_id:
                quarantined = await procedural.quarantine(db, proc_id)
                if quarantined:
                    logger.info("Quarantined procedure %s: %s", proc_id, reason)
                else:
                    logger.warning("Failed to quarantine procedure %s (not found or already quarantined)", proc_id)
            summary["quarantines"] += 1

        # 7. Contradictions
        for contradiction in output.contradictions:
            await self._write_observation(
                db, source="deep_reflection", type="contradiction",
                content=json.dumps(contradiction) if isinstance(contradiction, dict) else str(contradiction),
                priority="high",
            )
            summary["contradictions"] += 1

        # 8. Consolidated reflection summary for embedding
        summary_parts = []
        if output.cognitive_state_update:
            summary_parts.append(output.cognitive_state_update)
        if output.focus_next:
            summary_parts.append(f"Focus: {output.focus_next}")
        for obs_text in output.observations[:3]:
            if obs_text:
                summary_parts.append(obs_text)
        if summary_parts:
            summary_text = "\n\n".join(summary_parts)
            # Trim to ~1000 tokens (~4000 chars)
            if len(summary_text) > 4000:
                summary_text = summary_text[:4000] + "..."
            await self._write_observation(
                db, source="deep_reflection", type="reflection_summary",
                content=summary_text, priority="medium",
            )
            summary["reflection_summary_stored"] = True

        # 9. Mark gathered observations as influenced (deferred from context
        #    gathering — only mark after reflection produced real output)
        if gathered_obs_ids:
            try:
                await observations.mark_influenced_batch(db, list(gathered_obs_ids))
            except Exception:
                logger.warning(
                    "Failed to mark influenced observations after successful routing",
                    exc_info=True,
                )

        # 10. Mark surplus insights consumed (they've been seen by deep reflection)
        if gathered_surplus_ids:
            try:
                from genesis.db.crud import surplus
                count = await surplus.mark_consumed_batch(
                    db, list(gathered_surplus_ids),
                )
                summary["surplus_consumed"] = count
            except Exception:
                logger.warning(
                    "Failed to mark surplus insights consumed", exc_info=True,
                )

        # 11. Emit events
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.REFLECTION, Severity.INFO,
                "deep_reflection.completed",
                f"Deep reflection routed: {summary}",
                **summary,
            )

        return summary

    async def route_assessment(
        self, output: WeeklyAssessmentOutput, db: aiosqlite.Connection,
    ) -> str:
        """Route weekly self-assessment output. Returns observation ID or empty string on failure."""
        if output.parse_failed:
            logger.error("Weekly assessment parse failed — skipping routing")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    "weekly_assessment.parse_failed",
                    "Weekly assessment output could not be parsed",
                )
            return ""

        content = json.dumps({
            "dimensions": [
                {
                    "dimension": d.dimension,
                    "score": d.score,
                    "evidence": d.evidence,
                    "data_available": d.data_available,
                }
                for d in output.dimensions
            ],
            "overall_score": output.overall_score,
            "observations": output.observations,
            "recommendations": output.recommendations,
        })

        obs_id = await self._write_observation(
            db, source="weekly_assessment", type="self_assessment",
            content=content, priority="medium",
        )

        # Write dated markdown for human auditability
        self._write_reflection_markdown(
            "self-assessment", content, output.overall_score,
        )

        return obs_id

    async def route_calibration(
        self, output: QualityCalibrationOutput, db: aiosqlite.Connection,
    ) -> str:
        """Route quality calibration output. Returns observation ID or empty string on failure."""
        if output.parse_failed:
            logger.error("Quality calibration parse failed — skipping routing")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    "quality_calibration.parse_failed",
                    "Quality calibration output could not be parsed",
                )
            return ""

        obs_type = "quality_drift" if output.drift_detected else "quality_calibration"

        content = json.dumps({
            "drift_detected": output.drift_detected,
            "quarantine_candidates": output.quarantine_candidates,
            "observations": output.observations,
        })

        obs_id = await self._write_observation(
            db, source="quality_calibration", type=obs_type,
            content=content,
            priority="high" if output.drift_detected else "medium",
        )

        self._write_reflection_markdown(
            "quality-calibration", content,
            drift=output.drift_detected,
        )

        return obs_id

    # ── Private helpers ───────────────────────────────────────────────

    async def _execute_memory_operation(
        self, db: aiosqlite.Connection, op: MemoryOperation,
    ) -> bool:
        """Execute a single memory consolidation operation.

        Returns True if the operation was executed, False if skipped.
        Validates target_ids exist before operating (LLM hallucination guard).
        """
        now = datetime.now(UTC).isoformat()

        # Validate all target_ids exist
        valid_ids: list[str] = []
        for tid in op.target_ids:
            exists = await observations.get_by_id(db, tid)
            if exists:
                valid_ids.append(tid)
            else:
                logger.warning(
                    "Memory op %s references non-existent observation ID: %s",
                    op.operation, tid,
                )

        if not valid_ids:
            logger.warning(
                "Memory op %s: no valid target_ids, skipping", op.operation,
            )
            return False

        try:
            if op.operation == "dedup":
                # Keep the first, resolve the rest
                if len(valid_ids) > 1:
                    to_resolve = valid_ids[1:]
                    await observations.resolve_batch(
                        db, to_resolve,
                        resolved_at=now,
                        resolution_notes=f"deduplicated: {op.reason}",
                    )

            elif op.operation == "merge":
                if not op.merged_content:
                    logger.warning(
                        "Memory op merge: merged_content is None, skipping",
                    )
                    return False
                # Create new merged observation
                await self._write_observation(
                    db, source="deep_reflection", type="merged_observation",
                    content=op.merged_content, priority="medium",
                )
                # Resolve all originals
                await observations.resolve_batch(
                    db, valid_ids,
                    resolved_at=now,
                    resolution_notes=f"merged: {op.reason}",
                )

            elif op.operation == "prune":
                await observations.resolve_batch(
                    db, valid_ids,
                    resolved_at=now,
                    resolution_notes=f"pruned: {op.reason}",
                )

            elif op.operation == "flag_contradiction":
                # Create a contradiction observation linking the IDs
                await self._write_observation(
                    db, source="deep_reflection", type="contradiction",
                    content=json.dumps({
                        "conflicting_ids": valid_ids,
                        "reason": op.reason,
                    }),
                    priority="high",
                )
                # Originals stay unresolved — contradiction needs human judgment

            else:
                logger.warning("Unknown memory operation: %s", op.operation)
                return False

        except Exception:
            logger.error(
                "Failed to execute memory op %s on %s",
                op.operation, valid_ids, exc_info=True,
            )
            return False

        # Audit trail — auto-resolve so these don't inflate the unresolved
        # backlog (no downstream code queries them in unresolved state)
        audit_id = await self._write_observation(
            db, source="deep_reflection", type="memory_operation_executed",
            content=json.dumps({
                "operation": op.operation,
                "target_ids": valid_ids,
                "reason": op.reason,
            }),
            priority="low",
        )
        if audit_id:
            now = datetime.now(UTC).isoformat()
            await observations.resolve(
                db, audit_id, resolved_at=now,
                resolution_notes="Auto-resolved audit trail",
            )
        return True

    async def _write_observation(
        self, db, *, source: str, type: str, content: str, priority: str,
    ) -> str | None:
        """Write to observations table with content-hash dedup.

        Returns observation ID on write, None if deduplicated.
        """
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        self._obs_attempted += 1

        if await observations.exists_by_hash(
            db, source=source, content_hash=content_hash, unresolved_only=True,
        ):
            self._obs_deduped += 1
            total = self._obs_attempted
            if total % 10 == 0:
                novel = total - self._obs_deduped
                rate = (novel / total * 100) if total else 0
                logger.info("Observation novelty: %d/%d (%.0f%% new)", novel, total, rate)
            logger.debug("Observation dedup: skipping %s/%s (hash=%s)", source, type, content_hash[:12])
            return None

        if self._observation_writer is not None:
            return await self._observation_writer.write(
                db, source=source, type=type, content=content, priority=priority,
            )

        obs_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        await observations.create(
            db, id=obs_id, source=source, type=type,
            content=content, priority=priority, created_at=now,
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        return obs_id

    def _write_reflection_markdown(
        self, label: str, content: str, score: float | None = None,
        *, drift: bool | None = None,
    ) -> None:
        """Write a dated markdown file for human auditability."""
        now = datetime.now(UTC)
        month_dir = self._reflections_dir / f"{now.year}-{now.month:02d}"
        try:
            month_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{now.strftime('%Y-%m-%d')}-{label}.md"
            filepath = month_dir / filename

            header = f"# {label.replace('-', ' ').title()} — {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            if score is not None:
                header += f"Overall score: {score:.2f}\n\n"
            if drift is not None:
                header += f"Drift detected: {drift}\n\n"

            # Pretty-print the JSON content
            try:
                parsed = json.loads(content)
                body = json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError):
                body = content

            filepath.write_text(header + "```json\n" + body + "\n```\n")
        except Exception:
            logger.warning("Failed to write reflection markdown to %s", month_dir, exc_info=True)
