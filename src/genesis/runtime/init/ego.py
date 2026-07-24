"""Init function: _init_ego.

Two-ego architecture:
- **User Ego (CEO)**: Proactive user value. Opus, user-focused context.
  Owns morning reports. Single voice to the user.
- **Genesis Ego (COO)**: Self-maintenance. Sonnet, system-focused context.
  No morning reports. Escalates to user ego via observations.

Both share the same EgoSession infrastructure — they differ in context
builder, prompt, model, cadence, and state keys.

TopicManager and ReplyWaiter are NOT available at bootstrap time —
they're created in standalone.py after Telegram init. ProposalWorkflow
is initialized without them; standalone.py wires them via setters later.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

_IDENTITY_DIR = Path(__file__).resolve().parents[2] / "identity"


def _is_non_actionable_infra_event(subsystem: str, event_type: str) -> bool:
    """True for routing/provider chain-exhaustion events the COO ego can't act on.

    A provider outage / chain exhaustion is not reconfigurable by the ego, so a
    reactive Opus/high cycle on it always no-ops — these drove ~92% of the COO's
    zero-proposal reactive cycles. The condition still reaches the ego via
    ProviderEscalation -> `provider_failure` observation (routing/escalation.py),
    read in PROACTIVE context (genesis_context.py::_observations_section) — so the
    gate drops the WASTE, not the signal. Module-level so it is unit-testable.
    """
    return subsystem in ("routing", "providers") and event_type == "all_exhausted"


_REFLEX_OWNED_EVENT_TYPES = frozenset({"task.failed", "job.failed"})


def _is_reflex_owned_event(event_type: str) -> bool:
    """True for failure classes the reflex arc ingests, dedups, and cards.

    ``task.failed`` was dark until the reflex arc installed the default event
    bus (util/tasks.py) — the ego never reacted to it historically because
    the events were never emitted. ``job.failed`` is the same shape from the
    other direction: the funnel in ``runtime/_job_health.record_job_failure``
    now emits it for exception-driven background-job failures (the largest
    class of internal defects, previously invisible to the bus). Both belong to
    the reflex arc, which fingerprints recurrences into one signal and cards
    the user. A reactive ego cycle per failure would re-run the event-burst
    storm mode (see the push_reactive_event note in ego/cadence.py) with a
    message-keyed dedup that variable exception payloads bypass. Module-level so
    it is unit-testable.
    """
    return event_type in _REFLEX_OWNED_EVENT_TYPES


async def init(rt: GenesisRuntime) -> None:
    """Initialize both egos: user ego + genesis ego."""
    # Hard dependencies — skip if unavailable
    if rt._db is None:
        logger.warning("DB not available — ego disabled")
        return
    if rt._router is None:
        logger.warning("Router not available — ego disabled")
        return
    if rt._cc_invoker is None or rt._session_manager is None:
        logger.warning("CC relay not available — ego disabled")
        return

    try:
        from genesis.cc.invoker import CCInvoker
        from genesis.cc.session_config import SessionConfigBuilder
        from genesis.ego.cadence import EgoCadenceManager
        from genesis.ego.compaction import CompactionEngine
        from genesis.ego.config import load_ego_config
        from genesis.ego.dispatch import EgoDispatcher
        from genesis.ego.genesis_context import GenesisEgoContextBuilder
        from genesis.ego.proposals import ProposalWorkflow
        from genesis.ego.session import EgoSession
        from genesis.ego.user_context import UserEgoContextBuilder
        from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

        config = load_ego_config()
        if not config.enabled:
            logger.info("Ego disabled by config — skipping")
            return

        # MCP configs: user ego gets memory-only, genesis ego gets full health+memory.
        # User ego has no need for health tools — its jurisdiction is the user's
        # world, not Genesis infrastructure. System issues reach it only via
        # genesis ego escalations.
        config_builder = SessionConfigBuilder()
        user_ego_mcp_path = config_builder.build_mcp_config("user_reflection")
        genesis_ego_mcp_path = config_builder.build_mcp_config("reflection")

        # -- Shared components --
        # ProposalWorkflow is shared — both egos create proposals that go
        # through the same Telegram approval pipeline.
        proposal_workflow = ProposalWorkflow(
            db=rt._db,
            memory_store=rt._memory_store,
            autonomy_manager=getattr(rt, "_autonomy_manager", None),
        )
        rt._ego_proposal_workflow = proposal_workflow

        dispatcher = EgoDispatcher(db=rt._db)

        # ================================================================
        # USER EGO (CEO) — proactive user value
        # ================================================================
        # NOTE: "8_user_ego_compaction" is an OBSERVABILITY LABEL only — passed
        # to cost/event tracking, NOT to route_call(). The matching routing call
        # site is "8_ego_compaction" (in model_routing.yaml). The "8_*" namespace
        # is documented in observability/_call_site_meta.py master cross-reference.
        user_ego_compaction = CompactionEngine(
            db=rt._db,
            router=rt._router,
            state_key_summary="user_ego_compacted_summary",
            focus_summary_key="ego_focus_summary",
            call_site_id="8_user_ego_compaction",
        )

        user_ego_context = UserEgoContextBuilder(
            db=rt._db,
            health_data=rt._health_data,
            capabilities=_CAPABILITY_DESCRIPTIONS,
        )

        user_ego_invoker = CCInvoker()

        user_ego_session = EgoSession(
            invoker=user_ego_invoker,
            session_manager=rt._session_manager,
            compaction_engine=user_ego_compaction,
            context_builder=user_ego_context,
            proposal_workflow=proposal_workflow,
            dispatcher=dispatcher,
            config=config,  # Uses base config (Opus, HIGH effort)
            db=rt._db,
            event_bus=rt._event_bus,
            direct_session_runner=rt._direct_session_runner,
            mcp_config_path=user_ego_mcp_path,
            prompt_path=_IDENTITY_DIR / "USER_EGO_SESSION.md",
            call_site="7_user_ego_cycle",
            session_id_key="user_ego_cc_session_id",
            focus_summary_key="ego_focus_summary",  # User ego owns the canonical focus
            source_tag="user_ego_cycle",
            router=rt._router,
        )

        if rt._autonomous_dispatcher is not None:
            user_ego_session.set_autonomous_dispatcher(rt._autonomous_dispatcher)

        if getattr(rt, "_proposal_dispatch_gate", None) is not None:
            user_ego_session.set_proposal_gate(rt._proposal_dispatch_gate)

        if getattr(rt, "_outreach_pipeline", None) is not None:
            user_ego_session.set_outreach_pipeline(rt._outreach_pipeline)

        # Store as the primary ego session (backwards compatible)
        rt._ego_session = user_ego_session

        user_ego_cadence = EgoCadenceManager(
            session=user_ego_session,
            config=config,
            idle_detector=rt._idle_detector,
            db=rt._db,
            event_bus=rt._event_bus,
            autonomy_manager=getattr(rt, "_autonomy_manager", None),
        )
        rt._ego_cadence_manager = user_ego_cadence

        await user_ego_cadence.start()
        logger.info(
            "User ego initialized (cadence=%dm, model=%s, effort=%s)",
            config.cadence_minutes,
            config.model,
            config.default_effort,
        )

        # ================================================================
        # GENESIS EGO (COO) — self-maintenance
        # ================================================================
        genesis_ego_config = dataclasses.replace(
            config,
            model="sonnet",
            default_effort="high",
            cadence_minutes=config.genesis_cadence_minutes,
            max_interval_minutes=config.genesis_max_interval_minutes,
            morning_report_enabled=False,
        )

        # NOTE: "8_genesis_ego_compaction" is an OBSERVABILITY LABEL only
        # (same as 8_user_ego_compaction above). Routing call site is "8_ego_compaction".
        genesis_ego_compaction = CompactionEngine(
            db=rt._db,
            router=rt._router,
            state_key_summary="genesis_ego_compacted_summary",
            focus_summary_key="genesis_ego_focus_summary",
            call_site_id="8_genesis_ego_compaction",
        )

        genesis_ego_context = GenesisEgoContextBuilder(
            db=rt._db,
            health_data=rt._health_data,
            capabilities=_CAPABILITY_DESCRIPTIONS,
        )

        genesis_ego_invoker = CCInvoker()

        genesis_ego_session = EgoSession(
            invoker=genesis_ego_invoker,
            session_manager=rt._session_manager,
            compaction_engine=genesis_ego_compaction,
            context_builder=genesis_ego_context,
            proposal_workflow=proposal_workflow,
            dispatcher=dispatcher,
            config=genesis_ego_config,
            db=rt._db,
            event_bus=rt._event_bus,
            direct_session_runner=rt._direct_session_runner,
            mcp_config_path=genesis_ego_mcp_path,
            prompt_path=_IDENTITY_DIR / "GENESIS_EGO_SESSION.md",
            call_site="7_genesis_ego_cycle",
            session_id_key="genesis_ego_cc_session_id",
            focus_summary_key="genesis_ego_focus_summary",  # Separate from user ego
            source_tag="genesis_ego_cycle",
            router=rt._router,
        )

        if rt._autonomous_dispatcher is not None:
            genesis_ego_session.set_autonomous_dispatcher(rt._autonomous_dispatcher)

        if getattr(rt, "_proposal_dispatch_gate", None) is not None:
            genesis_ego_session.set_proposal_gate(rt._proposal_dispatch_gate)

        if getattr(rt, "_outreach_pipeline", None) is not None:
            genesis_ego_session.set_outreach_pipeline(rt._outreach_pipeline)

        rt._genesis_ego_session = genesis_ego_session

        genesis_ego_cadence = EgoCadenceManager(
            session=genesis_ego_session,
            config=genesis_ego_config,
            idle_detector=rt._idle_detector,
            db=rt._db,
            event_bus=rt._event_bus,
            autonomy_manager=getattr(rt, "_autonomy_manager", None),
        )
        rt._genesis_ego_cadence_manager = genesis_ego_cadence

        await genesis_ego_cadence.start()
        logger.info(
            "Genesis ego initialized (cadence=%dm, model=%s, effort=%s)",
            genesis_ego_config.cadence_minutes,
            genesis_ego_config.model,
            genesis_ego_config.default_effort,
        )

        # ================================================================
        # OUTCOME TRACKING — on_end hook for ego-dispatched sessions
        # ================================================================
        if rt._session_manager is not None:
            from genesis.cc.session_manager import cc_sessions
            from genesis.db.crud import observations as obs_crud

            async def _ego_dispatch_on_end(session_id: str) -> None:
                """Create an observation when an ego-dispatched session ends."""
                try:
                    session = await cc_sessions.get_by_id(rt._db, session_id)
                    if not session:
                        return
                    if session.get("source_tag") != "ego_dispatch":
                        return

                    import uuid
                    from datetime import UTC, datetime

                    status = session.get("status", "unknown")
                    cost = session.get("cost_usd", 0.0)

                    # caller_context lives inside the metadata JSON blob,
                    # not as a top-level column on cc_sessions.
                    import json as _json

                    _meta_raw = session.get("metadata") or "{}"
                    try:
                        _meta = (
                            _json.loads(_meta_raw)
                            if isinstance(_meta_raw, str)
                            else (_meta_raw or {})
                        )
                    except (ValueError, TypeError):
                        _meta = {}
                    caller_ctx = _meta.get("caller_context", "")

                    await obs_crud.create(
                        rt._db,
                        id=str(uuid.uuid4()),
                        source="ego_dispatch",
                        type="execution_outcome",
                        content=(
                            f"Ego dispatch session {session_id[:8]} "
                            f"completed: status={status}, cost=${cost:.4f}. "
                            f"Context: {caller_ctx}"
                        ),
                        priority="medium",
                        category="ego_dispatch",
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    logger.info(
                        "Recorded ego dispatch outcome for session %s",
                        session_id[:8],
                    )

                    # Write goal progress if proposal is linked to a goal
                    if "ego_proposal:" in caller_ctx:
                        try:
                            from genesis.db.crud import ego as ego_crud
                            from genesis.db.crud import user_goals

                            _pid = caller_ctx.split("ego_proposal:")[-1]
                            _prop = await ego_crud.get_proposal(rt._db, _pid)
                            _gid = _prop.get("goal_id") if _prop else None
                            if _gid:
                                _content = (_prop.get("content") or "")[:60]
                                # Extract outcome summary from session
                                # metadata (stored by DirectSessionRunner
                                # at direct_session.py:479,483).
                                # Prefer error when present — DirectSession
                                # calls complete() even on is_error=True;
                                # only Python exceptions trigger fail().
                                _error = (_meta.get("error") or "").strip()
                                if _error:
                                    _outcome = _error[:120]
                                else:
                                    _outcome = (_meta.get("output_text") or "")[:120]
                                _outcome = _outcome.replace("\n", " ").strip()
                                if _outcome:
                                    _note = (
                                        f"[{status}] {_content}: "
                                        f"{_outcome} (session:{session_id[:8]})"
                                    )
                                else:
                                    _note = f"[{status}] {_content} (session:{session_id[:8]})"
                                await user_goals.add_progress_note(
                                    rt._db,
                                    _gid,
                                    _note,
                                )
                                logger.info(
                                    "Recorded goal progress for %s via %s",
                                    _gid[:12],
                                    _pid[:8],
                                )
                        except Exception:
                            logger.debug(
                                "Failed to record goal progress",
                                exc_info=True,
                            )
                except Exception:
                    logger.warning(
                        "Failed to record ego dispatch outcome",
                        exc_info=True,
                    )

            rt._session_manager.add_on_end(_ego_dispatch_on_end)
            logger.info("Ego dispatch outcome hook registered")

        # ================================================================
        # EVENT SIGNAL WIRING — EventBus → ego signal queue
        # ================================================================
        if rt._event_bus is not None:
            from genesis.observability.types import Severity

            # Route by subsystem — reliable field on every GenesisEvent.
            # User-facing subsystems trigger user ego; system subsystems
            # trigger Genesis ego.
            _USER_SUBSYSTEMS = frozenset(
                {
                    "outreach",
                    "inbox",
                    "mail",
                    "recon",
                }
            )
            _SYSTEM_SUBSYSTEMS = frozenset(
                {
                    "health",
                    "routing",
                    "providers",
                    "guardian",
                    "awareness",
                    "surplus",
                    "learning",
                }
            )

            async def _on_high_severity_event(event) -> None:
                """Route high-severity events to the appropriate ego's signal queue.

                ERROR → reactive signal (push_reactive_event)
                CRITICAL → escalation signal (push_escalation_event)

                WARNING events are excluded (handled by the subscriber
                threshold).  The ego picks up WARNING-level issues during
                regular proactive cycles via health_status context — no
                need to wake up for them.
                """
                # Only react to ERROR+ events with actionable types
                if event.event_type in ("heartbeat", "metric"):
                    return

                # REFLEX GATE: task.failed belongs to the reflex arc (signal
                # store → dedup → user card) — never a per-failure reactive
                # ego cycle. See _is_reflex_owned_event.
                if _is_reflex_owned_event(event.event_type):
                    return

                subsystem = str(getattr(event, "subsystem", ""))

                # DOMAIN GATE: drop non-COO-actionable routing/provider exhaustion
                # from the reactive path (still surfaced via ProviderEscalation ->
                # proactive-context observation). See _is_non_actionable_infra_event.
                if _is_non_actionable_infra_event(subsystem, event.event_type):
                    return

                event_dict = {
                    "type": event.event_type,
                    "summary": str(event.message)[:200],
                    "priority": event.severity.name if hasattr(event, "severity") else "high",
                    "source": subsystem,
                }

                # Route by subsystem (reliable field on every event)
                if subsystem in _USER_SUBSYSTEMS:
                    target = user_ego_cadence
                elif subsystem in _SYSTEM_SUBSYSTEMS:
                    target = genesis_ego_cadence
                else:
                    # ego, perception, memory, etc. — route to Genesis ego
                    # (system-internal concerns)
                    target = genesis_ego_cadence

                # CRITICAL → escalation signal, ERROR → reactive signal
                is_critical = hasattr(event, "severity") and event.severity.name == "CRITICAL"
                if is_critical:
                    target.push_escalation_event(event_dict)
                else:
                    target.push_reactive_event(event_dict)

            rt._event_bus.subscribe(
                _on_high_severity_event,
                min_severity=Severity.ERROR,
            )
            logger.info("Ego event signal subscriber registered (ERROR+ threshold)")

    except ImportError:
        logger.warning("genesis.ego not available")
    except Exception:
        logger.exception("Failed to initialize ego")
