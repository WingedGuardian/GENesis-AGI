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
import os
from datetime import UTC, datetime
from html import escape as html_escape
from pathlib import Path

from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel
from genesis.guardian.collector import collect_diagnostics
from genesis.guardian.config import GuardianConfig, load_config, load_secrets
from genesis.guardian.diagnosis import DiagnosisEngine
from genesis.guardian.diagnosis_writer import write_diagnosis_result
from genesis.guardian.dialogue import DialogueStatus, build_request, send_dialogue
from genesis.guardian.health_signals import collect_all_signals
from genesis.guardian.pool import (
    TIER_CRIT,
    TIER_OK,
    decide_alert,
    measure_storage_pool,
    worst_tier,
)
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
    dispatcher = AlertDispatcher(
        fallback_queue_root=config.state_path / "alerts" / "queue",
    )

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


def _alert_from_queue_entry(entry: dict) -> Alert:
    """Rebuild an ``Alert`` from a persisted queue entry (schema v1)."""
    meta = entry.get("meta") or {}
    try:
        severity = AlertSeverity(entry.get("severity", "warning"))
    except ValueError:
        severity = AlertSeverity.WARNING
    return Alert(
        severity=severity,
        title=entry.get("title", ""),
        body=entry.get("body", ""),
        approval_url=meta.get("approval_url"),
        likely_cause=meta.get("likely_cause"),
        proposed_action=meta.get("proposed_action"),
    )


async def _drain_host_alert_queue(
    config: GuardianConfig, dispatcher: AlertDispatcher,
) -> None:
    """Replay queued host alerts through the dispatcher, then bound the queue.

    Never raises — a drain failure must not stop the health check. The replay
    send disables the queue fallback so a still-down channel does not
    re-enqueue the entry it is replaying (see ``AlertDispatcher.send``).
    """
    from genesis.guardian.alert import queue as alert_queue

    root = config.state_path / "alerts" / "queue"

    async def _send(entry: dict) -> bool:
        # Host has no dedup layer: dispatcher.send returns True iff a channel
        # delivered → terminal (unlink); False → still down → keep + stop.
        return await dispatcher.send(
            _alert_from_queue_entry(entry), allow_queue_fallback=False,
        )

    try:
        drained = await alert_queue.drain(root, _send)
        if drained:
            logger.info("Replayed %d queued host alert(s)", drained)
        alert_queue.prune(root)
    except Exception:
        logger.warning("host alert-queue drain failed", exc_info=True)


def _build_provisioning_adapter(config: GuardianConfig):
    """Build a Proxmox provisioning adapter from config + tokens, or None.

    Returns None when provisioning is disabled or unconfigured. Tokens are
    resolved all-or-nothing per source (mirrors ``_build_dispatcher``): the
    first source that carries an audit token supplies BOTH its tokens —
    env → shared-mount bridge → legacy secrets.env. The audit token is
    required (reads); the provision token enables mutations (absent ⇒ a
    read-only adapter — grows will 401 as a safe degradation, never a crash).
    """
    pc = config.provisioning
    if not pc.enabled:
        return None
    if not (pc.api_host and pc.node and pc.vmid):
        logger.warning(
            "provisioning enabled but api_host/node/vmid unset — no adapter built",
        )
        return None

    def _from_env() -> tuple[str, str]:
        return (
            os.environ.get("PROXMOX_AUDIT_TOKEN", ""),
            os.environ.get("PROXMOX_PROVISION_TOKEN", ""),
        )

    def _from_bridge() -> tuple[str, str]:
        from genesis.guardian.credential_bridge import load_provisioning_credentials
        creds = load_provisioning_credentials(config.state_dir)
        return (
            creds.get("PROXMOX_AUDIT_TOKEN", ""),
            creds.get("PROXMOX_PROVISION_TOKEN", ""),
        )

    def _from_legacy() -> tuple[str, str]:
        secrets = load_secrets()
        return (
            secrets.get("PROXMOX_AUDIT_TOKEN", ""),
            secrets.get("PROXMOX_PROVISION_TOKEN", ""),
        )

    audit = provision = ""
    for source in (_from_env, _from_bridge, _from_legacy):
        a, p = source()
        a, p = a.strip(), p.strip()
        if a:  # all-or-nothing: this source owns both tokens
            audit, provision = a, p
            break

    if not audit:
        logger.warning(
            "provisioning enabled but no PROXMOX_AUDIT_TOKEN in env/bridge/secrets",
        )
        return None

    from genesis.guardian.provisioning.proxmox import ProxmoxAdapter
    return ProxmoxAdapter(pc, audit_token=audit, provision_token=provision)


async def _maybe_propose_provisioning(
    config: GuardianConfig,
    dispatcher: AlertDispatcher,
) -> None:
    """Autonomous pool-crit → PROPOSE a disk grow (Genesis-DOWN path only).

    Caller gates on ``genesis_down`` — invoked only when the main Genesis bot
    is dead, so the guardian's getUpdates approval read is uncontended. When
    Genesis is UP the container owns any grow; the guardian just alerts.
    Never raises into the tick.
    """
    pc = config.provisioning
    if not (pc.enabled and pc.propose_on_pool_crit):
        return
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return
    from genesis.guardian.provisioning.flow import maybe_propose_pool_grow
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    try:
        await maybe_propose_pool_grow(
            config, adapter, dispatcher, ProvisioningLedger(config.state_dir),
        )
    except Exception:
        logger.warning("autonomous pool-grow proposal failed", exc_info=True)


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

    maintenance_path = Path(config.maintenance_file)
    if maintenance_path.exists():
        logger.info(
            "Maintenance mode active — standing down (remove %s to resume)",
            maintenance_path,
        )
        return

    state_path = config.state_path / "state.json"
    config.state_path.mkdir(parents=True, exist_ok=True)

    # Load persistent state
    sm = ConfirmationStateMachine(config)
    sm.load_state(state_path)

    dispatcher = _build_dispatcher(config)
    snapshots = SnapshotManager(config)
    diagnosis_engine = DiagnosisEngine(config)
    recovery_engine = RecoveryEngine(config, sm, snapshots, dispatcher)

    # F.3: drain any alerts queued while host→Telegram was down BEFORE running
    # the cycle — a recovered channel flushes the backlog first, and new
    # failures this tick re-enqueue behind it. Best-effort; never blocks a check.
    await _drain_host_alert_queue(config, dispatcher)

    try:
        await _check_cycle(config, sm, dispatcher, snapshots, diagnosis_engine, recovery_engine)
        # Snapshot lifecycle maintenance runs regardless of resulting state —
        # a guardian stuck in confirmed_dead/recovering for weeks must still
        # enforce expiry + prune stale snapshots (the incident: prune only ran
        # in HEALTHY, so it never fired while the pool silently filled).
        # is_healthy reflects THIS tick's post-cycle state: the daily healthy
        # snapshot (offline rollback lifeline) is only taken when HEALTHY.
        await _maintain_snapshots(
            config,
            snapshots,
            is_healthy=sm.current_state == GuardianState.HEALTHY,
            snapshot_size_history=sm.state.snapshot_size_history,
        )
        # Heartbeat means "Guardian process is alive and watching" —
        # NOT "Genesis container is healthy". Any successful check cycle
        # (regardless of resulting state) should refresh liveness. A
        # crashed _check_cycle raises out before reaching here, which
        # correctly withholds the heartbeat — that is a real Guardian
        # failure that Genesis-side monitoring should see. Written BEFORE the
        # pool check because the autonomous provisioning propose below can
        # block on an APPROVE for up to approval_timeout_s — liveness must not
        # hinge on that optional, bounded wait completing.
        await _write_guardian_heartbeat(config)
        # Storage-pool monitoring runs every cycle regardless of state — a
        # filling thin pool is the exact silent failure that caused the outage.
        # genesis_down gates the autonomous provisioning propose: only when
        # Genesis is CONFIRMED_DEAD is the main bot not polling, so only then
        # can the guardian read an APPROVE via getUpdates without a 409.
        await _check_storage_pool_and_alert(
            config, dispatcher,
            genesis_down=(sm.current_state == GuardianState.CONFIRMED_DEAD),
        )
        # Credential-file integrity backstop. The container self-heals first; the
        # guardian WARNs on first sight and steps in only after the grace window —
        # covering the window a degraded/dead server's awareness loop can't.
        await _check_credential_integrity_and_alert(config, dispatcher)
        # Git-repository health (F.1) — the PRIMARY detector for the outage class
        # that zeroed .git and disabled REVERT_CODE; a live incus-exec probe, since
        # the container's own awareness check may be dead exactly when it matters.
        await _check_container_git_and_alert(config, dispatcher)
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
        # GUARD-R2-01: on GENUINE recovery from a down-episode we alerted about,
        # send ONE "restored" ping and clear the flag. Gate on snapshot.all_alive
        # so auto-reset (routes CONFIRMED_DEAD→HEALTHY while STILL down,
        # all_alive=False) neither pings nor clears — the storm stays suppressed.
        if sm.state.down_alert_sent and snapshot.all_alive:
            if transition.old_state == GuardianState.CONFIRMED_DEAD:
                # Autonomous recovery (container returned on its own — process()
                # detected all_alive). The recovery-engine / CC-resolved /
                # approval / self-heal paths clear the flag at their own
                # recovery point + emit their own success alert, so
                # down_alert_sent is already False here for them — no double-ping.
                await dispatcher.send(Alert(
                    severity=AlertSeverity.INFO,
                    title="Genesis recovered",
                    body="Genesis is back online — all health signals passing.",
                ))
            sm.clear_down_alert_sent()
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
    # Snapshot lifecycle maintenance moved to _maintain_snapshots (called from
    # run_check for ALL states, not only HEALTHY).


async def _maintain_snapshots(
    config: GuardianConfig,
    snapshots: SnapshotManager,
    is_healthy: bool = False,
    snapshot_size_history: list[int] | None = None,
) -> None:
    """Enforce snapshot expiry, prune, and take the daily healthy snapshot.

    Runs after every check cycle regardless of guardian state, but the actual
    work (an ``incus config set`` + a list/delete pass) is throttled to once
    per 24h via a marker — the first tick has no marker so both fire
    immediately on deploy, then daily. `snapshots.expiry` is a persistent incus
    setting, so re-asserting it daily is ample and avoids a per-tick subprocess.

    The healthy snapshot (``is_healthy`` must reflect THIS tick's state — never
    snapshot a broken container as "healthy") is the offline SNAPSHOT_ROLLBACK
    lifeline: before this wiring, ``mark_healthy`` had no callers, so rollback
    could never find a target. Taken after prune so the pool is at its
    cleanest; rotation inside ``mark_healthy`` keeps exactly one.
    """
    prune_marker = config.state_path / ".last_prune"
    should_run = True
    if prune_marker.exists():
        try:
            last_prune = datetime.fromisoformat(prune_marker.read_text().strip())
            hours_since = (datetime.now(UTC) - last_prune).total_seconds() / 3600
            should_run = hours_since >= 24
        except (ValueError, OSError):
            should_run = True

    if not should_run:
        return

    # Idempotent — keep incus daemon-side expiry in force (guardian-independent
    # safety net that fires even if the guardian process later dies).
    try:
        await snapshots.enforce_expiry_policy()
    except Exception:
        logger.warning("enforce_expiry_policy failed", exc_info=True)

    try:
        pruned = await snapshots.prune()
        if pruned > 0:
            logger.info("Pruned %d old snapshots", pruned)
    except Exception:
        logger.warning("Snapshot prune failed", exc_info=True)

    if is_healthy and config.snapshots.healthy_enabled:
        try:
            name = await snapshots.mark_healthy(snapshot_size_history)
            if name:
                logger.info("Healthy snapshot refreshed: %s", name)
            else:
                logger.warning(
                    "Healthy snapshot NOT taken (pool gate or create failure) "
                    "— offline rollback lifeline not refreshed this cycle",
                )
        except Exception:
            logger.warning("Healthy snapshot take failed", exc_info=True)

    prune_marker.parent.mkdir(parents=True, exist_ok=True)
    prune_marker.write_text(datetime.now(UTC).isoformat())


_POOL_TIER_SEVERITY = {
    "warn": AlertSeverity.WARNING,
    "high": AlertSeverity.WARNING,
    "crit": AlertSeverity.CRITICAL,
}


async def _check_credential_integrity_and_alert(
    config: GuardianConfig, dispatcher: AlertDispatcher,
) -> None:
    """Guardian-side credential-integrity backstop (delegates to cred_watch).

    Never raises into the tick — a check exec failure is 'no signal', not an
    alert (container-down is the state machine's job)."""
    try:
        from genesis.guardian.cred_watch import check_credential_integrity_and_alert
        await check_credential_integrity_and_alert(config, dispatcher)
    except Exception:
        logger.warning("credential-integrity watch failed", exc_info=True)


async def _check_container_git_and_alert(
    config: GuardianConfig, dispatcher: AlertDispatcher,
) -> None:
    """Guardian-side git-health watch (delegates to git_watch).

    Live incus-exec probe of the container's local git — the PRIMARY detector,
    since the rootfs-RO outage this guards against can take the container's own
    awareness-loop alerting down. Never raises into the tick."""
    try:
        from genesis.guardian.git_watch import check_container_git_and_alert
        await check_container_git_and_alert(config, dispatcher)
    except Exception:
        logger.warning("git-health watch failed", exc_info=True)


async def _check_storage_pool_and_alert(
    config: GuardianConfig,
    dispatcher: AlertDispatcher,
    genesis_down: bool = False,
) -> None:
    """Measure the host storage pool and emit tiered alerts with hysteresis.

    State (last-alerted tier + timestamp) persists in the guardian state dir so
    the hysteresis survives across the guardian's stateless per-tick invocations.
    Alerts go through the guardian's own dispatcher (host Telegram channel),
    which survives a read-only/dead container — exactly when this matters most.
    """
    cfg = config.storage_pool
    if not cfg.enabled:
        return

    try:
        status = await measure_storage_pool(config)
    except Exception:
        logger.warning("storage-pool measurement failed", exc_info=True)
        return
    if not status.detected:
        logger.debug("storage-pool not measurable: %s", status.detail)
        return

    tier = worst_tier(status, cfg)

    state_file = config.state_path / "pool_alert_state.json"
    last_tier = TIER_OK
    last_alert_at: datetime | None = None
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            last_tier = data.get("tier", TIER_OK)
            raw_at = data.get("last_alert_at")
            last_alert_at = datetime.fromisoformat(raw_at) if raw_at else None
        except (ValueError, OSError):
            pass

    now = datetime.now(UTC)
    decision = decide_alert(tier, last_tier, last_alert_at, now, cfg.realert_hours)

    if decision.should_alert:
        if decision.is_resolution:
            severity = AlertSeverity.INFO
            title = "Storage pool recovered"
        else:
            severity = _POOL_TIER_SEVERITY.get(tier, AlertSeverity.WARNING)
            title = f"Storage pool {tier.upper()}"
        parts = []
        if status.data_pct is not None:
            parts.append(f"data {status.data_pct:.0f}%")
        if status.metadata_pct is not None:
            parts.append(f"metadata {status.metadata_pct:.0f}%")
        if status.vg_free_bytes is not None:
            parts.append(f"VG free {status.vg_free_bytes / 1024**3:.1f}G")
        if status.pool_used_pct is not None:
            parts.append(f"pool used {status.pool_used_pct:.0f}%")
        body = f"{decision.reason}. " + ", ".join(parts)
        if tier == TIER_CRIT:
            pool_kind = "Thin pool" if status.data_pct is not None else "Storage pool"
            body += (
                f"\n{pool_kind} near exhaustion — add space or free allocation "
                "before it forces the container read-only."
            )
        try:
            await dispatcher.send(Alert(severity=severity, title=title, body=body))
        except Exception:
            logger.warning("failed to send storage-pool alert", exc_info=True)

    # Persist tier every cycle (even without an alert) so a later rise from a
    # silently-decreased tier re-alerts correctly. Only advance the alert
    # timestamp when we actually alerted.
    new_alert_at = now if decision.should_alert else last_alert_at
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "tier": tier,
            "last_alert_at": new_alert_at.isoformat() if new_alert_at else None,
        }))
    except OSError:
        logger.warning("failed to persist pool alert state", exc_info=True)

    # Autonomous provisioning rung: only when the pool is CRITICAL *and* Genesis
    # is confirmed down this tick (so the guardian's getUpdates approval read is
    # uncontended — the outage-recovery path). When Genesis is up, the container
    # owns any grow; here we only alerted. The min_repropose_hours damper inside
    # maybe_propose_pool_grow prevents re-offering every tick.
    if tier == TIER_CRIT and genesis_down:
        await _maybe_propose_provisioning(config, dispatcher)


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
    """Event-driven self-heal check. Re-queries Sentinel state each tick.

    Guardian waits as long as Sentinel is in an active state (investigating,
    remediating, awaiting approval). No wall-clock timeout. User sovereignty
    is absolute — if Sentinel is parked on approval for 8 hours, Guardian
    waits 8 hours.

    Proceeds to diagnosis ONLY when:
    - Sentinel escalated (explicitly gave up)
    - Sentinel healthy but probes still fail (fixed wrong thing)
    - Genesis unreachable (Sentinel is down too)
    """
    from genesis.guardian.health_signals import HealthSnapshot

    current = sm.current_state

    if current == GuardianState.HEALTHY:
        logger.info("Genesis self-healed successfully")
        await dispatcher.send(Alert(
            severity=AlertSeverity.INFO,
            title="Genesis self-healed",
            body=f"Genesis fixed itself: {sm.state.dialogue_action}",
        ))
        return

    # Re-query Genesis for updated Sentinel state on each tick.
    # The dialogue endpoint is safe to re-POST: the is_active check
    # prevents re-dispatching Sentinel when it's already running.
    first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
    try:
        duration = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
    except (ValueError, TypeError):
        duration = 0.0

    request = build_request(
        snapshot=snapshot if isinstance(snapshot, HealthSnapshot) else HealthSnapshot(),
        duration_s=duration,
        guardian_state="awaiting_self_heal",
    )
    response = await send_dialogue(config, request)

    if response.acknowledged and response.sentinel_state:
        sm.update_sentinel_state(response.sentinel_state)
    elif not response.acknowledged:
        # Genesis unreachable — Sentinel is down too
        sm.update_sentinel_state("")

    if current == GuardianState.CONFIRMED_DEAD:
        # State machine already decided to proceed (Sentinel escalated,
        # healthy-but-failing, or unreachable)
        logger.warning(
            "Sentinel state '%s' — proceeding to diagnosis (was: %s)",
            sm.state.sentinel_state or "unreachable",
            sm.state.dialogue_action,
        )
        await _proceed_to_diagnosis(
            config, sm, dispatcher, diagnosis_engine, recovery_engine,
        )
        return

    # Still waiting — Sentinel is active
    logger.info(
        "Waiting for Sentinel (%s): %s",
        sm.state.sentinel_state,
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
    # GUARD-R2-01: this episode's "down" alert has now fired — subsequent
    # CONFIRMED_DEAD ticks must NOT re-diagnose/re-alert (no 30s storm).
    sm.mark_down_alert_sent()

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
    """FIREWALL: CC must never auto-resolve under propose-only mode.

    Under the propose-only redesign, CC investigates and PROPOSES — it never
    executes recovery, so `outcome` should always be "proposed" and this
    handler should never be reached. If CC ever returns `outcome == "resolved"`
    anyway (a prompt regression, a stale response, or CC ignoring the
    instruction and acting on its own), we MUST NOT silently confirm a
    recovery we did not authorize. Instead we log a warning and route through
    the human approval gate like any other diagnosis.
    """
    logger.warning(
        "CC reported resolved under propose-only mode — routing to approval "
        "gate (CC must not self-resolve; treating as an unauthorized claim)",
    )

    if recovery_engine is not None:
        # Ensure state is CONFIRMED_DEAD before the approval flow — may still
        # be in SURVEYING if called from _proceed_to_diagnosis.
        if sm.current_state != GuardianState.CONFIRMED_DEAD:
            sm.set_confirmed_dead()
        await _execute_recovery_with_approval(
            config, sm, dispatcher, recovery_engine, diagnosis,
        )
        return

    # Defensive: callers always pass recovery_engine, but never exit passively.
    actions_str = (
        ", ".join(diagnosis.actions_taken) if diagnosis.actions_taken else "unknown"
    )
    cname = config.container_name
    await dispatcher.send(Alert(
        severity=AlertSeverity.CRITICAL,
        title="Genesis down — CC claimed resolved (no approval pipeline)",
        body=(
            f"CC diagnosed: {diagnosis.likely_cause}\n"
            f"CC reported steps: {actions_str}\n"
            "CC claimed 'resolved' under propose-only mode — NOT trusted.\n"
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
    # GUARD-R2-01: alert once per down-episode. If we already notified the user
    # this episode, stay quiet — no re-diagnosis (the expensive Opus run), no
    # recovery retry, no re-alert — until genuine recovery (which clears the flag
    # and sends a single "restored" ping). Recovery detection is unaffected: it
    # happens in state_machine.process() via the cheap signal probes, not here.
    if sm.state.down_alert_sent:
        logger.info(
            "Down alert already sent this episode — staying quiet until recovery "
            "(no re-diagnosis/re-alert)",
        )
        return
    # First handler for this episode (e.g. reached CONFIRMED_DEAD via the
    # self-heal-escalation path that skips _proceed_to_diagnosis): own it now so
    # the next tick skips.
    sm.mark_down_alert_sent()

    try:
        diagnostic = await collect_diagnostics(config)
        signal_summary = json.dumps(sm.state.signal_history[-5:], indent=2)
        diagnosis = await diagnosis_engine.diagnose(diagnostic, signal_summary)
    except Exception:
        # Diagnosis failed before any alert fired — un-mark so the next tick
        # retries instead of silently muting this episode.
        sm.clear_down_alert_sent()
        raise

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


def _find_telegram_channel(
    dispatcher: AlertDispatcher,
) -> TelegramAlertChannel | None:
    """Locate the TelegramAlertChannel held by the dispatcher, if any.

    The dispatcher owns its channel list (`_channels`). The keyword-approval
    gate needs the Telegram channel directly to send a reply-target prompt and
    long-poll getUpdates for the reply. Returns None when no Telegram channel
    is configured (alerts-only / journal-only mode).
    """
    for channel in dispatcher._channels:
        if isinstance(channel, TelegramAlertChannel):
            return channel
    return None


# Liveness heartbeat cadence for the blocking approval wait. The Guardian is a
# oneshot service with no other instance, so the wait can be long; we log a
# liveness line roughly every this-many seconds so journald shows the wait is
# alive (not hung) without spamming.
_APPROVAL_LIVENESS_LOG_S = 1800  # ~30 min
# Backoff between getUpdates long-poll windows when a 409 Conflict occurs (the
# main bot is contending for the token). Short — we want to retry the gate, but
# not hot-loop on the contended endpoint.
_APPROVAL_CONFLICT_BACKOFF_S = 5
# Consecutive 409s before we surface a "can't confirm Genesis is down" alert.
_APPROVAL_CONFLICT_ALERT_THRESHOLD = 3


async def _wait_for_gate_keyword(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    telegram_channel: TelegramAlertChannel,
    gate_msg_id: int,
    gate_label: str,
) -> str | None:
    """Block until the user replies APPROVE/DENY to the gate message.

    Self-cancels (returns "__RECOVERED__") if health returns on its own while
    waiting — no fixed timeout. Returns "APPROVE", "DENY", or "__RECOVERED__".

    On each loop iteration:
      1. Re-check health — if recovered, the caller stands down (no action).
      2. Long-poll getUpdates for an APPROVE/DENY reply to gate_msg_id.
      3. On 409 Conflict, count it; after N consecutive, alert that the main
         bot appears alive (can't confirm Genesis is down), then back off.
    """
    keywords = frozenset({"APPROVE", "DENY"})
    consecutive_conflicts = 0
    waited_s = 0.0
    last_liveness_log = 0.0

    while True:
        # 1. Self-cancel on genuine recovery (no fixed timeout — only health
        #    recovery ends the wait, per spec).
        recheck = await collect_all_signals(config)
        if not recheck.failed_signals:
            return "__RECOVERED__"

        # 2. Long-poll for a reply keyword (blocks server-side ~25s).
        kw = await telegram_channel.poll_for_keyword(gate_msg_id, keywords)

        if kw == CONFLICT_SENTINEL:
            consecutive_conflicts += 1
            logger.warning(
                "getUpdates 409 Conflict at %s gate (consecutive=%d) — main bot "
                "is polling the same token",
                gate_label, consecutive_conflicts,
            )
            if consecutive_conflicts >= _APPROVAL_CONFLICT_ALERT_THRESHOLD:
                await dispatcher.send(Alert(
                    severity=AlertSeverity.WARNING,
                    title="Cannot confirm main bot is down (getUpdates conflict)",
                    body=(
                        "Guardian is trying to read your APPROVE/DENY reply, but "
                        "the main Genesis Telegram bot is actively polling the "
                        "same token — which means it may still be alive. Manual "
                        "check needed: verify whether Genesis is actually down "
                        "before approving recovery."
                    ),
                ))
                consecutive_conflicts = 0  # re-arm; don't spam every loop
            await asyncio.sleep(_APPROVAL_CONFLICT_BACKOFF_S)
            waited_s += _APPROVAL_CONFLICT_BACKOFF_S
            continue

        consecutive_conflicts = 0

        if kw in ("APPROVE", "DENY"):
            return kw

        # No reply this window — loop. Account ~25s poll for liveness logging.
        waited_s += 25.0
        if waited_s - last_liveness_log >= _APPROVAL_LIVENESS_LOG_S:
            logger.info(
                "Still awaiting %s approval reply (~%.0f min elapsed, Genesis "
                "still down)",
                gate_label, waited_s / 60.0,
            )
            last_liveness_log = waited_s


async def _execute_recovery_with_approval(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    dispatcher: AlertDispatcher,
    recovery_engine: RecoveryEngine,
    diagnosis: object,
) -> None:
    """Two-gate, blocking, keyword-reply Telegram approval, then recover.

    Gate everything: nothing recovers before approval. No fixed timeout —
    the wait self-cancels only when health returns on its own.

    Gate 1 (investigate): authorize CC to investigate + propose an action.
    Gate 2 (action): authorize executing the specific proposed action.

    The Guardian is a oneshot service (no concurrent instance), so blocking
    here is safe. The host-side Guardian reads the reply itself via getUpdates.
    """
    from genesis.guardian.diagnosis import RecoveryAction

    if diagnosis.recommended_action == RecoveryAction.ESCALATE:
        # ESCALATE is not an automated action — it means "all automated
        # recovery options exhausted, alert the user." No approval gate
        # needed because there's nothing to approve. The recovery engine
        # sends a clear alert asking for manual intervention.
        await recovery_engine.execute(diagnosis)
        return

    telegram_channel = _find_telegram_channel(dispatcher)
    if telegram_channel is None:
        # No Telegram channel — we cannot run the keyword-reply gate. Fall
        # back to alerts-only: report that recovery needs manual action.
        # NEVER auto-recover without the approval gate (gate everything).
        logger.warning(
            "No Telegram channel — cannot run keyword-approval gate; "
            "alerting for manual intervention instead of auto-recovering",
        )
        cname = config.container_name
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down — approval gate unavailable (no Telegram)",
            body=(
                f"Proposed action: {diagnosis.recommended_action.value}\n"
                f"Cause: {diagnosis.likely_cause}\n"
                f"Confidence: {diagnosis.confidence_pct}%\n\n"
                "Guardian cannot read an approval reply (no Telegram channel "
                "configured) and will NOT auto-recover. Manual action:\n"
                f"1. incus exec {cname} -- su - ubuntu -c "
                f"'systemctl --user restart genesis-server'\n"
                "2. Or re-trigger Guardian: systemctl --user restart "
                "genesis-guardian.timer"
            ),
        ))
        return

    failed_signals = [s.name for s in (await collect_all_signals(config)).failed_signals]
    failed_desc = ", ".join(failed_signals) if failed_signals else "unknown"

    # ── Gate 1: authorize investigation ──────────────────────────────────
    gate1_text = (
        "\U0001f6a8 <b>Genesis down</b> — failed signals: "
        f"{html_escape(failed_desc)}.\n"
        f"Likely: {html_escape(diagnosis.likely_cause)}.\n\n"
        "<b>Reply to this message</b> with APPROVE to authorize recovery, "
        "or DENY."
    )
    gate1_msg_id = await telegram_channel.send_text(gate1_text)
    if gate1_msg_id is None:
        logger.error("Failed to send Gate 1 prompt — cannot run approval gate")
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down — could not send approval prompt",
            body=(
                "Guardian could not send the Telegram approval prompt. "
                f"Proposed action: {diagnosis.recommended_action.value}. "
                "No automated recovery performed."
            ),
        ))
        # A FAILED send is not a successful "alert once" — the user never saw a
        # prompt. Clear the episode flag so the next cycle retries delivery
        # rather than going permanently silent on a transient Telegram failure.
        sm.clear_down_alert_sent()
        return

    kw = await _wait_for_gate_keyword(
        config, sm, dispatcher, telegram_channel, gate1_msg_id, "Gate 1 (investigate)",
    )

    if kw == "__RECOVERED__":
        await telegram_channel.send_text(
            "✅ Genesis recovered on its own — standing down, no action taken"
        )
        snapshot = await collect_all_signals(config)
        sm.process(snapshot)  # transitions toward HEALTHY
        sm.clear_down_alert_sent()  # GUARD-R2-01: episode over (self-recovered)
        return

    if kw == "DENY":
        await telegram_channel.send_text("Recovery denied — no action taken.")
        # Clear the episode flag so the next cycle can re-diagnose fresh.
        sm.clear_down_alert_sent()
        return

    if kw != "APPROVE":
        # Defensive: the wait-loop only returns APPROVE/DENY/__RECOVERED__ today.
        # Never let an unexpected value fall through to recovery execution.
        logger.error("Gate 1 unexpected reply %r — aborting without recovery", kw)
        sm.clear_down_alert_sent()
        return

    # kw == "APPROVE" → proceed to diagnose/propose, then Gate 2.

    # ── Diagnose (propose-only) ───────────────────────────────────────────
    await telegram_channel.send_text("\U0001f50d Diagnosing…")

    diagnosis_engine = DiagnosisEngine(config)
    diagnostic = await collect_diagnostics(config)
    signal_summary = json.dumps(sm.state.signal_history[-5:], indent=2)
    diagnosis = await diagnosis_engine.diagnose(diagnostic, signal_summary)

    # F.1: REVERT_CODE runs `git stash`/`git revert`, which need healthy, WRITABLE
    # local git. If the container's git is unhealthy (the F.1 outage class — incl. a
    # read-only rootfs that passes reads but fails the writes stash/revert need), a
    # revert is doomed. Redirect the PROPOSAL to SNAPSHOT_ROLLBACK *before* Gate 2 so
    # the user approves the action that actually runs — never silently swap a
    # post-approval action (that would break gate-everything and hand the user a more
    # destructive recovery than they authorized). Rollback restores a healthy .git AND
    # repairs a read-only rootfs in one move.
    if diagnosis.recommended_action == RecoveryAction.REVERT_CODE:
        from genesis.guardian.git_watch import container_git_supports_revert

        if not await container_git_supports_revert(config):
            from dataclasses import replace

            logger.warning(
                "REVERT_CODE proposed but container git is unhealthy/read-only — "
                "redirecting the proposal to SNAPSHOT_ROLLBACK before approval"
            )
            diagnosis = replace(
                diagnosis,
                recommended_action=RecoveryAction.SNAPSHOT_ROLLBACK,
                likely_cause=(
                    diagnosis.likely_cause
                    + " — container git is unhealthy, so a code revert is unavailable; "
                    "proposing snapshot rollback instead"
                ),
            )

    logger.info(
        "Gate-1 approved; diagnosis: %s (confidence=%d%%, action=%s, source=%s)",
        diagnosis.likely_cause, diagnosis.confidence_pct,
        diagnosis.recommended_action.value, diagnosis.source,
    )

    # Persist the proposed diagnosis to the shared mount for Genesis to ingest.
    first_failure = sm.state.first_failure_at or datetime.now(UTC).isoformat()
    try:
        outage_s = (datetime.now(UTC) - datetime.fromisoformat(first_failure)).total_seconds()
    except (ValueError, TypeError):
        outage_s = 0.0
    write_diagnosis_result(diagnosis, config, outage_duration_s=outage_s)

    if diagnosis.recommended_action == RecoveryAction.ESCALATE:
        # Investigation concluded there is no safe automated action — escalate
        # (the recovery engine sends a manual-intervention alert).
        await telegram_channel.send_text(
            "Investigation complete — no safe automated recovery available. "
            "Escalating for manual intervention."
        )
        await recovery_engine.execute(diagnosis)
        return

    # ── Gate 2: authorize the specific action ─────────────────────────────
    gate2_text = (
        f"Diagnosed: {html_escape(diagnosis.likely_cause)} "
        f"({diagnosis.confidence_pct}%).\n"
        f"Proposed: <b>{html_escape(diagnosis.recommended_action.value)}</b>.\n\n"
        "<b>Reply</b> APPROVE to execute, or DENY."
    )
    gate2_msg_id = await telegram_channel.send_text(gate2_text)
    if gate2_msg_id is None:
        logger.error("Failed to send Gate 2 prompt — no recovery executed")
        await dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title="Genesis down — could not send action-approval prompt",
            body=(
                f"Proposed action: {diagnosis.recommended_action.value}. "
                "Guardian could not send the Telegram prompt. No recovery performed."
            ),
        ))
        # Failed delivery ≠ "already alerted" — let the next cycle retry.
        sm.clear_down_alert_sent()
        return

    kw2 = await _wait_for_gate_keyword(
        config, sm, dispatcher, telegram_channel, gate2_msg_id, "Gate 2 (action)",
    )

    if kw2 == "__RECOVERED__":
        await telegram_channel.send_text(
            "✅ Genesis recovered on its own — standing down, no action taken"
        )
        snapshot = await collect_all_signals(config)
        sm.process(snapshot)
        sm.clear_down_alert_sent()
        return

    if kw2 == "DENY":
        await telegram_channel.send_text("Action rejected — no recovery performed.")
        sm.clear_down_alert_sent()
        return

    if kw2 != "APPROVE":
        # Defensive: never execute recovery on an unexpected gate reply. The
        # APPROVE check below is positive, not a fall-through.
        logger.error("Gate 2 unexpected reply %r — NOT executing recovery", kw2)
        sm.clear_down_alert_sent()
        return

    # kw2 == "APPROVE" → execute the approved action.
    logger.info(
        "Gate-2 approved — executing recovery action %s",
        diagnosis.recommended_action.value,
    )
    await recovery_engine.execute(diagnosis)
