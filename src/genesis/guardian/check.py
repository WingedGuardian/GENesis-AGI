"""Main check logic — HOST-SIDE. The core Guardian flow per timer invocation.

Flow:
  1. Load config + state from disk
  2. Collect health signals (parallel, <10s)
  3. Feed snapshot to state machine
  4. Act based on state (see below)
  5. Save state, exit

State actions:
  HEALTHY        → write heartbeat, prune snapshots if due, exit
  SIGNAL_DROPPED → log, save state, exit (next tick rechecks)
  CONFIRMING     → recheck loop until confirmed or recovered
  SURVEYING      → collect diagnostics, run CC diagnosis
  CONFIRMED_DEAD → request approval, execute recovery
  RECOVERED      → verify (handled by state machine on next tick)
  PAUSED         → log, exit (infrastructure checks in state machine)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.alert.telegram import TelegramAlertChannel
from genesis.guardian.approval import ApprovalServer
from genesis.guardian.collector import collect_diagnostics
from genesis.guardian.config import GuardianConfig, load_config, load_secrets
from genesis.guardian.diagnosis import DiagnosisEngine
from genesis.guardian.diagnosis_writer import write_diagnosis_result
from genesis.guardian.dialogue import DialogueStatus, build_request, send_dialogue
from genesis.guardian.health_signals import collect_all_signals
from genesis.guardian.recovery import RecoveryEngine
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import (
    ConfirmationStateMachine,
    GuardianState,
)

logger = logging.getLogger(__name__)

_STARTED_AT = datetime.now(UTC)


def _setup_logging() -> None:
    """Configure logging for Guardian — journald + persistent file.

    File log survives host reboots and journald rotation. Critical for
    post-mortem analysis (incident 2026-04-08: CC failure evidence lost).
    """
    import sys
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_format = "%(asctime)s [guardian] %(levelname)s %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=datefmt,
    )

    # Skip file handler during tests to avoid polluting filesystem
    if "pytest" in sys.modules or "_pytest" in sys.modules:
        return

    log_dir = Path("~/.local/state/genesis-guardian").expanduser()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "guardian.log",
            maxBytes=1_000_000,   # 1 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(log_format, datefmt=datefmt))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        logging.getLogger(__name__).warning(
            "Cannot create Guardian log file in %s — file logging disabled",
            log_dir,
        )


def _build_dispatcher(config: GuardianConfig) -> AlertDispatcher:
    """Build the alert dispatcher with configured channels."""
    dispatcher = AlertDispatcher()

    token = config.alert.telegram_bot_token
    chat_id = config.alert.telegram_chat_id
    thread_id = config.alert.telegram_thread_id

    # Try shared mount credentials (auto-propagated from container)
    # Treat each source as atomic — all-or-nothing, never mix sources
    if not token or not chat_id:
        from genesis.guardian.credential_bridge import load_telegram_credentials
        creds = load_telegram_credentials(config.state_dir)
        bridge_token = creds.get("TELEGRAM_BOT_TOKEN", "")
        bridge_chat = creds.get("TELEGRAM_CHAT_ID", "")
        if bridge_token and bridge_chat:
            token = bridge_token
            chat_id = bridge_chat
            thread_id = thread_id or creds.get("TELEGRAM_THREAD_ID", "")

    # Legacy fallback: local secrets.env copy (from pre-bridge installs)
    if not token or not chat_id:
        secrets = load_secrets()
        legacy_token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        legacy_chat = secrets.get("TELEGRAM_CHAT_ID", "")
        if legacy_token and legacy_chat:
            token = legacy_token
            chat_id = legacy_chat
            thread_id = thread_id or secrets.get("TELEGRAM_THREAD_ID", "")

    if token and chat_id:
        dispatcher.add_channel(TelegramAlertChannel(
            bot_token=token,
            chat_id=chat_id,
            thread_id=thread_id or None,
        ))
    else:
        logger.warning("No Telegram credentials — alerts will only go to journal")

    return dispatcher


async def _write_guardian_heartbeat(config: GuardianConfig) -> None:
    """Write heartbeat file into the container so Genesis knows Guardian is alive.

    Uses stdin pipe instead of heredoc to avoid shell injection.
    """
    uptime_s = (datetime.now(UTC) - _STARTED_AT).total_seconds()
    heartbeat = json.dumps({
        "guardian_alive": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "uptime_s": round(uptime_s),
    })

    try:
        # Use stdin pipe — avoids heredoc injection risk
        proc = await asyncio.create_subprocess_exec(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            "mkdir -p ~/.genesis && cat > ~/.genesis/guardian_heartbeat.json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=heartbeat.encode("utf-8")),
            timeout=10.0,
        )
        if proc.returncode != 0:
            logger.error(
                "Failed to write guardian heartbeat: %s",
                stderr_bytes.decode("utf-8", errors="replace"),
            )
    except TimeoutError:
        logger.error("Guardian heartbeat write timed out", exc_info=True)
    except OSError as exc:
        logger.error("Guardian heartbeat write failed: %s", exc)


async def run_check(config: GuardianConfig | None = None) -> None:
    """Run a single Guardian health check cycle.

    This is the main entry point called by __main__.py via asyncio.run().
    Each invocation is a complete check cycle — no persistent state beyond
    the state file on disk.
    """
    if config is None:
        config = load_config()

    state_path = config.state_path / "state.json"
    config.state_path.mkdir(parents=True, exist_ok=True)

    # Load persistent state
    sm = ConfirmationStateMachine(config)
    sm.load_state(state_path)

    dispatcher = _build_dispatcher(config)
    snapshots = SnapshotManager(config)
    diagnosis_engine = DiagnosisEngine(config)
    recovery_engine = RecoveryEngine(config, sm, snapshots, dispatcher)

    try:
        await _check_cycle(config, sm, dispatcher, snapshots, diagnosis_engine, recovery_engine)
        # Heartbeat means "Guardian process is alive and watching" —
        # NOT "Genesis container is healthy". Any successful check cycle
        # (regardless of resulting state) should refresh liveness. A
        # crashed _check_cycle raises out before reaching here, which
        # correctly withholds the heartbeat — that is a real Guardian
        # failure that Genesis-side monitoring should see.
        await _write_guardian_heartbeat(config)
    finally:
        # Always save state, even on error
        sm.save_state(state_path)


async def _check_cycle(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    snapshots: SnapshotManager,
    diagnosis_engine: DiagnosisEngine,
    recovery_engine: RecoveryEngine,
) -> None:
    """Execute the main check logic based on current state."""
    # Step 1: Collect health signals
    snapshot = await collect_all_signals(config)

    # Step 2: Feed to state machine
    transition = sm.process(snapshot)

    logger.info(
        "State: %s → %s (%s)",
        transition.old_state, transition.new_state, transition.reason,
    )

    state = sm.current_state

    # Step 3: Act based on state
    if state == GuardianState.HEALTHY:
        await _handle_healthy(config, snapshots)

    elif state == GuardianState.SIGNAL_DROPPED:
        logger.warning(
            "Signal dropped: %s",
            ", ".join(s.name for s in snapshot.failed_signals),
        )

    elif state == GuardianState.CONFIRMING:
        await _handle_confirming(config, sm, snapshot)

    elif state == GuardianState.SURVEYING:
        await _handle_surveying(
            config, sm, dispatcher, diagnosis_engine, recovery_engine, snapshot,
        )

    elif state == GuardianState.CONTACTING_GENESIS:
        # Handled within _handle_surveying — shouldn't persist between invocations
        logger.warning("Stale CONTACTING_GENESIS state — re-entering survey")
        sm.set_surveying()

    elif state == GuardianState.AWAITING_SELF_HEAL:
        await _handle_awaiting_self_heal(
            config, sm, dispatcher, diagnosis_engine, recovery_engine,
            snapshot,
        )

    elif state == GuardianState.CONFIRMED_DEAD:
        if transition.action_needed:
            await _handle_confirmed_dead(
                config, sm, dispatcher, diagnosis_engine, recovery_engine,
            )

    elif state == GuardianState.PAUSED:
        if transition.action_needed:
            # Pause reminder or infrastructure failure while paused
            await dispatcher.send(Alert(
                severity=AlertSeverity.INFO,
                title="Genesis pause reminder",
                body=transition.reason,
            ))

    elif state in (GuardianState.RECOVERING, GuardianState.RECOVERED):
        # These are transient states managed by the recovery engine
        pass


async def _handle_healthy(
    config: GuardianConfig,
    snapshots: SnapshotManager,
) -> None:
    """Actions when all probes are healthy."""
    # Heartbeat is written by run_check after each successful check cycle,
    # regardless of state outcome, so HEALTHY no longer needs a direct call.

    # Periodic snapshot pruning (don't prune every check — too expensive)
    prune_marker = config.state_path / ".last_prune"
    should_prune = True
    if prune_marker.exists():
        try:
            last_prune = datetime.fromisoformat(prune_marker.read_text().strip())
            hours_since = (datetime.now(UTC) - last_prune).total_seconds() / 3600
            should_prune = hours_since >= 24
        except (ValueError, OSError):
            should_prune = True

    if should_prune:
        pruned = await snapshots.prune()
        if pruned > 0:
            logger.info("Pruned %d old snapshots", pruned)
        prune_marker.parent.mkdir(parents=True, exist_ok=True)
        prune_marker.write_text(datetime.now(UTC).isoformat())


async def _handle_confirming(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    initial_snapshot: object,
) -> None:
    """Recheck loop during confirmation phase."""
    # Wait before rechecking (absorbs restart transients)
    await asyncio.sleep(config.confirmation.recheck_delay_s)

    # Recheck
    snapshot = await collect_all_signals(config)
    transition = sm.process(snapshot)

    logger.info(
        "Recheck: %s → %s (%s)",
        transition.old_state, transition.new_state, transition.reason,
    )


async def _handle_surveying(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    diagnosis_engine: DiagnosisEngine,
    recovery_engine: RecoveryEngine,
    snapshot: object,
) -> None:
    """First contact Genesis, then fall back to diagnosis if Genesis is dark.

    Flow:
      1. Try to contact Genesis via dialogue protocol
      2. If Genesis responds "handling" → AWAITING_SELF_HEAL (wait for it)
      3. If Genesis responds "stand_down" → PAUSED
      4. If Genesis responds "need_help" or is unreachable → full diagnosis
    """
    from genesis.guardian.health_signals import HealthSnapshot

    # Calculate outage duration
    first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
    try:
        duration = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
    except (ValueError, TypeError):
        duration = 0.0

    # Step 1: Contact Genesis
    sm.set_contacting_genesis()
    logger.info("Contacting Genesis before attempting recovery...")

    request = build_request(
        snapshot=snapshot if isinstance(snapshot, HealthSnapshot) else HealthSnapshot(),
        duration_s=duration,
        guardian_state="surveying",
    )
    response = await send_dialogue(config, request)

    if response.acknowledged:
        logger.info(
            "Genesis responded: status=%s action=%s eta=%ds context=%s",
            response.status, response.action, response.eta_s, response.context,
        )

        if response.status == DialogueStatus.HANDLING:
            # Genesis is aware and acting — give it time
            sm.set_awaiting_self_heal(
                action=response.action,
                eta_s=response.eta_s or 90,  # default 90s if not specified
            )
            logger.info(
                "Genesis is handling it: %s (ETA: %ds)",
                response.action, response.eta_s,
            )
            return

        if response.status == DialogueStatus.STAND_DOWN:
            # Genesis says this is expected — pause
            sm.set_paused(reason=response.context)
            logger.info("Genesis requested stand-down: %s", response.context)
            return

        # NEED_HELP — Genesis explicitly asked for help, proceed to diagnosis
        logger.info("Genesis requested help: %s", response.context)

    else:
        logger.warning(
            "Genesis unreachable or errored: %s", response.context,
        )

    # Step 2: Genesis can't help — full diagnosis and recovery pipeline
    await _proceed_to_diagnosis(
        config, sm, dispatcher, diagnosis_engine, recovery_engine,
    )


async def _handle_awaiting_self_heal(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    diagnosis_engine: DiagnosisEngine,
    recovery_engine: RecoveryEngine,
    snapshot: object,
) -> None:
    """Check if Genesis's self-heal worked. If ETA expired and still down, proceed.

    Uses the snapshot already collected and processed by _check_cycle.
    The state machine transition was already computed — we act on the current state.
    """
    current = sm.current_state

    if current == GuardianState.HEALTHY:
        logger.info("Genesis self-healed successfully")
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO,
            title="Genesis self-healed",
            body=f"Genesis fixed itself: {sm.state.dialogue_action}",
        ))
        return

    if current == GuardianState.CONFIRMED_DEAD:
        # ETA expired, Genesis failed to self-heal
        logger.warning(
            "Genesis self-heal failed (ETA expired for: %s)",
            sm.state.dialogue_action,
        )
        await _proceed_to_diagnosis(
            config, sm, dispatcher, diagnosis_engine, recovery_engine,
        )
        return

    # Still waiting — ETA not expired yet
    logger.info(
        "Waiting for Genesis self-heal: %s",
        sm.state.dialogue_action,
    )


async def _proceed_to_diagnosis(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    diagnosis_engine: DiagnosisEngine,
    recovery_engine: RecoveryEngine,
) -> None:
    """Full diagnosis and recovery pipeline — Genesis is truly down."""
    await dispatcher.send(Alert(
        severity=AlertSeverity.CRITICAL,
        title="Genesis down — running diagnostics",
        body="Genesis could not be contacted or could not self-heal. "
             "Running full diagnostics...",
    ))

    diagnostic = await collect_diagnostics(config)
    signal_summary = json.dumps(sm.state.signal_history[-5:], indent=2)
    diagnosis = await diagnosis_engine.diagnose(diagnostic, signal_summary)

    logger.info(
        "Diagnosis: %s (confidence=%d%%, action=%s, outcome=%s, source=%s)",
        diagnosis.likely_cause,
        diagnosis.confidence_pct,
        diagnosis.recommended_action,
        diagnosis.outcome,
        diagnosis.source,
    )

    # Persist diagnosis to shared mount for Genesis to ingest on recovery
    first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
    try:
        outage_s = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
    except (ValueError, TypeError):
        outage_s = 0.0
    write_diagnosis_result(diagnosis, config, outage_duration_s=outage_s)

    # Track CC availability state
    if diagnosis.source == "cc_unavailable":
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        logger.warning("CC unavailable — Guardian in alert-only mode")
    elif diagnosis.source == "cc":
        if sm.state.cc_unavailable_since:
            logger.info("CC recovered — resuming intelligent diagnosis")
        sm.clear_cc_unavailable()

    # CC-driven recovery: if CC already resolved the issue, verify and report
    if diagnosis.outcome == "resolved":
        logger.info("CC resolved the issue — verifying recovery")
        await _handle_cc_resolved(config, sm, dispatcher, diagnosis, recovery_engine)
        return

    sm.set_confirmed_dead()

    await _execute_recovery_with_approval(
        config, sm, dispatcher, recovery_engine, diagnosis,
    )


async def _handle_cc_resolved(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    diagnosis: object,
    recovery_engine: RecoveryEngine | None = None,
) -> None:
    """CC already fixed the problem — verify and report.

    When the agentic CC session resolves the issue itself (investigation +
    recovery + verification), we skip the approval flow and just confirm
    health has returned. If verification fails, falls through to approval-
    gated recovery (never exits passively).
    """
    # Verify by re-checking signals
    await asyncio.sleep(config.recovery.verification_delay_s)
    snapshot = await collect_all_signals(config)
    sm.process(snapshot)

    actions_str = ", ".join(diagnosis.actions_taken) if diagnosis.actions_taken else "unknown"

    if sm.current_state == GuardianState.HEALTHY:
        logger.info("CC-driven recovery verified — Genesis is healthy")
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO,
            title="Genesis recovered (CC auto-resolved)",
            body=f"Cause: {diagnosis.likely_cause}\n"
                 f"Confidence: {diagnosis.confidence_pct}%\n"
                 f"Actions: {actions_str}\n"
                 f"Outcome: {diagnosis.outcome}",
        ))
        # Heartbeat is now written in run_check after the full cycle; no
        # redundant write here.
    else:
        logger.warning(
            "CC reported resolved but signals still failing — "
            "falling back to approval-based recovery",
        )
        if recovery_engine is not None:
            # Ensure state is CONFIRMED_DEAD before approval flow —
            # may still be in SURVEYING if called from _proceed_to_diagnosis
            if sm.current_state != GuardianState.CONFIRMED_DEAD:
                sm.set_confirmed_dead()
            # Route to approval gate — never exit passively
            await _execute_recovery_with_approval(
                config, sm, dispatcher, recovery_engine, diagnosis,
            )
        else:
            # Defensive: should not happen (callers pass recovery_engine),
            # but send alert rather than silently dropping
            cname = config.container_name
            await dispatcher.send(Alert(
                severity=AlertSeverity.CRITICAL,
                title="Genesis down — CC fix did not hold",
                body=(
                    f"CC diagnosed: {diagnosis.likely_cause}\n"
                    f"CC actions taken: {actions_str}\n"
                    "Post-fix health check: FAILED (signals still down)\n"
                    "Approval-gated recovery pipeline unavailable.\n\n"
                    "IMMEDIATE ACTION REQUIRED:\n"
                    "1. SSH to host VM\n"
                    "2. Check Guardian log: ~/.local/state/genesis-guardian/guardian.log\n"
                    "3. Review latest diagnosis: ls -t "
                    "~/.local/state/genesis-guardian/shared/findings/ | head -1\n"
                    f"4. Manual restart: incus exec {cname} -- "
                    f"su - ubuntu -c 'systemctl --user restart genesis-server'"
                ),
            ))


async def _handle_confirmed_dead(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    diagnosis_engine: DiagnosisEngine,
    recovery_engine: RecoveryEngine,
) -> None:
    """Handle confirmed dead state — re-diagnose and attempt recovery."""
    diagnostic = await collect_diagnostics(config)
    signal_summary = json.dumps(sm.state.signal_history[-5:], indent=2)
    diagnosis = await diagnosis_engine.diagnose(diagnostic, signal_summary)

    # Persist re-diagnosis to shared mount
    first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
    try:
        outage_s = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
    except (ValueError, TypeError):
        outage_s = 0.0
    write_diagnosis_result(diagnosis, config, outage_duration_s=outage_s)

    # Track CC availability transitions
    if diagnosis.source == "cc_unavailable":
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        logger.warning(
            "CC unavailable — routing through approval gate with fallback diagnosis",
        )
        # Don't return early — fall through to approval gate. The approval
        # server blocks for token_expiry_s (default 86400s = 24h), and the
        # Guardian is a oneshot service so the timer won't overlap. This
        # effectively throttles to one approval cycle per token_expiry_s.
        # NOTE: if token_expiry_s is changed, this implicit throttle changes too.
    elif diagnosis.source == "cc":
        if sm.state.cc_unavailable_since:
            logger.info("CC recovered — proceeding with CC diagnosis")
        sm.clear_cc_unavailable()

    # CC-driven recovery: if CC already resolved, verify and report
    if diagnosis.outcome == "resolved":
        logger.info("CC resolved the issue on re-diagnosis — verifying")
        await _handle_cc_resolved(config, sm, dispatcher, diagnosis, recovery_engine)
        return

    if sm.should_escalate():
        diagnosis = diagnosis.__class__(
            likely_cause=diagnosis.likely_cause,
            confidence_pct=diagnosis.confidence_pct,
            evidence=diagnosis.evidence,
            recommended_action="ESCALATE",
            actions_taken=diagnosis.actions_taken,
            outcome=diagnosis.outcome,
            reasoning=f"Max recovery attempts ({sm.state.recovery_attempts}) exceeded. "
                      + diagnosis.reasoning,
            source=diagnosis.source,
            cc_failure_reason=diagnosis.cc_failure_reason,
        )

    await _execute_recovery_with_approval(
        config, sm, dispatcher, recovery_engine, diagnosis,
    )


async def _execute_recovery_with_approval(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    recovery_engine: RecoveryEngine,
    diagnosis: object,
) -> None:
    """Send alert with approval link, wait for approval, then recover."""
    from genesis.guardian.diagnosis import RecoveryAction

    if diagnosis.recommended_action == RecoveryAction.ESCALATE:
        # ESCALATE is not an automated action — it means "all automated
        # recovery options exhausted, alert the user." No approval gate
        # needed because there's nothing to approve. The recovery engine
        # sends a clear alert asking for manual intervention.
        await recovery_engine.execute(diagnosis)
        return

    # Start approval server
    approval = ApprovalServer(config.approval)
    try:
        approval_url = approval.start()

        # Send alert with approval link
        failed_signals = [s.name for s in (await collect_all_signals(config)).failed_signals]
        first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
        try:
            duration = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
        except (ValueError, TypeError):
            duration = 0.0

        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title=f"Genesis down — approve recovery: {diagnosis.recommended_action.value}",
            body=f"Cause: {diagnosis.likely_cause}\n"
                 f"Confidence: {diagnosis.confidence_pct}%\n"
                 f"Source: {diagnosis.source}",
            approval_url=approval_url,
            failed_probes=failed_signals,
            duration_s=duration,
            likely_cause=diagnosis.likely_cause,
            proposed_action=diagnosis.recommended_action.value,
        ))

        # Wait for approval (blocks until approved or timeout)
        approved = approval.wait_for_approval(
            timeout_s=config.approval.token_expiry_s,
        )

        if approved:
            logger.info("Recovery approved by user")
            await recovery_engine.execute(diagnosis)
        else:
            logger.warning("Recovery approval timed out — no action taken")
            failed_desc = ", ".join(failed_signals) if failed_signals else "unknown"
            timeout_h = config.approval.token_expiry_s // 3600
            cname = config.container_name
            await dispatcher.send(Alert(
                severity=AlertSeverity.WARNING,
                title="Recovery approval expired — Genesis still down",
                body=(
                    f"Proposed action: {diagnosis.recommended_action.value}\n"
                    f"Cause: {diagnosis.likely_cause}\n"
                    f"Failed signals: {failed_desc}\n"
                    f"Approval link expired after {timeout_h}h with no response.\n\n"
                    "No automated recovery was performed.\n"
                    "Guardian will re-diagnose on next timer cycle.\n\n"
                    "If you want to act now:\n"
                    f"1. incus exec {cname} -- su - ubuntu -c "
                    f"'systemctl --user restart genesis-server'\n"
                    "2. Or re-trigger Guardian: systemctl --user restart "
                    "genesis-guardian.timer"
                ),
                failed_probes=failed_signals,
                duration_s=duration,
                likely_cause=diagnosis.likely_cause,
                proposed_action=diagnosis.recommended_action.value,
            ))
    finally:
        approval.stop()
