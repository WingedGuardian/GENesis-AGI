"""Triage pipeline factory — assembles Phase 6 learning components into a callable."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from genesis.learning.classification.attribution import route_learning_signals
from genesis.learning.classification.delta import DeltaAssessor
from genesis.learning.classification.outcome import OutcomeClassifier
from genesis.learning.events import LEARNING_EVENTS
from genesis.learning.harvesting.debrief import parse_debrief
from genesis.learning.observation_writer import ObservationWriter
from genesis.learning.procedural.extractor import extract_procedure
from genesis.learning.triage.classifier import TriageClassifier
from genesis.learning.triage.prefilter import should_skip
from genesis.learning.triage.summarizer import build_summary
from genesis.learning.types import OutcomeClass, TriageDepth
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)


def build_triage_pipeline(
    *,
    db: Any,
    triage_classifier: TriageClassifier,
    outcome_classifier: OutcomeClassifier,
    delta_assessor: DeltaAssessor,
    observation_writer: ObservationWriter,
    event_bus: Any | None = None,
    router: Any | None = None,
    runtime: Any | None = None,
    identity_loader: Any | None = None,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build the post-response triage pipeline callable.

    Returns an async function matching ConversationLoop's triage_pipeline
    signature: ``async (output, user_text, channel) -> None``.
    """

    async def pipeline(output: Any, user_text: str, channel: str) -> None:
        try:
            await _run_pipeline(output, user_text, channel)
            # Record successful execution so the neural monitor shows runs
            if runtime is not None:
                runtime.record_job_success("retrospective_triage")
            _record_call_site(triage_depth=None)
        except Exception:
            if runtime is not None:
                runtime.record_job_failure("retrospective_triage", "pipeline exception")
            raise

    async def _run_pipeline(output: Any, user_text: str, channel: str) -> None:
        # 1. Summarise
        summary = build_summary(
            output,
            session_id=output.session_id,
            user_text=user_text,
            channel=channel,
        )

        # 2. Pre-filter
        if should_skip(summary):
            return

        # 3. Triage classification
        triage = await triage_classifier.classify(summary)

        if triage.depth == TriageDepth.SKIP:
            return

        # 4. Emit triage event
        if event_bus is not None:
            await event_bus.emit(
                subsystem=Subsystem.LEARNING,
                severity=Severity.INFO,
                event_type=LEARNING_EVENTS["TRIAGE_CLASSIFIED"],
                message=f"Triage depth={triage.depth.value}: {triage.rationale}",
                depth=triage.depth.value,
            )

        # 5. Deep analysis (depth >= WORTH_THINKING)
        outcome = None
        delta = None
        if triage.depth >= TriageDepth.WORTH_THINKING:
            outcome = await outcome_classifier.classify(summary)
            delta = await delta_assessor.assess(summary)

            # Log prediction for calibration (fire-and-forget)
            if outcome and runtime is not None:
                try:
                    prediction_logger = getattr(runtime, "_prediction_logger", None)
                    if prediction_logger is not None:
                        await prediction_logger.log(
                            action_id=f"triage-{summary.session_id or 'unknown'}",
                            prediction=f"Outcome classified as {outcome.value}",
                            confidence=0.7 if outcome.value == "success" else 0.5,
                            domain="triage",
                            reasoning=triage.rationale or "pipeline classification",
                        )
                except Exception:
                    logger.debug("Prediction logging failed (non-fatal)", exc_info=True)

        # 6. Observation + attribution routing (depth >= FULL_ANALYSIS)
        if triage.depth >= TriageDepth.FULL_ANALYSIS and outcome and delta:
            await observation_writer.write(
                db,
                source="retrospective",
                type=f"triage_depth_{triage.depth.value}",
                content=f"Outcome: {outcome.value}\n{triage.rationale}",
                priority="medium",
                category="learning",
            )
            await route_learning_signals(db, delta, outcome, observation_writer)

            # 6.1. Drive adaptation (error-isolated — must not crash pipeline)
            try:
                await _adapt_drives(db, outcome, delta)
            except Exception:
                logger.error("Drive adaptation failed (non-fatal)", exc_info=True)

            # 6.2. Autonomy calibration — wire Bayesian regression to live outcomes
            if runtime is not None:
                try:
                    mgr = getattr(runtime, "_autonomy_manager", None)
                    if mgr is not None:
                        from datetime import UTC, datetime

                        if outcome == OutcomeClass.SUCCESS:
                            await mgr.record_success("direct_session")
                        elif outcome in (
                            OutcomeClass.APPROACH_FAILURE,
                            OutcomeClass.CAPABILITY_GAP,
                        ):
                            await mgr.record_correction(
                                "direct_session",
                                corrected_at=datetime.now(UTC).isoformat(),
                            )
                except Exception:
                    logger.error("Autonomy calibration failed (non-fatal)", exc_info=True)

        # 6.5. Procedure extraction (error-isolated — must not crash pipeline)
        if outcome in (OutcomeClass.APPROACH_FAILURE, OutcomeClass.WORKAROUND_SUCCESS) and router is not None:
            try:
                summary_text = f"User: {summary.user_text}\nOutput: {summary.output_text[:500]}"
                await extract_procedure(
                    db,
                    summary_text=summary_text,
                    outcome=outcome.value,
                    router=router,
                )
            except Exception:
                logger.error("Procedure extraction failed (non-fatal)", exc_info=True)

        # 6.6. STEERING.md auto-population from user corrections
        # Only extract from foreground user sessions — autonomous pipelines
        # (inbox, mail, reflection) must never write to identity files.
        _AUTONOMOUS_CHANNELS = {"inbox", "mail", "reflection", "surplus"}
        if (
            outcome == OutcomeClass.APPROACH_FAILURE
            and identity_loader is not None
            and summary.channel not in _AUTONOMOUS_CHANNELS
        ):
            try:
                _extract_steering_rule(summary, identity_loader)
            except Exception:
                logger.error("Steering rule extraction failed (non-fatal)", exc_info=True)

            # 6.7. BIS: capture raw correction for future theme clustering
            # GROUNDWORK(bis): Additive — steering rule behavior unchanged.
            try:
                await _record_behavioral_correction(
                    db, summary, observation_writer,
                )
            except Exception:
                logger.error("BIS correction capture failed (non-fatal)", exc_info=True)

        # 7. Parse debrief learnings from output
        learnings = parse_debrief(output.text)
        for learning in learnings:
            await observation_writer.write(
                db,
                source="cc_debrief",
                type="learning",
                content=learning,
                priority="low",
                category="learning",
            )

    def _extract_steering_rule(
        summary: Any, loader: Any,
    ) -> None:
        """Extract a steering rule from user correction and add to STEERING.md.

        Only fires on approach_failure — user explicitly corrected Genesis.
        Looks for strong negative signals ("never", "don't", "stop", "wrong").

        Note: add_steering_rule() does synchronous file I/O (read + write
        STEERING.md).  Acceptable because the file is tiny (<2KB) and local,
        and this runs in a fire-and-forget background task.
        """
        import re

        user_text = summary.user_text or ""
        # Only auto-add rules from strong negative feedback patterns
        strong_negative = re.search(
            r"\b(never|don'?t|stop|wrong|shouldn'?t|must not|do not)\b",
            user_text,
            re.IGNORECASE,
        )
        if not strong_negative:
            return

        # Extract the rule — use the user's own words (truncated)
        rule = user_text.strip()
        if len(rule) > 200:
            rule = rule[:200] + "..."
        loader.add_steering_rule(rule)
        logger.info("Auto-added steering rule from user correction: %.80s...", rule)

    async def _record_behavioral_correction(
        db_conn: Any, summary: Any, obs_writer: ObservationWriter,
    ) -> None:
        """Capture a raw behavioral correction for BIS theme clustering.

        GROUNDWORK(bis): Purely additive — stores the raw user text and
        context in behavioral_corrections table for future vector-based
        theme clustering and treatment escalation.
        """
        from genesis.db.crud.behavioral import create_correction

        user_text = summary.user_text or ""
        if not user_text.strip():
            return

        context = (summary.output_text or "")[:500]
        await create_correction(
            db_conn,
            raw_user_text=user_text[:500],
            context=context,
            severity=0.5,  # Default; V4 will LLM-assess severity
        )
        logger.info("BIS: captured behavioral correction: %.60s...", user_text)

    def _record_call_site(*, triage_depth: str | None) -> None:
        """Best-effort record to call_site_last_run (async fire-and-forget)."""
        from genesis.util.tasks import tracked_task

        async def _record() -> None:
            from genesis.observability.call_site_recorder import record_last_run

            # 29_retrospective_triage — observability record for the triage classifier call.
            # Same call site as classifier.py:47 (the LIVE triage). NOT 2_triage (removed),
            # NOT 30_triage_calibration (rules update), NOT email_triage (outreach).
            await record_last_run(
                db,
                call_site_id="29_retrospective_triage",
                provider="pipeline",
                model_id="triage_classifier",
                response_text=f"depth={triage_depth}" if triage_depth else "executed",
            )

        tracked_task(_record(), name="triage-record-call-site")

    async def _adapt_drives(db_conn: Any, outcome: Any, delta: Any) -> None:
        """Adapt drive weights based on learning outcome and attribution.

        EMA-style updates with small α (0.005) so weights move slowly —
        dozens of interactions needed to shift meaningfully.
        Drive-outcome mapping:
          cooperation: user_model_gap, scope_underspecified → down; SUCCESS → up
          competence: genesis_capability → down; approach recovered → up
          curiosity: approach_failure (bad exploration) → down; novel finding → up
          preservation: external_blocker (system unreliable) → down
        """
        from genesis.db.crud import drive_weights

        _ALPHA = 0.005  # Small learning rate — slow adaptation

        attributions = set()
        if delta and hasattr(delta, "attributions"):
            attributions = {a.value if hasattr(a, "value") else str(a) for a in (delta.attributions or [])}

        outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)

        nudges: dict[str, float] = {}

        # Cooperation: up on success, down on user-model gaps
        if outcome_val == "success":
            nudges["cooperation"] = nudges.get("cooperation", 0) + _ALPHA
        if "user_model_gap" in attributions or "scope_underspecified" in attributions:
            nudges["cooperation"] = nudges.get("cooperation", 0) - _ALPHA

        # Competence: down on capability gap, up on workaround success
        if "genesis_capability" in attributions:
            nudges["competence"] = nudges.get("competence", 0) - _ALPHA
        if outcome_val == "workaround_success":
            nudges["competence"] = nudges.get("competence", 0) + _ALPHA

        # Curiosity: down on approach failure, up on success with novel attribution
        if outcome_val == "approach_failure":
            nudges["curiosity"] = nudges.get("curiosity", 0) - _ALPHA
        if outcome_val == "success" and "genesis_interpretation" not in attributions:
            nudges["curiosity"] = nudges.get("curiosity", 0) + _ALPHA * 0.5

        # Preservation: down on external blockers
        if "external_limitation" in attributions:
            nudges["preservation"] = nudges.get("preservation", 0) - _ALPHA

        for drive_name, delta_val in nudges.items():
            if abs(delta_val) > 1e-9:
                await drive_weights.adapt_weight(db_conn, drive_name, delta_val)

    return pipeline
