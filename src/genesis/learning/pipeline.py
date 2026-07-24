"""Triage pipeline factory — assembles Phase 6 learning components into a callable."""

from __future__ import annotations

import logging
import re
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


# Channels driven by Genesis's autonomous subsystems rather than direct user
# input. Used to gate behaviors that should fire only on autonomous activity
# (e.g., aggressive SUCCESS-path procedure extraction) and to gate behaviors
# that should fire only on foreground activity (e.g., STEERING.md auto-update).
_AUTONOMOUS_CHANNELS = {"inbox", "mail", "reflection", "surplus"}

# WS-3 gate-2 (identity): channel -> origin for a steering-rule write. An
# ALLOW-map of positively-owner channels (ChannelType StrEnum values; all four
# are owner-gated surfaces), defaulting fail-closed to external_untrusted --
# deliberately the OPPOSITE polarity of _AUTONOMOUS_CHANNELS above, which is a
# deny-list that fails OPEN for any new/unlisted channel. `voice` is absent on
# purpose: ambient multi-speaker STT means user_text can be a non-owner human
# in the room. The gate only OBSERVES (shadow) -- a deny-list escape that
# writes a steering rule now produces a would-block row instead of being
# invisible.
_CHANNEL_ORIGIN = {
    "terminal": "owner",
    "telegram": "owner",
    "whatsapp": "owner",
    "web": "owner",
}


# A STEERING.md rule must READ as a terse imperative directive addressed to
# Genesis — not a chatty, multi-sentence status update. This guard is why the
# 2026-06-30 incident (a benign, multi-sentence user status update that merely
# contained the word "never" mid-sentence) could NOT be written as a "hard
# constraint" even though the outcome classifier mislabeled it approach_failure.
# It is DEFENSE-IN-DEPTH: the
# root fix is the outcome classifier, but this makes a mis-classification unable
# to corrupt an identity file on its own. Fail-CLOSED by design — missing a real
# correction is cheap (BIS still captures it; the user can restate directively),
# writing a non-directive as a hard constraint is the failure we prevent.
_DIRECTIVE_START = re.compile(
    r"^(never|don'?t|do not|stop|always|must not|"
    r"please\s+(?:don'?t|do not|never|stop|always)|"
    r"you\s+(?:must|should|shouldn'?t|need to|can'?t|cannot|never|always))\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MAX_DIRECTIVE_WORDS = 30


def _looks_like_directive(user_text: str) -> bool:
    """True if *user_text* reads as a terse imperative directive to Genesis.

    Three layers, all must pass: (1) at most two sentences, (2) at most
    ``_MAX_DIRECTIVE_WORDS`` words, (3) opens with an imperative verb or an
    explicit "you must/should…" addressed to Genesis. Fail-CLOSED: anything
    that is not clearly a directive is rejected.

    Note: the sentence split treats an abbreviation's period (``e.g.``, ``U.S.``)
    as a sentence terminator, so a terse directive containing one is rejected.
    This is intentional — fail-closed prefers dropping a legit directive (the
    user can restate it, and BIS still captures the raw correction) over risking
    a chatty message becoming a hard constraint.
    """
    text = (user_text or "").strip()
    if not text:
        return False
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) > 2:
        return False
    if len(text.split()) > _MAX_DIRECTIVE_WORDS:
        return False
    return bool(_DIRECTIVE_START.match(text))


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
        except Exception as exc:
            if runtime is not None:
                # _fire_triage (inbox/monitor + cc/conversation) wraps this in a
                # tracked_task but SWALLOWS the re-raise with a bare except+log,
                # so no task.failed is ever emitted for this path. Let the funnel
                # emit job.failed — it is the only bus signal this failure gets.
                runtime.record_job_failure("retrospective_triage", "pipeline exception", exc=exc)
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
            if outcome == OutcomeClass.CLASSIFICATION_FAILED:
                # CLASSIFICATION_FAILED is an error sentinel, not a real
                # outcome. The execution_traces.outcome_class CHECK constraint
                # does not allow this value — by setting outcome to None here,
                # all downstream guards short-circuit and nothing tries to
                # serialize the sentinel to the DB.
                logger.warning(
                    "Outcome classification failed for session %s; "
                    "skipping downstream learning (delta, attribution, extraction).",
                    summary.session_id or "unknown",
                )
                outcome = None
            else:
                delta = await delta_assessor.assess(summary)

            # Proto-ledger prediction logging REMOVED (WS-2 P2b, Sunset S1). It
            # wrote unfalsifiable domain='triage' rows (a restatement of the
            # classifier's own verdict, no deadline, hard-coded confidence) that
            # nothing ever graded. The real ledger (ledger_predictions + the P2
            # grader) supersedes it.

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

            # 6.2. Autonomy calibration — REMOVED (WS-2 P2b, the A1 harm-removal).
            # direct_session earn-back evidence no longer eats the LLM
            # classifier's SUCCESS/APPROACH_FAILURE/CAPABILITY_GAP self-verdict
            # (Genesis grading its own state — the dominant WS-0 failure class).
            # The P2 grader now feeds direct_session record_success/correction
            # from *mechanically graded* task_execution rows (ledger/grader.py:
            # failure-only — lane 'completed'→success, 'phase:failed'→correction,
            # nothing on slowness/cancel — behind the shadow-first ws2_ledger
            # settings gate).

        # 6.5. Procedure extraction — DEPRECATED (30-day grace period)
        #
        # The new path runs in extraction_job.py via two streams:
        #   Stream 1: programmatic struggle detection (action spine + heuristics)
        #   Stream 2: SLM extraction prompt flagging (procedure_candidate type)
        # Both route through the Judge LLM on call site 38.
        #
        # This legacy path (500-char summary extractor) remains as a fallback
        # during the transition. Remove after 2026-07-09.
        is_autonomous = summary.channel in _AUTONOMOUS_CHANNELS
        if router is not None and (
            outcome in (OutcomeClass.APPROACH_FAILURE, OutcomeClass.WORKAROUND_SUCCESS)
            or (outcome == OutcomeClass.SUCCESS and is_autonomous)
        ):
            try:
                logger.debug("Running deprecated procedure extraction (legacy 500-char path)")
                summary_text = f"User: {summary.user_text}\nOutput: {summary.response_text[:500]}"
                await extract_procedure(
                    db,
                    summary_text=summary_text,
                    outcome=outcome.value,
                    router=router,
                    session_tools_count=len(summary.tool_calls),
                )
            except Exception:
                logger.error("Procedure extraction failed (non-fatal)", exc_info=True)

        # 6.6. STEERING.md auto-population from user corrections
        # Only extract from foreground user sessions — autonomous pipelines
        # (inbox, mail, reflection) must never write to identity files.
        if (
            outcome == OutcomeClass.APPROACH_FAILURE
            and identity_loader is not None
            and summary.channel not in _AUTONOMOUS_CHANNELS
        ):
            try:
                written_rule = _extract_steering_rule(summary, identity_loader)
                if written_rule:
                    # WS-3 B1 gate-2 (identity): shadow-record the steering
                    # write, classified by CHANNEL (allow-map; unknown/voice ->
                    # external_untrusted, fail-closed) -- so a deny-list escape
                    # is OBSERVED. Owner channels self-guard to no row.
                    # Best-effort, never raises; counts only, never content.
                    from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED
                    from genesis.security import immunity_shadow

                    await immunity_shadow.record_would_block(
                        gate="identity",
                        source_kind="identity_write",
                        source_ref="learning/pipeline.py::_run_pipeline",
                        process="server",
                        blockable_count=1,
                        origin_class=_CHANNEL_ORIGIN.get(
                            summary.channel,
                            ORIGIN_EXTERNAL_UNTRUSTED,
                        ),
                        db=db,
                        detail={"mode": "steering", "channel": summary.channel},
                    )
            except Exception:
                logger.error("Steering rule extraction failed (non-fatal)", exc_info=True)

            # 6.7. BIS: capture raw correction for future theme clustering
            # GROUNDWORK(bis): Additive — steering rule behavior unchanged.
            try:
                await _record_behavioral_correction(
                    db,
                    summary,
                    observation_writer,
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
        summary: Any,
        loader: Any,
    ) -> str | None:
        """Extract a steering rule from a user correction and add to STEERING.md.

        Fires only on approach_failure (gated upstream). A rule is written ONLY
        if the user's text reads as a terse imperative directive addressed to
        Genesis — see :func:`_looks_like_directive`. This defends against a
        mis-classified chatty status update becoming a "hard constraint" (the
        2026-06-30 incident, where a benign Telegram DM containing "its never
        too late" was captured verbatim as a rule).

        Note: add_steering_rule() does synchronous file I/O (read + write
        STEERING.md). Acceptable because the file is tiny (<2KB) and local, and
        this runs in a fire-and-forget background task.
        """
        user_text = (summary.user_text or "").strip()
        if not _looks_like_directive(user_text):
            return None

        rule = user_text if len(user_text) <= 200 else user_text[:200] + "..."
        loader.add_steering_rule(rule)
        logger.info("Auto-added steering rule from user correction: %.80s...", rule)
        # Return the written rule so the caller can distinguish a REAL write
        # from a directive-filter reject (the WS-3 gate-2 emit fires only on
        # actual writes -- a reject is a non-event).
        return rule

    async def _record_behavioral_correction(
        db_conn: Any,
        summary: Any,
        obs_writer: ObservationWriter,
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

        context = (summary.response_text or "")[:500]
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
            # NOT 30_triage_calibration (rules update), NOT outreach_email_triage (outreach).
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
            attributions = {
                a.value if hasattr(a, "value") else str(a) for a in (delta.attributions or [])
            }

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
