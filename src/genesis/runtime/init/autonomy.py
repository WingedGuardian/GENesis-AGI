"""Init function: _init_autonomy."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize autonomy: protection, state machine, classification, verification."""
    try:
        # Fail-CLOSED default: the proposal dispatch gate must NEVER be left
        # unset. If the real gate can't be built below (missing DB/manager or a
        # ctor error), the ego reads a None gate as "no gate" and dispatches
        # every approved proposal UNGATED. Install a blocking sentinel FIRST so
        # any later failure degrades to fail-closed; the real gate replaces it
        # on success further down.
        from genesis.autonomy.proposal_gate import DenyHighRiskSentinel

        rt._proposal_dispatch_gate = DenyHighRiskSentinel()

        from genesis.autonomy.classification import ActionClassifier
        from genesis.autonomy.protection import ProtectedPathRegistry
        from genesis.autonomy.verification import TaskVerifier

        rt._protected_paths = ProtectedPathRegistry.from_yaml()
        logger.info("Protected paths registry loaded")

        rt._action_classifier = ActionClassifier()
        logger.info("Action classifier loaded")

        # GROUNDWORK(task-verify): constructed + validator-registered here, but
        # .verify() is never invoked on any live path yet (see verification.py).
        rt._task_verifier = TaskVerifier()

        from genesis.autonomy.verification import _code_task_validator

        rt._task_verifier.register_validator("code", _code_task_validator)
        logger.info("Task verifier initialized (code validator registered)")

        if rt._db is not None:
            from genesis.autonomy.state_machine import AutonomyManager

            rt._autonomy_manager = AutonomyManager(
                db=rt._db,
                event_bus=rt._event_bus,
            )
            await rt._autonomy_manager.load_or_create_defaults()
            logger.info("Autonomy manager created and state seeded")
        else:
            logger.warning("DB not available — autonomy state machine disabled")

        if rt._db is not None:
            from genesis.autonomy.approval import ApprovalManager
            from genesis.autonomy.autonomous_dispatch import (
                AutonomousCliApprovalGate,
                AutonomousDispatchRouter,
            )
            from genesis.autonomy.cli_policy import AutonomousCliPolicyExporter

            rt._approval_manager = ApprovalManager(
                db=rt._db,
                event_bus=rt._event_bus,
                classifier=rt._action_classifier,
            )

            # WS-8: wire the deterministic email autonomy gate into the
            # (already-built) outreach pipeline. Autonomy init runs AFTER
            # outreach init, so both the pipeline and the approval manager
            # exist here — wiring it during outreach init would pass a None
            # approval manager.
            if rt._outreach_pipeline is not None:
                from genesis.autonomy.email_gate import EmailAutonomyGate

                rt._outreach_pipeline.set_autonomy_gate(EmailAutonomyGate(
                    db=rt._db,
                    approval_manager=rt._approval_manager,
                    event_bus=rt._event_bus,
                ))
                logger.info("Email autonomy gate wired into outreach pipeline")

                # WS-8 PR-D: muted-by-default owner notification for autonomous
                # sends — toggled via config/autonomy.yaml `email_send_notify`.
                try:
                    from pathlib import Path

                    import yaml

                    import genesis as _genesis
                    _cfg = (
                        Path(_genesis.__file__).resolve().parents[2]
                        / "config" / "autonomy.yaml"
                    )
                    _data = yaml.safe_load(_cfg.read_text()) if _cfg.exists() else {}
                    _notify = bool((_data or {}).get("email_send_notify", False))
                except Exception:
                    _notify = False
                rt._outreach_pipeline.set_autonomous_send_notify(_notify)

            rt._autonomous_cli_policy_exporter = AutonomousCliPolicyExporter()
            if rt._router is not None:
                rt._autonomous_cli_approval_gate = AutonomousCliApprovalGate(
                    runtime=rt,
                    approval_manager=rt._approval_manager,
                )
                # Restore quote-reply map from DB (survives restart)
                try:
                    await rt._autonomous_cli_approval_gate.hydrate_delivery_map(rt._db)
                except Exception:
                    logger.warning("Failed to hydrate delivery-to-request map", exc_info=True)
                rt._autonomous_dispatcher = AutonomousDispatchRouter(
                    router=rt._router,
                    approval_gate=rt._autonomous_cli_approval_gate,
                )
                if rt._cc_reflection_bridge is not None:
                    try:
                        rt._cc_reflection_bridge.set_autonomous_dispatcher(
                            rt._autonomous_dispatcher,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to wire autonomous dispatcher into reflection bridge",
                            exc_info=True,
                        )
                if rt._inbox_monitor is not None and hasattr(
                    rt._inbox_monitor, "set_autonomous_dispatcher",
                ):
                    try:
                        rt._inbox_monitor.set_autonomous_dispatcher(
                            rt._autonomous_dispatcher,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to wire autonomous dispatcher into inbox monitor",
                            exc_info=True,
                        )
                logger.info("Autonomous dispatch router initialized")
            if rt._awareness_loop is not None:
                try:
                    rt._awareness_loop.set_autonomous_cli_policy_exporter(
                        rt._autonomous_cli_policy_exporter.export,
                    )
                except Exception:
                    logger.warning(
                        "Failed to wire CLI policy exporter into awareness loop",
                        exc_info=True,
                    )
            try:
                rt._autonomous_cli_policy_exporter.export()
            except Exception:
                logger.warning(
                    "Failed initial CLI policy export",
                    exc_info=True,
                )

            from genesis.util.tasks import tracked_task

            async def _poll_approval_timeouts() -> None:
                while True:
                    try:
                        await asyncio.sleep(60)
                        expired = await rt._approval_manager.expire_timed_out()
                        if expired:
                            logger.info("Expired %d approval requests", expired)
                        rt.record_job_success("approval_timeout_poll")
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        rt.record_job_failure("approval_timeout_poll", exc=exc)
                        logger.error("Approval timeout polling failed", exc_info=True)

            rt._approval_timeout_task = tracked_task(
                _poll_approval_timeouts(), name="approval-timeout-poller"
            )
            logger.info("Approval manager + timeout polling started")

        if rt._protected_paths and rt._cc_invoker:
            rt._cc_invoker.set_protected_paths(rt._protected_paths)
            logger.info("ProtectedPathRegistry wired into CCInvoker")

        # Proposal dispatch gate — evaluates approved ego proposals before dispatch.
        # Opaque to the ego: blocks proposals that exceed the current autonomy
        # level for their action domain. Wired into ego sessions in init/ego.py.
        if rt._db is not None and hasattr(rt, "_autonomy_manager") and rt._autonomy_manager:
            try:
                from genesis.autonomy.proposal_gate import ProposalDispatchGate
                from genesis.autonomy.rules import RuleEngine

                rt._proposal_dispatch_gate = ProposalDispatchGate(
                    autonomy_manager=rt._autonomy_manager,
                    rule_engine=rt._action_classifier._rule_engine if hasattr(
                        rt._action_classifier, "_rule_engine"
                    ) else RuleEngine(),
                    protected_paths=rt._protected_paths,
                )
                logger.info("Proposal dispatch gate initialized")
            except Exception:
                # Keep the fail-closed sentinel installed at the top of init()
                # (do NOT clear it) and surface the degradation loudly.
                logger.error(
                    "Failed to initialize proposal dispatch gate — running on "
                    "the fail-closed DenyHighRiskSentinel",
                    exc_info=True,
                )
                if rt._event_bus is not None:
                    from genesis.observability.types import Severity, Subsystem

                    await rt._event_bus.emit(
                        Subsystem.AUTONOMY,
                        Severity.ERROR,
                        "autonomy.gate_init_failed",
                        "Proposal dispatch gate init failed; using fail-closed sentinel",
                    )

        # Post-execution auditor — parses transcripts, feeds autonomy signals.
        # Wired into DirectSessionRunner in init/direct_session.py.
        if rt._db is not None and hasattr(rt, "_autonomy_manager") and rt._autonomy_manager:
            try:
                from genesis.autonomy.audit import PostExecutionAuditor

                rt._post_execution_auditor = PostExecutionAuditor(
                    protected_paths=rt._protected_paths,
                    autonomy_manager=rt._autonomy_manager,
                    event_bus=rt._event_bus,
                )
                logger.info("Post-execution auditor initialized")
            except Exception:
                # Auditor is observability, not a gate — no fail-closed here;
                # just surface the degraded autonomy signal loudly.
                logger.error(
                    "Failed to initialize post-execution auditor", exc_info=True,
                )
                if rt._event_bus is not None:
                    from genesis.observability.types import Severity, Subsystem

                    await rt._event_bus.emit(
                        Subsystem.AUTONOMY,
                        Severity.WARNING,
                        "autonomy.auditor_init_failed",
                        "Post-execution auditor init failed (autonomy signals degraded)",
                    )

        # Remediation registry — mechanical reflex layer for health probes
        try:
            from genesis.autonomy.remediation import RemediationRegistry, register_defaults

            outreach_fn = None
            if hasattr(rt, "_outreach_pipeline") and rt._outreach_pipeline:
                async def _outreach_submit(severity: str, title: str, body: str) -> None:
                    # Only CRITICAL health probes reach Telegram (as BLOCKER).
                    # WARNINGs stay dashboard-only — they are informational,
                    # not actionable alerts. This matches HealthOutreachBridge
                    # which also filters to CRITICAL + whitelist only.
                    if severity != "critical":
                        logger.debug(
                            "Remediation outreach suppressed (severity=%s): %s",
                            severity, title,
                        )
                        return
                    from genesis.outreach.pipeline import OutreachCategory, OutreachRequest
                    await rt._outreach_pipeline.submit(OutreachRequest(
                        category=OutreachCategory.BLOCKER,
                        topic=title,
                        context=body,
                        salience_score=0.9,
                        signal_type="health_alert",
                        source_id=f"remediation:{title}",
                        # Remediation body is a machine fact — deliver exactly.
                        verbatim=True,
                    ))
                outreach_fn = _outreach_submit
            rt._remediation_registry = RemediationRegistry(outreach_fn=outreach_fn)
            register_defaults(rt._remediation_registry)
            if rt._awareness_loop:
                rt._awareness_loop.set_remediation_registry(rt._remediation_registry)
            logger.info(
                "Remediation registry initialized (%d actions, wired=%s)",
                len(rt._remediation_registry.actions),
                rt._awareness_loop is not None,
            )
        except Exception:
            logger.warning("Failed to initialize remediation registry", exc_info=True)

        logger.info("Step 14: Autonomy subsystem initialized")

    except ImportError:
        logger.warning("genesis.autonomy not available")
    except Exception:
        logger.exception("Failed to initialize autonomy")
